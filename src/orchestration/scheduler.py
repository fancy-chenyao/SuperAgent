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
import logging
import time
from collections.abc import Mapping
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.interface.artifact import StepResult, StepStatus
from src.interface.task_graph import TaskGraph, TaskStep, WorkflowStatus
from src.manager.executor.agent_result_adapter import (
    AgentResultNormalizationError,
    normalize_agent_result,
)
from src.manager.executor.artifact_adapter import to_artifacts
from src.manager.executor.base import ExecutionStatus
from src.orchestration.completion import (
    ClaimStatus,
    ReceiptStore,
    evaluate_completion,
    idempotency_key,
    normalize_input,
)
from src.orchestration.providers import RoutingProvider, RoutingResult
from src.orchestration.resolver import (
    ArtifactAccessDenied,
    ArtifactResolver,
    ArtifactSchemaIncompatible,
    ArtifactSchemaInvalid,
)
from src.orchestration.failure_mapper import (
    failure_from_exception,
    failure_from_step_result,
    make_failure,
)
from src.orchestration.schema_registry import get_schema_registry
from src.orchestration.store import ArtifactNotFoundError, ArtifactStore

logger = logging.getLogger(__name__)

# execute_step(step, selected_agent, inputs, context) -> ExecuteResult-like
ExecuteStep = Callable[..., Awaitable[Any]]


def _is_dispatch_permission_error(exc: BaseException) -> bool:
    """Recognize policy denials without importing the security stack eagerly."""

    if isinstance(exc, PermissionError):
        return True
    try:
        from src.security.enforcement import PermissionDeniedError
    except Exception:  # noqa: BLE001 - optional security dependencies
        return False
    return isinstance(exc, PermissionDeniedError)
# Optional async lifecycle hooks (used by the runtime bridge for SSE/logging).
StepHook = Callable[..., Awaitable[None]]

_DEFAULT_WRITE_LOCK = "__write__"


def _external_operation_id(exec_result: Any, artifact: Any) -> Optional[str]:
    """Extract a durable provider/business id from normalized executor output."""

    metadata = getattr(exec_result, "metadata", None) or {}
    candidates: list[Any] = []
    if isinstance(metadata, Mapping):
        candidates.extend(
            metadata.get(key)
            for key in (
                "external_op_id",
                "external_operation_id",
                "provider_operation_id",
            )
        )
    payload = getattr(artifact, "payload", None)
    if isinstance(payload, Mapping):
        outcome = payload.get("business_outcome")
        if isinstance(outcome, Mapping):
            resource = outcome.get("resource")
            candidates.extend(
                outcome.get(key)
                for key in (
                    "external_op_id",
                    "external_operation_id",
                    "resource_id",
                )
            )
            if isinstance(resource, Mapping):
                candidates.append(resource.get("id"))
        candidates.extend(
            payload.get(key)
            for key in (
                "external_op_id",
                "external_operation_id",
                "operation_id",
                "message_id",
                "request_id",
                "submission_id",
            )
        )
    for value in candidates:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


class StepPersistenceError(Exception):
    """Raised by a ``commit_step_result`` hook when a CRITICAL durable write
    (artifact payload / checkpoint) fails.

    The scheduler converts this (and any other exception from the commit hook)
    into a FAILED step so a successful step is NEVER reported as SUCCEEDED when
    its outputs/checkpoint could not be durably persisted.
    """


class WorkflowResult(dict):
    """``{step_id: StepResult}`` plus a workflow-level terminal verdict.

    Subclasses ``dict`` so existing callers keep using ``results[step_id]`` /
    ``results.values()`` / ``sid in results`` unchanged, while the runtime can
    read the workflow-level ``terminal_status`` (a :class:`WorkflowStatus`) and
    the classified step lists (clarifications / rejected / needs-reconciliation)
    instead of re-deriving them from per-step metrics.
    """

    def __init__(
        self,
        step_results: Optional[Dict[str, StepResult]] = None,
        *,
        terminal_status: WorkflowStatus,
        clarifications: Optional[List[str]] = None,
        rejected_steps: Optional[List[str]] = None,
        needs_reconciliation: Optional[List[str]] = None,
        blocked_steps: Optional[List[str]] = None,
        additional_failures: Optional[List[Any]] = None,
    ) -> None:
        super().__init__(step_results or {})
        self.terminal_status = terminal_status
        self.clarifications = list(clarifications or [])
        self.rejected_steps = list(rejected_steps or [])
        self.needs_reconciliation = list(needs_reconciliation or [])
        self.blocked_steps = list(blocked_steps or [])
        self.additional_failures = list(additional_failures or [])


class InputResolutionError(Exception):
    """A required upstream input could not be resolved (fail closed).

    ``reason`` is a machine-readable classification: ``artifact_not_produced``,
    ``artifact_not_found``, ``access_denied``, ``selector_error`` or
    ``schema_incompatible``. The scheduler converts this into a FAILED step
    instead of silently running the agent with a missing/empty input.
    """

    def __init__(self, *, param: str, source: Optional[str], reason: str, detail: str = "") -> None:
        self.param = param
        self.source = source
        self.reason = reason
        self.detail = detail
        msg = f"required input {param!r} from {source!r} unresolved: {reason}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


class TaskScheduler:
    def __init__(
        self,
        *,
        execute_step: ExecuteStep,
        routing_provider: Optional[RoutingProvider] = None,
        store: Optional[ArtifactStore] = None,
        resolver: Optional[ArtifactResolver] = None,
        receipt_store: Optional[ReceiptStore] = None,
        redispatch_enabled: bool = False,
        retry_delay_seconds: float = 0.0,
    ) -> None:
        self._execute_step = execute_step
        self._routing = routing_provider
        self.store = store or ArtifactStore()
        self.resolver = resolver or ArtifactResolver(self.store)
        self.receipt_store = receipt_store
        self.redispatch_enabled = bool(redispatch_enabled)
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))

        # Runtime state (reset per run)
        self._outputs: Dict[str, Dict[str, Any]] = {}
        # An agent may drive multiple steps, so map each agent to *all* of its
        # step ids (declaration order) instead of clobbering to the last one.
        self._agent_to_steps: Dict[str, List[str]] = {}
        # Cached routing verdict per step id, filled by the pre-flight pass.
        self._routes: Dict[str, RoutingResult] = {}

    async def run(
        self,
        graph: TaskGraph,
        *,
        context: Optional[dict] = None,
        initial_completed: Optional[set[str]] = None,
        initial_outputs: Optional[Dict[str, Dict[str, Any]]] = None,
        initial_results: Optional[Dict[str, StepResult]] = None,
        on_step_start: Optional[StepHook] = None,
        on_step_end: Optional[StepHook] = None,
        commit_step_result: Optional[StepHook] = None,
    ) -> "WorkflowResult":
        """Execute ``graph`` and return a :class:`WorkflowResult`.

        The result maps ``step_id -> StepResult`` (dict-compatible) and also
        carries a workflow-level ``terminal_status``.

        ``initial_completed`` skips already-done steps on resume; the matching
        ``initial_outputs`` (``{step_id: {param: ArtifactRef}}``) re-seeds their
        produced outputs so resumed downstream steps can resolve upstream data.

        ``commit_step_result`` is the CRITICAL persistence hook (artifact payload
        + checkpoint + completion state): if it raises, the step is marked FAILED
        (never SUCCEEDED) so durable state and the reported status cannot diverge.
        ``on_step_end`` is the non-critical hook (logging/SSE) and is best-effort.
        """
        graph.validate_dag()
        smap = graph.step_map()
        context = context or {}

        self._outputs = {}
        if initial_outputs:
            for sid, outs in initial_outputs.items():
                if isinstance(outs, dict):
                    self._outputs[sid] = dict(outs)
        self._agent_to_steps = {}
        for s in graph.steps:
            key = getattr(s, "agent_name", None) or s.step_id
            self._agent_to_steps.setdefault(key, []).append(s.step_id)

        completed: set[str] = set(initial_completed or [])
        results: Dict[str, StepResult] = {
            sid: result
            for sid, result in (initial_results or {}).items()
            if sid in smap
        }
        restored_step_ids = set(results)
        attempted: set[str] = set(completed) | restored_step_ids
        additional_failures: List[Any] = []

        # A resume preflight may supply a failed result for a checkpoint that
        # claimed completion but whose protected Artifact is no longer
        # readable. Publish and persist that failure, but never execute the step
        # again (it may have performed a side effect before the corruption).
        for step_id in restored_step_ids:
            result = results[step_id]
            if result.status == StepStatus.SUCCEEDED:
                continue
            step = smap[step_id]
            if commit_step_result is not None:
                try:
                    await commit_step_result(step=step, result=result)
                except Exception:  # noqa: BLE001 - report storage failure separately
                    additional_failures.append(
                        make_failure("PERSISTENCE_FAILED", step_id=step_id)
                    )
            if on_step_end is not None:
                try:
                    await on_step_end(step=step, result=result)
                except Exception:  # noqa: BLE001 - monitoring is best effort
                    pass

        # Pre-flight routing: resolve the routing verdict for every not-yet-done
        # step BEFORE executing anything, and cache it. This makes a global
        # clarification enforceable (any CLARIFY halts the whole workflow before
        # a single side-effect step starts). A later recovery route is allowed
        # only after this pass fully dispatches, only for a retryable read-only
        # step, and never promotes runtime CLARIFY/REJECT into a global gate.
        self._routes = await self._preflight_routing(graph, context, skip=attempted)

        clarify_result = self._global_clarify(graph, results, completed=completed)
        if clarify_result is not None:
            # Clarification is a pre-execution terminal path, but every declared
            # step still receives the same durable/SSE/log lifecycle as normal
            # Scheduler outcomes.
            for step in graph.steps:
                if step.step_id in restored_step_ids or step.step_id in completed:
                    continue
                result = clarify_result[step.step_id]
                if commit_step_result is not None:
                    try:
                        await commit_step_result(step=step, result=result)
                    except Exception:  # noqa: BLE001 - critical write failed
                        additional_failures.append(
                            make_failure(
                                "PERSISTENCE_FAILED",
                                step_id=step.step_id,
                            )
                        )
                if on_step_end is not None:
                    try:
                        await on_step_end(step=step, result=result)
                    except Exception:  # noqa: BLE001 - monitoring is best effort
                        pass
            if additional_failures:
                clarify_result.terminal_status = WorkflowStatus.FAILED
                clarify_result.additional_failures = additional_failures
            clarify_result.blocked_steps = [
                sid
                for sid, result in clarify_result.items()
                if result.status == StepStatus.SKIPPED
            ]
            return clarify_result

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
                self._run_step(smap[sid], context, on_step_start,
                               on_step_end, commit_step_result)
                for sid in batch
            ]
            batch_results = await asyncio.gather(*coros)

            for sid, result in zip(batch, batch_results):
                results[sid] = result
                attempted.add(sid)
                if result.is_success:
                    completed.add(sid)
                    self._outputs[sid] = dict(result.outputs)

        # Every declared step receives an explicit terminal outcome. Steps that
        # could not enter the runnable frontier because an upstream dependency
        # failed are SKIPPED, not silently omitted from the result map.
        blocked_steps = [
            step for step in graph.steps if step.step_id not in attempted
        ]
        for step in blocked_steps:
            blocked_by = [dep for dep in step.depends_on if dep not in completed]
            error = (
                "step blocked by failed upstream dependencies: "
                + ", ".join(blocked_by)
            )
            result = StepResult(
                step_id=step.step_id,
                status=StepStatus.SKIPPED,
                error=error,
                metrics={"blocked_by": blocked_by},
                failure=make_failure(
                    "UPSTREAM_STEP_FAILED",
                    step_id=step.step_id,
                    message="Step was not executed because an upstream step failed.",
                    details_safe={"blocked_by": blocked_by},
                ),
            )
            results[step.step_id] = result
            attempted.add(step.step_id)
            if commit_step_result is not None:
                try:
                    await commit_step_result(step=step, result=result)
                except Exception:  # noqa: BLE001 - critical write failed
                    additional_failures.append(
                        make_failure(
                            "PERSISTENCE_FAILED",
                            step_id=step.step_id,
                        )
                    )
            if on_step_end is not None:
                try:
                    await on_step_end(step=step, result=result)
                except Exception:  # noqa: BLE001 - monitoring is best effort
                    pass

        return self._finalize(results, additional_failures=additional_failures)

    async def _preflight_routing(
        self, graph: TaskGraph, context: dict, *, skip: Optional[set[str]] = None
    ) -> Dict[str, RoutingResult]:
        """Route every not-yet-completed step and cache the verdict.

        A routing exception is isolated to that step (recorded as a synthetic
        ``ROUTING_ERROR`` verdict) so one bad route never aborts the whole
        pre-flight or crashes independent branches.
        """
        skip = skip or set()
        routes: Dict[str, RoutingResult] = {}
        for step in graph.steps:
            if step.step_id in skip:
                continue
            try:
                routes[step.step_id] = await self._route(step, context)
            except Exception as exc:  # noqa: BLE001 - degrade to a routing error
                routes[step.step_id] = RoutingResult(
                    selected_agent=None,
                    decision="ROUTING_ERROR",
                    reason_codes=[f"routing_error: {exc}"],
                )
        return routes

    def _global_clarify(
        self,
        graph: TaskGraph,
        results: Dict[str, StepResult],
        *,
        completed: set[str],
    ) -> Optional["WorkflowResult"]:
        """If any step needs clarification, halt the whole workflow (fail closed).

        Returns a terminal :class:`WorkflowResult` when a clarification is
        required (no step is executed), else ``None``.
        """
        clarify_ids = [
            sid
            for sid, r in self._routes.items()
            if str(getattr(r, "decision", "") or "").upper() == "CLARIFY"
        ]
        if not clarify_ids:
            return None
        clarifications: List[str] = []
        for sid in clarify_ids:
            route = self._routes[sid]
            clarification = getattr(route, "clarification", None)
            results[sid] = StepResult(
                step_id=sid,
                status=StepStatus.FAILED,
                error="clarification required before execution",
                metrics={
                    "routing_decision": "CLARIFY",
                    "clarify": True,
                    "clarification": clarification,
                    "reason_codes": list(getattr(route, "reason_codes", []) or []),
                },
            )
            results[sid] = results[sid].model_copy(
                update={
                    "failure": failure_from_step_result(
                        sid,
                        results[sid].error,
                        results[sid].metrics,
                    )
                }
            )
            if clarification:
                clarifications.append(clarification)
        blocked_steps: List[str] = []
        for step in graph.steps:
            if step.step_id in results or step.step_id in completed:
                continue
            blocked_steps.append(step.step_id)
            results[step.step_id] = StepResult(
                step_id=step.step_id,
                status=StepStatus.SKIPPED,
                error="step blocked until workflow clarification is resolved",
                metrics={"blocked_by": clarify_ids},
                failure=make_failure(
                    "CLARIFICATION_BLOCKED",
                    step_id=step.step_id,
                    message="Step was not executed because workflow clarification is required.",
                    details_safe={"blocked_by": clarify_ids},
                ),
            )
        return WorkflowResult(
            results,
            terminal_status=WorkflowStatus.CLARIFY_REQUIRED,
            clarifications=clarifications,
            blocked_steps=blocked_steps,
        )

    def _finalize(
        self,
        results: Dict[str, StepResult],
        *,
        additional_failures: Optional[List[Any]] = None,
    ) -> "WorkflowResult":
        """Derive the workflow-level terminal status from per-step outcomes."""
        rejected = [
            sid
            for sid, r in results.items()
            if str((r.metrics or {}).get("routing_decision", "")).upper()
            in ("REJECT", "NO_CAPABLE_AGENT")
        ]
        needs_recon = [
            sid for sid, r in results.items() if (r.metrics or {}).get("needs_reconciliation")
        ]
        blocked = [
            sid for sid, r in results.items() if r.status == StepStatus.SKIPPED
        ]
        failed = [sid for sid, r in results.items() if r.status !=
                  StepStatus.SUCCEEDED]
        primary_failed = [
            sid for sid, r in results.items() if r.status == StepStatus.FAILED
        ]
        succeeded = [sid for sid, r in results.items() if r.status ==
                     StepStatus.SUCCEEDED]

        if needs_recon:
            status = WorkflowStatus.NEEDS_RECONCILIATION
        elif not failed:
            status = WorkflowStatus.SUCCEEDED
        elif succeeded:
            status = WorkflowStatus.PARTIAL_FAILED
        elif rejected and set(rejected) == set(primary_failed):
            status = WorkflowStatus.REJECTED
        else:
            status = WorkflowStatus.FAILED

        return WorkflowResult(
            results,
            terminal_status=status,
            rejected_steps=rejected,
            needs_reconciliation=needs_recon,
            blocked_steps=blocked,
            additional_failures=additional_failures,
        )

    def _select_batch(self, runnable: List[str], smap: Dict[str, TaskStep]) -> List[str]:
        """Pick a concurrent batch honoring resource locks.

        - Writes/sends with disjoint locks may run together; a write with no
          declared lock takes an implicit shared lock (untagged writes never
          overlap).
        - A read that declares a ``resource_lock`` conflicting with a selected
          write in this batch is deferred (read/write conflict on the same
          resource), so a read is never run concurrently with a write it
          shares a lock with. Untagged reads are unaffected and run freely.
        """
        reads = [sid for sid in runnable if smap[sid].is_read_only]
        writes = [sid for sid in runnable if not smap[sid].is_read_only]

        selected_writes: List[str] = []
        used_locks: set[str] = set()
        for sid in writes:
            locks = set(smap[sid].resource_locks) or {_DEFAULT_WRITE_LOCK}
            if locks & used_locks:
                continue  # conflicting write/write: defer to a later round
            selected_writes.append(sid)
            used_locks |= locks

        selected_reads: List[str] = []
        for sid in reads:
            read_locks = set(smap[sid].resource_locks)
            if read_locks & used_locks:
                continue  # read/write conflict on the same resource: defer
            selected_reads.append(sid)

        return selected_reads + selected_writes

    async def _run_step(
        self,
        step: TaskStep,
        context: dict,
        on_step_start: Optional[StepHook],
        on_step_end: Optional[StepHook],
        commit_step_result: Optional[StepHook] = None,
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

        # Attach the public failure descriptor before the critical commit so
        # checkpoints, restored state, SSE, and task logs all carry the same
        # structured object.
        result = self._with_failure(step, result)

        # CRITICAL persistence FIRST (artifact payload + checkpoint + completion
        # state). A failure here must change the terminal status: a step whose
        # outputs/checkpoint could not be durably saved is NOT reported
        # SUCCEEDED (and a committed side effect needs reconciliation).
        if commit_step_result is not None:
            try:
                await commit_step_result(step=step, result=result)
            except Exception as exc:  # noqa: BLE001 - critical write failed
                metrics = dict(result.metrics or {})
                metrics["persistence_failed"] = True
                if result.is_success and not step.is_read_only:
                    metrics["needs_reconciliation"] = True
                result = StepResult(
                    step_id=step.step_id,
                    status=StepStatus.FAILED,
                    error=f"step persistence failed: {exc}",
                    outputs=result.outputs,
                    metrics=metrics,
                )

        # A commit failure replaces the original outcome, so classify that new
        # persistence/reconciliation failure as well.
        result = self._with_failure(step, result)

        # Non-critical end hook (logging / SSE / monitoring): best effort only.
        if on_step_end is not None:
            try:
                await on_step_end(step=step, result=result)
            except Exception:  # noqa: BLE001 - the end hook must not crash the batch
                pass
        return result

    def _with_failure(self, step: TaskStep, result: StepResult) -> StepResult:
        """Attach a safe descriptor to a non-success result exactly once."""

        if result.is_success or getattr(result, "failure", None) is not None:
            return result
        route = self._routes.get(step.step_id)
        failure_agent = (
            (result.metrics or {}).get("selected_agent")
            or getattr(route, "selected_agent", None)
            or getattr(step, "agent_name", None)
            or step.preferred_resource_id
        )
        return result.model_copy(
            update={
                "failure": failure_from_step_result(
                    step.step_id,
                    result.error,
                    result.metrics,
                    agent_id=failure_agent,
                )
            }
        )

    def _prepare_agent_execution(
        self,
        step: TaskStep,
        context: dict,
        selected_agent: Optional[str],
    ) -> tuple[dict, dict]:
        """Build an isolated context and resolve inputs for the actual Agent."""

        step_ctx = dict(context)
        factory = context.get("context_factory")
        if callable(factory):
            try:
                step_ctx["execution_context"] = factory(step, selected_agent)
            except Exception:  # noqa: BLE001 - context failures fail closed later
                step_ctx["execution_context"] = None
        inputs, upstream_sensitivities, upstream_refs = self._resolve_inputs(
            step,
            step_ctx,
            consumer_agent=selected_agent,
        )
        step_ctx["upstream_sensitivities"] = upstream_sensitivities
        step_ctx["upstream_artifact_refs"] = upstream_refs
        return step_ctx, inputs

    @staticmethod
    def _attempt_trace(
        failure: Any,
        *,
        attempt: int,
        phase: str,
    ) -> dict[str, Any]:
        """Return a payload-free recovery trace entry."""

        return {
            "attempt": attempt,
            "phase": phase,
            "code": str(getattr(failure, "code", "INTERNAL_STEP_ERROR")),
            "retryable": bool(getattr(failure, "retryable", False)),
        }

    def _classify_attempt_result(
        self,
        step: TaskStep,
        result: StepResult,
        *,
        selected_agent: Optional[str],
    ) -> StepResult:
        """Attach the public failure descriptor while still inside the retry loop."""

        metrics = dict(result.metrics or {})
        metrics.setdefault("selected_agent", selected_agent)
        classified = result.model_copy(update={"metrics": metrics})
        if classified.is_success or classified.failure is not None:
            return classified
        return classified.model_copy(
            update={
                "failure": failure_from_step_result(
                    step.step_id,
                    classified.error,
                    classified.metrics,
                    agent_id=selected_agent,
                )
            }
        )

    async def _execute_read_only_with_agent(
        self,
        step: TaskStep,
        step_ctx: dict,
        selected_agent: Optional[str],
        inputs: dict,
        *,
        retry_budget: int,
        phase: str,
    ) -> StepResult:
        """Execute one read-only Agent with a retryable-driven bounded budget."""

        max_attempts = 1 + min(max(int(retry_budget), 0), 1)
        attempt_failures: list[dict[str, Any]] = []
        for attempt_index in range(max_attempts):
            attempt_number = attempt_index + 1
            try:
                exec_result = await self._invoke(
                    step,
                    selected_agent,
                    inputs,
                    step_ctx,
                )
            except asyncio.TimeoutError:
                failure = make_failure(
                    "AGENT_TIMEOUT",
                    step_id=step.step_id,
                    agent_id=selected_agent,
                    details_safe={"attempts": attempt_number},
                )
                candidate = StepResult(
                    step_id=step.step_id,
                    status=StepStatus.FAILED,
                    failure=failure,
                    metrics={
                        "attempts": attempt_number,
                        "selected_agent": selected_agent,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - classify and fail closed
                if _is_dispatch_permission_error(exc):
                    failure = make_failure(
                        "AGENT_DISPATCH_DENIED",
                        step_id=step.step_id,
                        agent_id=selected_agent,
                    )
                else:
                    # An arbitrary exception may be a programming/configuration
                    # defect. Only explicit transient signals are auto-retried.
                    failure = failure_from_exception(
                        exc,
                        step_id=step.step_id,
                        agent_id=selected_agent,
                        retryable=False,
                    )
                candidate = StepResult(
                    step_id=step.step_id,
                    status=StepStatus.FAILED,
                    failure=failure,
                    metrics={
                        "attempts": attempt_number,
                        "selected_agent": selected_agent,
                        **(
                            {"failure_code": "AGENT_DISPATCH_DENIED"}
                            if _is_dispatch_permission_error(exc)
                            else {}
                        ),
                    },
                )
            else:
                if getattr(exec_result, "is_success", True):
                    candidate = self._record_success(
                        step,
                        exec_result,
                        step_ctx,
                        attempt_number,
                        selected_agent,
                    )
                    if candidate.is_success:
                        metrics = dict(candidate.metrics or {})
                        metrics.update(
                            {
                                "attempts": attempt_number,
                                "retry_count": attempt_index,
                                "selected_agent": selected_agent,
                                "attempt_failures": attempt_failures,
                                "recovery_path": (
                                    [phase, "same_agent_retry"]
                                    if attempt_number > 1
                                    else [phase]
                                ),
                            }
                        )
                        return candidate.model_copy(update={"metrics": metrics})
                else:
                    status = getattr(exec_result, "status", None)
                    metrics = {
                        "attempts": attempt_number,
                        "selected_agent": selected_agent,
                    }
                    error = getattr(exec_result, "error", None) or "step failed"
                    if status == ExecutionStatus.TIMEOUT:
                        failure = make_failure(
                            "AGENT_TIMEOUT",
                            step_id=step.step_id,
                            agent_id=selected_agent,
                            details_safe={"attempts": attempt_number},
                        )
                        candidate = StepResult(
                            step_id=step.step_id,
                            status=StepStatus.FAILED,
                            failure=failure,
                            metrics=metrics,
                        )
                    else:
                        candidate = StepResult(
                            step_id=step.step_id,
                            status=StepStatus.FAILED,
                            error=error,
                            metrics=metrics,
                        )

            candidate = self._classify_attempt_result(
                step,
                candidate,
                selected_agent=selected_agent,
            )
            attempt_failures.append(
                self._attempt_trace(
                    candidate.failure,
                    attempt=attempt_number,
                    phase=phase,
                )
            )
            metrics = dict(candidate.metrics or {})
            metrics.update(
                {
                    "attempts": attempt_number,
                    "retry_count": attempt_index,
                    "selected_agent": selected_agent,
                    "attempt_failures": list(attempt_failures),
                    "recovery_path": (
                        [phase, "same_agent_retry"]
                        if attempt_number > 1
                        else [phase]
                    ),
                }
            )
            candidate = candidate.model_copy(update={"metrics": metrics})
            if (
                not bool(getattr(candidate.failure, "retryable", False))
                or attempt_number >= max_attempts
            ):
                return candidate
            if self.retry_delay_seconds:
                await asyncio.sleep(self.retry_delay_seconds)

        raise AssertionError("read-only recovery loop exhausted unexpectedly")

    async def _route_excluding(
        self,
        step: TaskStep,
        context: dict,
        excluded_agent_ids: set[str],
    ) -> RoutingResult:
        """Ask the bound router for a new verdict after excluding failed Agents."""

        if self._routing is None:
            return RoutingResult(
                selected_agent=None,
                decision="NO_CAPABLE_AGENT",
                reason_codes=["REDISPATCH_ROUTER_UNAVAILABLE"],
            )
        authorized = set(context.get("authorized_agent_ids") or set())
        authorized.difference_update(excluded_agent_ids)
        return await self._routing.decide(
            step,
            user_query=context.get("user_query", ""),
            task_id=context.get("task_id", ""),
            workflow_id=context.get("workflow_id", ""),
            agents=context.get("agents", ()),
            authorized_agent_ids=authorized,
            metadata=context.get("metadata"),
        )

    def _redispatch_contract_outcome(
        self,
        step: TaskStep,
        context: dict,
        selected_agent: str,
    ) -> Optional[str]:
        """Validate a recovery candidate before sending it any step inputs."""

        planned_contract = getattr(step, "agent_contract", None)
        if planned_contract is None:
            return "REDISPATCH_PLANNED_CONTRACT_MISSING"
        candidate_contract = self._trusted_agent_contract(selected_agent, context)
        if candidate_contract is None:
            return "REROUTED_AGENT_CONTRACT_MISSING"

        expected_outputs = list(getattr(step, "expected_outputs", None) or [])
        if not expected_outputs:
            return "REDISPATCH_EXPECTED_OUTPUTS_MISSING"
        planned_outputs = {
            ref.name: ref.schema_ref for ref in planned_contract.produces
        }
        candidate_outputs = {
            ref.name: ref.schema_ref for ref in candidate_contract.produces
        }
        for name in expected_outputs:
            if name not in candidate_outputs:
                return "REDISPATCH_OUTPUT_MISSING"
            planned_schema = planned_outputs.get(name)
            if planned_schema and candidate_outputs[name] != planned_schema:
                return "REDISPATCH_OUTPUT_SCHEMA_MISMATCH"
        return None

    @staticmethod
    def _with_redispatch_outcome(
        result: StepResult,
        *,
        outcome: str,
    ) -> StepResult:
        metrics = dict(result.metrics or {})
        metrics["redispatch_outcome"] = outcome
        return result.model_copy(update={"metrics": metrics})

    async def _maybe_redispatch_read_only(
        self,
        step: TaskStep,
        context: dict,
        selected_agent: Optional[str],
        primary: StepResult,
    ) -> StepResult:
        """Run at most one equivalent Agent after a retryable primary failure."""

        failure = getattr(primary, "failure", None)
        if (
            not self.redispatch_enabled
            or self._routing is None
            or not step.is_read_only
            or not selected_agent
            or failure is None
            or not failure.retryable
            or bool((primary.metrics or {}).get("needs_reconciliation"))
        ):
            return primary

        if getattr(step, "agent_contract", None) is None:
            return self._with_redispatch_outcome(
                primary,
                outcome="REDISPATCH_PLANNED_CONTRACT_MISSING",
            )
        authorized = set(context.get("authorized_agent_ids") or set())
        authorized.discard(selected_agent)
        try:
            routing = await self._route_excluding(step, context, {selected_agent})
        except Exception:  # noqa: BLE001 - recovery routing never crashes the DAG
            return self._with_redispatch_outcome(
                primary,
                outcome="ROUTING_FAILED",
            )
        decision = str(getattr(routing, "decision", "") or "").upper()
        rerouted_agent = getattr(routing, "selected_agent", None)
        if decision != "DISPATCH":
            return self._with_redispatch_outcome(
                primary,
                outcome=decision or "ROUTING_FAILED",
            )
        if not rerouted_agent:
            return self._with_redispatch_outcome(
                primary,
                outcome="DISPATCH_NO_AGENT",
            )
        if rerouted_agent == selected_agent:
            return self._with_redispatch_outcome(
                primary,
                outcome="REDISPATCH_RESELECTED_EXCLUDED_AGENT",
            )
        if rerouted_agent not in authorized:
            return self._with_redispatch_outcome(
                primary,
                outcome="REDISPATCH_AGENT_UNAUTHORIZED",
            )

        contract_outcome = self._redispatch_contract_outcome(
            step,
            context,
            rerouted_agent,
        )
        if contract_outcome is not None:
            return self._with_redispatch_outcome(
                primary,
                outcome=contract_outcome,
            )
        try:
            rerouted_ctx, rerouted_inputs = self._prepare_agent_execution(
                step,
                context,
                rerouted_agent,
            )
        except InputResolutionError:
            return self._with_redispatch_outcome(
                primary,
                outcome="REDISPATCH_INPUT_RESOLUTION_FAILED",
            )

        rerouted = await self._execute_read_only_with_agent(
            step,
            rerouted_ctx,
            rerouted_agent,
            rerouted_inputs,
            retry_budget=0,
            phase="redispatch",
        )
        primary_metrics = dict(primary.metrics or {})
        rerouted_metrics = dict(rerouted.metrics or {})
        primary_attempts = int(primary_metrics.get("attempts") or 0)
        combined_failures = list(primary_metrics.get("attempt_failures") or [])
        for item in rerouted_metrics.get("attempt_failures") or []:
            entry = dict(item)
            entry["attempt"] = primary_attempts + int(entry.get("attempt") or 0)
            combined_failures.append(entry)
        merged_metrics = dict(rerouted_metrics)
        merged_metrics.update(
            {
                "attempts": primary_attempts
                + int(rerouted_metrics.get("attempts") or 0),
                "retry_count": int(primary_metrics.get("retry_count") or 0),
                "redispatch_count": 1,
                "redispatched_from": selected_agent,
                "redispatched_to": rerouted_agent,
                "redispatch_outcome": (
                    "SUCCEEDED"
                    if rerouted.is_success
                    else str(getattr(rerouted.failure, "code", "FAILED"))
                ),
                "attempt_failures": combined_failures,
                "recovery_path": [
                    *(primary_metrics.get("recovery_path") or ["primary"]),
                    "redispatch",
                ],
                "selected_agent": rerouted_agent,
            }
        )
        return rerouted.model_copy(update={"metrics": merged_metrics})

    async def _execute_step_core(
        self,
        step: TaskStep,
        context: dict,
        on_step_start: Optional[StepHook],
    ) -> StepResult:
        # Reuse the pre-flight routing verdict; only route here if a step was
        # somehow not covered by the pre-flight pass (defensive).
        routing = self._routes.get(step.step_id)
        if routing is None:
            routing = await self._route(step, context)
        decision_kind = str(getattr(routing, "decision",
                            "DISPATCH") or "DISPATCH").upper()
        selected_agent = getattr(routing, "selected_agent", None)
        reason_codes = list(getattr(routing, "reason_codes", []) or [])

        # A routing exception during pre-flight is surfaced as a failed step,
        # isolating it from independent branches (never crashes the DAG).
        if decision_kind == "ROUTING_ERROR":
            detail = "; ".join(reason_codes) or "routing error"
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error=f"step crashed: {detail}",
                metrics={"crashed": True, "routing_decision": "ROUTING_ERROR"},
            )

        # Honor the routing verdict. A rejection or clarification is terminal for
        # this step and must NEVER fall back to a preferred agent.
        if decision_kind in ("REJECT", "NO_CAPABLE_AGENT"):
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error=f"routing {decision_kind}: {', '.join(reason_codes) or 'no capable agent'}",
                metrics={"routing_decision": decision_kind,
                         "reason_codes": reason_codes},
            )
        if decision_kind == "CLARIFY":
            # Defensive: a global clarification halts the workflow before any
            # step runs, so this should be unreachable during execution.
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error="clarification required before execution",
                metrics={
                    "routing_decision": "CLARIFY",
                    "clarify": True,
                    "clarification": getattr(routing, "clarification", None),
                    "reason_codes": reason_codes,
                },
            )
        # An explicit DISPATCH with no concrete agent is an illegal routing
        # result: fail closed BEFORE the start hook / executor run. Only applies
        # when a routing provider is bound (direct/unit scheduling with no
        # provider legitimately passes ``selected_agent=None`` to the executor).
        if decision_kind == "DISPATCH" and selected_agent is None and self._routing is not None:
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error="illegal routing: DISPATCH without a selected agent",
                metrics={"routing_decision": "DISPATCH_NO_AGENT",
                         "reason_codes": reason_codes},
            )

        try:
            step_ctx, inputs = self._prepare_agent_execution(
                step,
                context,
                selected_agent,
            )
        except InputResolutionError as exc:
            # A declared upstream dependency could not be satisfied. Fail closed
            # rather than run the agent with a missing/empty input.
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error=str(exc),
                metrics={
                    "input_error": exc.reason,
                    "param": exc.param,
                    "source": exc.source,
                    "selected_agent": selected_agent,
                },
            )

        # Idempotency + crash-safety for side-effect steps: a single ATOMIC
        # claim replaces the old separate get()+put(STARTED) so two instances
        # can never both execute the same side effect.
        idem_key: Optional[str] = None
        claim_id: Optional[str] = None
        if self.receipt_store is not None and not step.is_read_only:
            idem_key = idempotency_key(step_ctx.get(
                "task_id", ""), step.step_id, inputs)
            # Surface the key everywhere a downstream provider/tool can dedupe:
            # step context, ExecutionContext.metadata (-> RemoteExecutor request
            # context / security_context / agent prompt).
            step_ctx["idempotency_key"] = idem_key
            exec_ctx = step_ctx.get("execution_context")
            meta = getattr(exec_ctx, "metadata", None)
            if isinstance(meta, dict):
                meta["idempotency_key"] = idem_key
            claim = self.receipt_store.claim_if_absent(
                idem_key,
                {
                    "idempotency_key": idem_key,
                    "task_id": step_ctx.get("task_id", ""),
                    "step_id": step.step_id,
                    "agent": selected_agent,
                    "status": "STARTED",
                    "normalized_input": normalize_input(inputs),
                    "external_op_id": None,
                    "timestamp": time.time(),
                },
            )
            if claim.status == ClaimStatus.SUCCEEDED:
                # Confirmed prior success: never re-run the side effect.
                prior = claim.receipt or {}
                return StepResult(
                    step_id=step.step_id,
                    status=StepStatus.SUCCEEDED,
                    outputs=prior.get("outputs") or {},
                    metrics={"idempotent_reuse": True,
                             "selected_agent": selected_agent,
                             "idempotency_key": idem_key,
                             "receipt_status": "SUCCEEDED",
                             "external_op_id": prior.get("external_op_id")},
                )
            if claim.status == ClaimStatus.IN_PROGRESS:
                # Another instance already claimed/started this side effect, or a
                # prior run began it but its outcome is unconfirmed. Do NOT run --
                # require reconciliation (never auto re-send).
                prior = claim.receipt or {}
                return self._needs_reconciliation(
                    step,
                    selected_agent,
                    "needs reconciliation: side-effect claimed by another instance or prior outcome unconfirmed",
                    external_op_id=prior.get("external_op_id"),
                )
            if claim.status == ClaimStatus.CORRUPT:
                # Fail closed: the receipt store is unparseable so we cannot know
                # whether the side effect already happened.
                return StepResult(
                    step_id=step.step_id,
                    status=StepStatus.FAILED,
                    error="receipt store corrupt: refusing to run side effect (fail closed)",
                    metrics={"receipt_store_corrupt": True,
                             "selected_agent": selected_agent},
                )
            claim_id = claim.claim_id

        if on_step_start is not None:
            await on_step_start(step=step, selected_agent=selected_agent, inputs=inputs)

        if idem_key is not None:
            # Claimed side effect: execute AT MOST ONCE. Any unconfirmed or
            # failed outcome keeps the STARTED receipt and needs reconciliation
            # (never an auto retry / re-send).
            try:
                exec_result = await self._invoke(step, selected_agent, inputs, step_ctx)
            except asyncio.TimeoutError:
                return self._needs_reconciliation(
                    step, selected_agent, f"timeout after {step.timeout}s")
            except Exception as exc:  # noqa: BLE001 - indeterminate outcome
                if _is_dispatch_permission_error(exc):
                    return StepResult(
                        step_id=step.step_id,
                        status=StepStatus.FAILED,
                        error="agent dispatch denied by policy",
                        metrics={
                            "failure_code": "AGENT_DISPATCH_DENIED",
                            "selected_agent": selected_agent,
                        },
                    )
                return self._needs_reconciliation(
                    step, selected_agent, f"side-effect executor error: {exc}")
            if not getattr(exec_result, "is_success", True):
                return self._needs_reconciliation(
                    step,
                    selected_agent,
                    getattr(exec_result, "error",
                            None) or "side-effect step failed",
                )
            result: Optional[StepResult] = self._record_success(
                step, exec_result, step_ctx, 1, selected_agent)
            result.metrics["idempotency_key"] = idem_key
            if not result.is_success:
                # The external side effect may already have happened, but its
                # business result could not be normalized/validated. Keep the
                # STARTED receipt and require reconciliation; never persist a
                # contradictory SUCCEEDED receipt with a FAILED step.
                reconciled = self._needs_reconciliation(
                    step,
                    selected_agent,
                    result.error or "side-effect result validation failed",
                    external_op_id=_external_operation_id(exec_result, None),
                )
                reconciled.metrics.update(result.metrics)
                reconciled.metrics["needs_reconciliation"] = True
                return reconciled
        else:
            if step.is_read_only:
                result = await self._execute_read_only_with_agent(
                    step,
                    step_ctx,
                    selected_agent,
                    inputs,
                    retry_budget=step.retry,
                    phase="primary",
                )
                if not result.is_success:
                    return await self._maybe_redispatch_read_only(
                        step,
                        context,
                        selected_agent,
                        result,
                    )
            else:
                # A non-read step without a receipt store still executes at
                # most once. It is deliberately outside automatic recovery.
                try:
                    exec_result = await self._invoke(
                        step,
                        selected_agent,
                        inputs,
                        step_ctx,
                    )
                except asyncio.TimeoutError:
                    return StepResult(
                        step_id=step.step_id,
                        status=StepStatus.FAILED,
                        error=f"timeout after {step.timeout}s",
                        metrics={
                            "attempts": 1,
                            "selected_agent": selected_agent,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    if _is_dispatch_permission_error(exc):
                        return StepResult(
                            step_id=step.step_id,
                            status=StepStatus.FAILED,
                            error="agent dispatch denied by policy",
                            metrics={
                                "attempts": 1,
                                "failure_code": "AGENT_DISPATCH_DENIED",
                                "selected_agent": selected_agent,
                            },
                        )
                    return StepResult(
                        step_id=step.step_id,
                        status=StepStatus.FAILED,
                        error=str(exc),
                        metrics={
                            "attempts": 1,
                            "selected_agent": selected_agent,
                        },
                    )
                if not getattr(exec_result, "is_success", True):
                    return StepResult(
                        step_id=step.step_id,
                        status=StepStatus.FAILED,
                        error=getattr(exec_result, "error", None) or "step failed",
                        metrics={
                            "attempts": 1,
                            "selected_agent": selected_agent,
                        },
                    )
                result = self._record_success(
                    step,
                    exec_result,
                    step_ctx,
                    1,
                    selected_agent,
                )
                if not result.is_success:
                    return result

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
        # Record the confirmed SUCCEEDED receipt via the owning claim. If
        # persistence fails the side effect already happened but cannot be
        # recorded -> do NOT mark success; require reconciliation (never risk a
        # re-send on resume).
        if idem_key is not None:
            try:
                self.receipt_store.complete(
                    idem_key,
                    claim_id,
                    {
                        "idempotency_key": idem_key,
                        "task_id": step_ctx.get("task_id", ""),
                        "step_id": step.step_id,
                        "agent": selected_agent,
                        "status": "SUCCEEDED",
                        "normalized_input": normalize_input(inputs),
                        "external_op_id": (result.metrics or {}).get("external_op_id"),
                        "outputs": result.outputs,
                        "timestamp": time.time(),
                    },
                )
                result.metrics["receipt_status"] = "SUCCEEDED"
            except Exception as exc:  # noqa: BLE001 - side effect done, receipt lost
                return self._needs_reconciliation(
                    step,
                    selected_agent,
                    f"side effect succeeded but receipt persistence failed: {exc}",
                    external_op_id=(result.metrics or {}).get(
                        "external_op_id"),
                )
        return result

    def _needs_reconciliation(
        self,
        step: TaskStep,
        selected_agent: Optional[str],
        error: str,
        *,
        external_op_id: Optional[str] = None,
    ) -> StepResult:
        """A side effect with an unconfirmed outcome: FAILED + needs_reconciliation.

        The STARTED receipt is intentionally retained so a later run reconciles
        (verifies the external op) rather than blindly re-running the side effect.
        """
        return StepResult(
            step_id=step.step_id,
            status=StepStatus.FAILED,
            error=error,
            metrics={
                "needs_reconciliation": True,
                "selected_agent": selected_agent,
                "external_op_id": external_op_id,
            },
        )

    async def _invoke(
        self, step: TaskStep, selected_agent: Optional[str], inputs: dict, context: dict
    ) -> Any:
        coro = self._execute_step(
            step=step, selected_agent=selected_agent, inputs=inputs, context=context
        )
        if step.timeout:
            return await asyncio.wait_for(coro, step.timeout)
        return await coro

    def _trusted_agent_contract(self, agent_name: str, context: dict) -> Any:
        """Trusted registry contract for a dynamically routed Agent (or None).

        ``context["agents"]`` is the runtime's registry listing (the same
        trusted source routing selects from), never planner output.
        """
        for agent in (context or {}).get("agents") or []:
            if str(getattr(agent, "agent_name", "") or "") == str(agent_name):
                return getattr(agent, "agent_contract", None)
        return None

    def _record_success(
        self,
        step: TaskStep,
        exec_result: Any,
        context: dict,
        attempts: int,
        selected_agent: Optional[str] = None,
    ) -> StepResult:
        # The result is attributed to and validated against the Agent that
        # ACTUALLY ran. When routing picked a different Agent than the plan,
        # binding the planned Agent's identity/contract would either reject a
        # legitimate envelope (PRODUCER_AGENT_MISMATCH) or misattribute a
        # legacy payload to the wrong producer.
        planned_agent = (
            getattr(step, "agent_name", None)
            or getattr(step, "preferred_resource_id", None)
        )
        producer_agent = selected_agent or planned_agent
        contract = getattr(step, "agent_contract", None)
        if selected_agent and planned_agent and selected_agent != planned_agent:
            contract = self._trusted_agent_contract(selected_agent, context)
            if contract is None and getattr(step, "agent_contract", None) is not None:
                # The planned step is contracted but the rerouted Agent has no
                # trusted contract: the result cannot be validated -> refuse.
                return StepResult(
                    step_id=step.step_id,
                    status=StepStatus.FAILED,
                    error=(
                        f"no trusted contract for rerouted agent "
                        f"{selected_agent!r}: refusing to publish an "
                        "unvalidated result"
                    ),
                    metrics={
                        "attempts": attempts,
                        "result_error": "REROUTED_AGENT_CONTRACT_MISSING",
                        "selected_agent": selected_agent,
                    },
                )
        try:
            normalized = normalize_agent_result(
                exec_result,
                agent_contract=contract,
                expected_outputs=list(step.expected_outputs),
                producer_agent=producer_agent,
            )
            artifacts = to_artifacts(
                normalized,
                exec_result,
                step=step,
                context=context.get("execution_context"),
                upstream_sensitivities=context.get("upstream_sensitivities"),
            )
        except AgentResultNormalizationError as exc:
            # Remote business diagnostics (remote_code / remote_details) are
            # deliberately excluded from SSE, checkpoints and the TaskLogger.
            # The server log is their only retention point, so record them
            # here before they are filtered out.
            logger.warning(
                "step %s: agent result rejected (%s): %s; details=%r",
                step.step_id,
                exc.code,
                exc,
                exc.details,
            )
            return StepResult(
                step_id=step.step_id,
                status=StepStatus.FAILED,
                error=str(exc),
                metrics={
                    "attempts": attempts,
                    "result_error": exc.code,
                    "result_error_details": exc.details,
                    "result_retryable": exc.retryable,
                },
            )

        upstream_refs = list(context.get("upstream_artifact_refs") or [])
        outputs: Dict[str, Any] = {}
        stored_artifacts: list[Any] = []
        for logical_name, artifact in artifacts.items():
            if upstream_refs:
                lineage = list(artifact.derived_from or [])
                seen = {
                    (ref.artifact_id, ref.version, ref.selector)
                    for ref in lineage
                }
                for ref in upstream_refs:
                    key = (ref.artifact_id, ref.version, ref.selector)
                    if key not in seen:
                        lineage.append(ref)
                        seen.add(key)
                artifact = artifact.model_copy(update={"derived_from": lineage})
            # Contracted output validation is a publication boundary. An
            # invalid typed result must never be registered for downstream use.
            if contract is not None and artifact.schema_valid is not True:
                return StepResult(
                    step_id=step.step_id,
                    status=StepStatus.FAILED,
                    error=f"output {logical_name!r} failed schema validation",
                    metrics={
                        "attempts": attempts,
                        "result_error": "SCHEMA_VALIDATION_FAILED",
                        "logical_name": logical_name,
                        "schema_ref": artifact.schema_ref,
                    },
                )
            outputs[logical_name] = self.store.put(artifact)
            stored_artifacts.append(artifact)

        metrics: Dict[str, Any] = {"attempts": attempts}
        # Carry the external operation id (e.g. provider message id) so the
        # receipt records a verifiable side-effect identifier.
        primary_artifact = stored_artifacts[0] if stored_artifacts else None
        external_op_id = _external_operation_id(exec_result, primary_artifact)
        if external_op_id is not None:
            metrics["external_op_id"] = external_op_id
        return StepResult(
            step_id=step.step_id,
            status=StepStatus.SUCCEEDED,
            outputs=outputs,
            metrics=metrics,
        )

    async def _route(self, step: TaskStep, context: dict) -> RoutingResult:
        """Return the routing verdict for ``step`` (never falls back silently).

        When no routing provider is bound (unit tests / direct scheduling), the
        preferred resource id is treated as an explicit DISPATCH.
        """
        if self._routing is None:
            return RoutingResult(selected_agent=step.preferred_resource_id, decision="DISPATCH")
        result = await self._routing.decide(
            step,
            user_query=context.get("user_query", ""),
            task_id=context.get("task_id", ""),
            workflow_id=context.get("workflow_id", ""),
            agents=context.get("agents", ()),
            authorized_agent_ids=context.get("authorized_agent_ids", set()),
            metadata=context.get("metadata"),
        )
        return result

    def _find_source_step(self, source: Optional[str]) -> Optional[str]:
        """Resolve a step id or agent name to a concrete producer step id.

        A given agent may drive several steps, so bind to the most recent one
        that has already produced outputs. Returns ``None`` when no step for the
        agent has produced output yet (the caller fails closed rather than
        falling back to an arbitrary declared step).
        """
        if not source:
            return None
        # Planner bindings call this field ``source_step`` and may correctly
        # carry the producer's step_id. Prefer that exact identity before the
        # backward-compatible agent-name lookup.
        if source in self._outputs:
            return source
        step_ids = self._agent_to_steps.get(source)
        if not step_ids:
            return None
        for sid in reversed(step_ids):
            if sid in self._outputs:
                return sid
        return None

    def _resolve_ref(
        self, *, ref: Any, subject: Any, scenario: Any, consumer_agent: Optional[str]
    ) -> tuple[Any, Any]:
        """Resolve a concrete :class:`ArtifactRef` to ``(value, sensitivity)``.

        The read is subject to the resolver's access + schema guards; the paired
        artifact's ``sensitivity`` is returned so the caller can propagate the
        upstream sensitivity onto the consuming step's captured output.
        """
        value = self.resolver.resolve(
            ref, subject=subject, scenario=scenario, consumer_agent=consumer_agent
        )
        artifact = self.store.get(ref)
        return value, getattr(artifact, "sensitivity", None)

    def _resolve_ref_checked(
        self,
        *,
        param: str,
        ref: Any,
        source: str,
        subject: Any,
        scenario: Any,
        consumer_agent: Optional[str],
    ) -> tuple[Any, Any]:
        """``_resolve_ref`` mapping every failure to a classified, fail-closed
        :class:`InputResolutionError` (used for non-optional inputs)."""
        try:
            return self._resolve_ref(
                ref=ref, subject=subject, scenario=scenario, consumer_agent=consumer_agent
            )
        except ArtifactAccessDenied as exc:
            raise InputResolutionError(
                param=param, source=source, reason="access_denied", detail=str(exc)
            ) from exc
        except ArtifactSchemaInvalid as exc:
            raise InputResolutionError(
                param=param, source=source, reason="schema_invalid", detail=str(exc)
            ) from exc
        except ArtifactSchemaIncompatible as exc:
            raise InputResolutionError(
                param=param, source=source, reason="schema_incompatible", detail=str(exc)
            ) from exc
        except ArtifactNotFoundError as exc:
            raise InputResolutionError(
                param=param, source=source, reason="artifact_not_found", detail=str(exc)
            ) from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise InputResolutionError(
                param=param, source=source, reason="selector_error", detail=str(exc)
            ) from exc

    def _require_contract_inputs(
        self,
        step: TaskStep,
        resolved: Dict[str, Any],
        *,
        context: dict,
        consumer_agent: Optional[str] = None,
    ) -> None:
        """Every ``required`` Agent-contract input must be resolved before the
        Agent runs. A plan that simply omits the binding must fail closed here,
        never silently degrade to LLM parameter extraction inside the Agent.

        Symmetric with the result-side contract binding: when routing selected
        a different Agent than the plan, the requirement set comes from the
        ACTUAL Agent's trusted contract, not the planned one. A rerouted Agent
        without a trusted contract is not checked here -- the result side
        already fails closed (REROUTED_AGENT_CONTRACT_MISSING) before anything
        is published."""
        contract = getattr(step, "agent_contract", None)
        planned_agent = (
            getattr(step, "agent_name", None)
            or getattr(step, "preferred_resource_id", None)
        )
        if consumer_agent and planned_agent and consumer_agent != planned_agent:
            contract = self._trusted_agent_contract(consumer_agent, context)
        if contract is None:
            return
        missing = [
            ref.name
            for ref in contract.requires
            if ref.required and ref.name not in resolved
        ]
        if missing:
            raise InputResolutionError(
                param=missing[0],
                source=None,
                reason="required_contract_input_missing",
                detail=f"contract requires unresolved inputs: {missing}",
            )

    def _resolve_inputs(
        self, step: TaskStep, context: dict, *, consumer_agent: Optional[str] = None
    ) -> tuple[Dict[str, Any], list, list]:
        """Resolve a step's declared inputs to concrete values (fail closed).

        Two input forms are resolved, in order:

        1. Author-declared ``required_inputs`` (``{param: ArtifactRef}``): a
           concrete ref resolved through the access/schema guards. These are
           always required -- an unresolvable ref fails closed.
        2. Planner-derived symbolic ``input_bindings`` (``source_step`` /
           ``source_output``): resolved to the producing step's captured output.

        A parameter declared by BOTH forms makes the graph illegal (never a
        silent override): the step fails closed with ``duplicate_param``. Only
        bindings explicitly marked ``optional`` are skipped on failure.
        """
        resolved: Dict[str, Any] = {}
        upstream_sensitivities: list = []
        upstream_refs: list = []
        subject = context.get("subject")
        scenario = context.get("scenario")

        # 1) Author-declared concrete required inputs.
        required = getattr(step, "required_inputs", None) or {}
        required_params: set[str] = set()
        if isinstance(required, dict):
            for param, ref in required.items():
                if ref is None:
                    continue
                required_params.add(param)
                value, sensitivity = self._resolve_ref_checked(
                    param=param,
                    ref=ref,
                    source="required_inputs",
                    subject=subject,
                    scenario=scenario,
                    consumer_agent=consumer_agent,
                )
                resolved[param] = value
                upstream_sensitivities.append(sensitivity)
                upstream_refs.append(ref)

        # 2) Planner-derived symbolic input bindings.
        bindings = getattr(step, "input_bindings", None)
        if not isinstance(bindings, list):
            self._require_contract_inputs(
                step, resolved, context=context, consumer_agent=consumer_agent
            )
            return resolved, upstream_sensitivities, upstream_refs
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            param = binding.get("parameter_name")
            src_agent = binding.get("source_step")
            src_output = binding.get("source_output")
            source_artifacts = binding.get("source_artifacts")
            optional = bool(binding.get("optional", False))
            if source_artifacts is not None and not isinstance(source_artifacts, list):
                raise InputResolutionError(
                    param=str(param or ""),
                    source=None,
                    reason="invalid_fan_in",
                    detail="source_artifacts must be a list",
                )
            if isinstance(source_artifacts, list):
                if not param:
                    raise InputResolutionError(
                        param="",
                        source=None,
                        reason="invalid_fan_in",
                        detail="fan-in binding requires parameter_name",
                    )
                if src_agent or src_output:
                    raise InputResolutionError(
                        param=param,
                        source=None,
                        reason="invalid_fan_in",
                        detail="single-source and source_artifacts forms cannot be mixed",
                    )
                if param in required_params or param in resolved:
                    raise InputResolutionError(
                        param=param,
                        source=None,
                        reason="duplicate_param",
                    )
                contract_data = getattr(step, "agent_contract", None)
                contract_requires = (
                    list(contract_data.requires)
                    if contract_data is not None
                    else []
                )
                contract_input = next(
                    (ref for ref in contract_requires if ref.name == param),
                    None,
                )
                binding_optional = optional and (
                    contract_input is None or not contract_input.required
                )
                if not source_artifacts:
                    if binding_optional:
                        continue
                    raise InputResolutionError(
                        param=param,
                        source=None,
                        reason="invalid_fan_in",
                        detail="source_artifacts must not be empty",
                    )
                assembled_sources: list[dict[str, Any]] = []
                binding_refs: list[Any] = []
                binding_sensitivities: list[Any] = []
                optional_source_missing = False
                for source_binding in source_artifacts:
                    if not isinstance(source_binding, dict):
                        raise InputResolutionError(
                            param=param,
                            source=None,
                            reason="invalid_fan_in",
                            detail="source_artifacts entries must be objects",
                        )
                    source_step = source_binding.get("source_step")
                    source_output = source_binding.get("source_output")
                    source_step_id = self._find_source_step(source_step)
                    ref = (
                        self._outputs.get(source_step_id, {}).get(source_output)
                        if source_step_id and source_output
                        else None
                    )
                    if ref is None:
                        if binding_optional:
                            optional_source_missing = True
                            break
                        raise InputResolutionError(
                            param=param,
                            source=source_step,
                            reason="artifact_not_produced",
                        )
                    value, sensitivity = self._resolve_ref_checked(
                        param=param,
                        ref=ref,
                        source=str(source_step),
                        subject=subject,
                        scenario=scenario,
                        consumer_agent=consumer_agent,
                    )
                    artifact = self.store.get(ref)
                    assembled_sources.append(
                        {
                            "logical_name": artifact.logical_name,
                            "schema_ref": artifact.schema_ref,
                            "payload": value,
                        }
                    )
                    binding_refs.append(ref)
                    binding_sensitivities.append(sensitivity)
                if optional_source_missing:
                    continue
                assembly = binding.get("assembly") or {}
                assembled = {
                    "sources": assembled_sources,
                    "title": str(
                        assembly.get("title")
                        or getattr(step, "title", "")
                        or "综合汇总"
                    ),
                    "instruction": str(
                        assembly.get("instruction")
                        or getattr(step, "description", "")
                        or "使用全部上游来源完成当前步骤"
                    ),
                }
                contract_data = getattr(step, "agent_contract", None) or {}
                contract_inputs = (
                    contract_data.get("input_schema_refs", {})
                    if isinstance(contract_data, dict)
                    else getattr(contract_data, "input_schema_refs", {})
                )
                expected_schema = contract_inputs.get(param)
                assembly_schema = assembly.get("schema_ref")
                if (
                    expected_schema
                    and assembly_schema
                    and expected_schema != assembly_schema
                ):
                    raise InputResolutionError(
                        param=param,
                        source=None,
                        reason="schema_incompatible",
                        detail=(
                            f"assembly schema {assembly_schema!r} does not match "
                            f"contract schema {expected_schema!r}"
                        ),
                    )
                target_schema = expected_schema or assembly_schema
                if target_schema:
                    registry = get_schema_registry()
                    if not registry.has(target_schema):
                        # Only fill missing built-in schemas: callers may have
                        # registered a stricter schema under the same versioned
                        # reference, which must never be silently replaced.
                        from src.manager.executor.agent_result_adapter import (
                            _register_missing_agent_schemas,
                        )

                        _register_missing_agent_schemas(registry)
                    valid, errors = registry.validate(assembled, target_schema)
                    if not valid:
                        raise InputResolutionError(
                            param=param,
                            source=None,
                            reason="schema_invalid",
                            detail="; ".join(errors),
                        )
                resolved[param] = assembled
                upstream_refs.extend(binding_refs)
                upstream_sensitivities.extend(binding_sensitivities)
                continue
            # A binding without a param or source is not an upstream artifact
            # dependency (the agent resolves it from context), so it is skipped.
            if not param or not src_agent:
                continue
            # A param declared by both required_inputs and a binding is an
            # illegal graph: fail closed rather than silently override.
            if param in required_params:
                raise InputResolutionError(
                    param=param,
                    source=src_agent,
                    reason="duplicate_param",
                    detail="declared by both required_inputs and input_bindings",
                )
            src_step_id = self._find_source_step(src_agent)
            # ``source_step`` must resolve to a concrete producer step id.
            if src_step_id is None:
                if optional:
                    continue
                raise InputResolutionError(
                    param=param, source=src_agent, reason="artifact_not_produced"
                )
            outputs = self._outputs.get(src_step_id, {})
            # ``source_output`` must exist EXACTLY -- never fall back to the
            # first available output (which could leak the wrong artifact).
            if src_output:
                ref = outputs.get(src_output)
            elif len(outputs) == 1:
                # No explicit output name and the producer has a single output:
                # unambiguous, so bind to it.
                ref = next(iter(outputs.values()))
            else:
                ref = None
            if ref is None:
                if optional:
                    continue
                raise InputResolutionError(
                    param=param, source=src_agent, reason="artifact_not_produced"
                )
            try:
                value, sensitivity = self._resolve_ref(
                    ref=ref, subject=subject, scenario=scenario, consumer_agent=consumer_agent
                )
                resolved[param] = value
                upstream_sensitivities.append(sensitivity)
                upstream_refs.append(ref)
            except ArtifactAccessDenied as exc:
                if optional:
                    continue
                raise InputResolutionError(
                    param=param, source=src_agent, reason="access_denied", detail=str(exc)
                ) from exc
            except ArtifactSchemaInvalid as exc:
                if optional:
                    continue
                raise InputResolutionError(
                    param=param, source=src_agent, reason="schema_invalid", detail=str(exc)
                ) from exc
            except ArtifactSchemaIncompatible as exc:
                if optional:
                    continue
                raise InputResolutionError(
                    param=param, source=src_agent, reason="schema_incompatible", detail=str(exc)
                ) from exc
            except ArtifactNotFoundError as exc:
                if optional:
                    continue
                raise InputResolutionError(
                    param=param, source=src_agent, reason="artifact_not_found", detail=str(exc)
                ) from exc
            except (KeyError, IndexError, TypeError) as exc:
                if optional:
                    continue
                raise InputResolutionError(
                    param=param, source=src_agent, reason="selector_error", detail=str(exc)
                ) from exc
        self._require_contract_inputs(
            step, resolved, context=context, consumer_agent=consumer_agent
        )
        return resolved, upstream_sensitivities, upstream_refs
