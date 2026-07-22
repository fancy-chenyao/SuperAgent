"""TaskGraph scheduler (Plan §8, Phase 3).

Executes a validated :class:`TaskGraph`:

- READY frontier from ``depends_on`` (a step runs once all deps have SUCCEEDED).
- Read-only steps run concurrently; write steps are serialized by
  ``resource_locks`` (a write with no declared lock takes an implicit shared
  lock, so untagged writes never overlap).
- Per-step ``timeout`` and ``retry``.
- Failure isolation: a failed step is not marked complete, so only its
  downstream branch is blocked; independent branches keep running. Re-running
  with ``initial_completed`` skips done steps and re-runs only the failed frontier.

The scheduler is decoupled from the real agent runtime via an injected
``execute_step`` coroutine, so it is fully unit-testable with a fake executor.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.interface.artifact import StepResult, StepStatus
from src.interface.task_graph import TaskGraph, TaskStep
from src.manager.executor.artifact_adapter import to_artifact
from src.orchestration.completion import (
    ReceiptStore,
    evaluate_completion,
    idempotency_key,
    validate_receipt,
)
from src.orchestration.providers import RoutingProvider
from src.orchestration.resolver import ArtifactResolver
from src.orchestration.store import ArtifactStore

# execute_step(step, selected_agent, inputs, context) -> ExecuteResult-like
ExecuteStep = Callable[..., Awaitable[Any]]
# Optional async lifecycle hooks (used by the runtime bridge for SSE/logging).
StepHook = Callable[..., Awaitable[None]]

_DEFAULT_WRITE_LOCK = "__write__"


class TaskScheduler:
    def __init__(
        self,
        *,
        execute_step: ExecuteStep,
        routing_provider: Optional[RoutingProvider] = None,
        store: Optional[ArtifactStore] = None,
        resolver: Optional[ArtifactResolver] = None,
        receipt_store: Optional[ReceiptStore] = None,
    ) -> None:
        self._execute_step = execute_step
        self._routing = routing_provider
        self.store = store or ArtifactStore()
        self.resolver = resolver or ArtifactResolver(self.store)
        self.receipt_store = receipt_store

        # Runtime state (reset per run)
        self._outputs: Dict[str, Dict[str, Any]] = {}
        # An agent may drive multiple steps, so map each agent to *all* of its
        # step ids (declaration order) instead of clobbering to the last one.
        self._agent_to_steps: Dict[str, List[str]] = {}

    async def run(
        self,
        graph: TaskGraph,
        *,
        context: Optional[dict] = None,
        initial_completed: Optional[set[str]] = None,
        on_step_start: Optional[StepHook] = None,
        on_step_end: Optional[StepHook] = None,
    ) -> Dict[str, StepResult]:
        """Execute ``graph`` and return ``{step_id: StepResult}``."""
        graph.validate_dag()
        smap = graph.step_map()
        context = context or {}

        self._outputs = {}
        self._agent_to_steps = {}
        for s in graph.steps:
            key = getattr(s, "agent_name", None) or s.step_id
            self._agent_to_steps.setdefault(key, []).append(s.step_id)

        completed: set[str] = set(initial_completed or [])
        attempted: set[str] = set(completed)
        results: Dict[str, StepResult] = {}

        while True:
            runnable = [
                s.step_id
                for s in graph.steps
                if s.step_id not in attempted
                and all(dep in completed for dep in s.depends_on)
            ]
            if not runnable:
                break

            batch = self._select_batch(runnable, smap)
            coros = [
                self._run_step(smap[sid], context, on_step_start, on_step_end)
                for sid in batch
            ]
            batch_results = await asyncio.gather(*coros)

            for sid, result in zip(batch, batch_results):
                results[sid] = result
                attempted.add(sid)
                if result.is_success:
                    completed.add(sid)
                    self._outputs[sid] = dict(result.outputs)

        return results

    def _select_batch(self, runnable: List[str], smap: Dict[str, TaskStep]) -> List[str]:
        """Pick a concurrent batch: all reads + writes with disjoint locks."""
        reads = [sid for sid in runnable if smap[sid].is_read_only]
        writes = [sid for sid in runnable if not smap[sid].is_read_only]

        selected_writes: List[str] = []
        used_locks: set[str] = set()
        for sid in writes:
            locks = set(smap[sid].resource_locks) or {_DEFAULT_WRITE_LOCK}
            if locks & used_locks:
                continue  # conflicting write: defer to a later round
            selected_writes.append(sid)
            used_locks |= locks

        return reads + selected_writes

    async def _run_step(
        self,
        step: TaskStep,
        context: dict,
        on_step_start: Optional[StepHook],
        on_step_end: Optional[StepHook],
    ) -> StepResult:
        # A single step must never crash the whole batch: routing, input
        # resolution, the start hook or completion evaluation raising would
        # otherwise abort ``asyncio.gather`` and kill independent branches.
        try:
            result = await self._execute_step_core(step, context, on_step_start)
        except Exception as exc:  # noqa: BLE001 - degrade to a failed step
            result = StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error=f"step crashed: {exc}",
                metrics={"crashed": True},
            )

        if on_step_end is not None:
            try:
                await on_step_end(step=step, result=result)
            except Exception:  # noqa: BLE001 - the end hook must not crash the batch
                pass
        return result

    async def _execute_step_core(
        self,
        step: TaskStep,
        context: dict,
        on_step_start: Optional[StepHook],
    ) -> StepResult:
        selected_agent = await self._route(step, context)
        inputs = self._resolve_inputs(step, context)

        if on_step_start is not None:
            await on_step_start(step=step, selected_agent=selected_agent, inputs=inputs)

        # Idempotency: a side-effect step with a valid prior receipt is not
        # re-executed (e.g. an email is never sent twice on retry/resume).
        idem_key: Optional[str] = None
        if self.receipt_store is not None and not step.is_read_only:
            idem_key = idempotency_key(context.get("task_id", ""), step.step_id, inputs)
            prior = self.receipt_store.get(idem_key)
            if prior and validate_receipt(prior):
                return StepResult(
                    step_id=step.step_id,
                    status=StepStatus.SUCCEEDED,
                    outputs=prior.get("outputs") or {},
                    metrics={"idempotent_reuse": True, "selected_agent": selected_agent},
                )

        attempts = max(1, step.retry + 1)
        last_error: Optional[str] = None
        result: Optional[StepResult] = None

        for attempt in range(attempts):
            try:
                exec_result = await self._invoke(step, selected_agent, inputs, context)
            except asyncio.TimeoutError:
                last_error = f"timeout after {step.timeout}s"
                continue
            except Exception as exc:  # noqa: BLE001 - record and maybe retry
                last_error = str(exc)
                continue

            if getattr(exec_result, "is_success", True):
                result = self._record_success(step, exec_result, context, attempt + 1)
                break
            last_error = getattr(exec_result, "error", None) or "step failed"

        if result is None:
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error=last_error,
                metrics={"attempts": attempts, "selected_agent": selected_agent},
            )

        # Completion conditions gate success; a failing predicate marks FAILED.
        passed, failed_expr = evaluate_completion(
            step.completion_conditions, result.outputs, result.metrics, "SUCCEEDED"
        )
        if not passed:
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error=f"completion condition failed: {failed_expr}",
                outputs=result.outputs,
                metrics=result.metrics,
            )
        if self.receipt_store is not None and not step.is_read_only and idem_key:
            self.receipt_store.put(
                idem_key,
                {
                    "step_id": step.step_id,
                    "status": "SUCCEEDED",
                    "outputs": result.outputs,
                    "idempotency_key": idem_key,
                },
            )
        return result

    async def _invoke(
        self, step: TaskStep, selected_agent: Optional[str], inputs: dict, context: dict
    ) -> Any:
        coro = self._execute_step(
            step=step, selected_agent=selected_agent, inputs=inputs, context=context
        )
        if step.timeout:
            return await asyncio.wait_for(coro, step.timeout)
        return await coro

    def _record_success(
        self, step: TaskStep, exec_result: Any, context: dict, attempts: int
    ) -> StepResult:
        artifact = to_artifact(exec_result, step=step, context=context.get("execution_context"))
        ref = self.store.put(artifact)
        names = list(step.expected_outputs) or [artifact.logical_name]
        outputs = {name: ref for name in names}
        return StepResult(
            step_id=step.step_id,
            status=StepStatus.SUCCEEDED,
            outputs=outputs,
            metrics={"attempts": attempts},
        )

    async def _route(self, step: TaskStep, context: dict) -> Optional[str]:
        if self._routing is None:
            return step.preferred_resource_id
        result = await self._routing.decide(
            step,
            user_query=context.get("user_query", ""),
            task_id=context.get("task_id", ""),
            workflow_id=context.get("workflow_id", ""),
            agents=context.get("agents", ()),
            authorized_agent_ids=context.get("authorized_agent_ids", set()),
            metadata=context.get("metadata"),
        )
        return getattr(result, "selected_agent", None) or step.preferred_resource_id

    def _find_source_step(self, src_agent: Optional[str]) -> Optional[str]:
        """Resolve an agent name in a binding to a concrete producer step id.

        A given agent may drive several steps, so bind to the most recent one
        that has already produced outputs; fall back to the first declared step
        when none has run yet (yields empty outputs, handled by the caller).
        """
        if not src_agent:
            return None
        step_ids = self._agent_to_steps.get(src_agent)
        if not step_ids:
            return None
        for sid in reversed(step_ids):
            if sid in self._outputs:
                return sid
        return step_ids[0]

    def _resolve_inputs(self, step: TaskStep, context: dict) -> Dict[str, Any]:
        """Best-effort resolve symbolic ``input_bindings`` to concrete values."""
        bindings = getattr(step, "input_bindings", None)
        resolved: Dict[str, Any] = {}
        if not isinstance(bindings, list):
            return resolved
        subject = context.get("subject")
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            param = binding.get("parameter_name")
            src_agent = binding.get("source_step")
            src_output = binding.get("source_output")
            src_step_id = self._find_source_step(src_agent)
            if not param or not src_step_id:
                continue
            outputs = self._outputs.get(src_step_id, {})
            ref = outputs.get(src_output) or (
                next(iter(outputs.values())) if outputs else None
            )
            if ref is None:
                continue
            try:
                resolved[param] = self.resolver.resolve(ref, subject=subject)
            except Exception:  # noqa: BLE001 - unresolved input is non-fatal here
                continue
        return resolved
