"""Scheduler <-> workflow runtime bridge (Plan §8, Phase 3d — R1).

Adapts the :class:`TaskScheduler` to the existing ``_process_workflow`` runtime:
it produces the same SSE event shapes (``start_of_workflow`` / ``start_of_agent``
/ ``end_of_agent`` / ``end_of_workflow``), saves checkpoints, drives task logging
and hooks, and carries ``memory_session_id`` / ``memory_context`` (added on main).

Design notes
------------
- ``execute_step`` and ``routing_provider`` are **injectable** so this module is
  unit-testable with fakes; the real agent execution + routing are used only when
  they are not supplied.
- Heavy imports (agent_manager, executor factory, security, S-ABAC) are performed
  lazily inside the real ``execute_step`` so importing this module stays light.
- Concurrency: step lifecycle events are funneled through a single
  ``asyncio.Queue`` and drained in order by the async generator, mirroring
  ``process._execute_node_with_runtime_events``.
- DAG checkpoints record the set of completed ``step_id`` plus captured artifacts
  in ``state`` (not a linear step index).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

from src.interface.artifact import ArtifactRef, StepStatus
from src.interface.task_graph import TaskGraph, WorkflowStatus
from src.orchestration.artifact_guard import PolicyEngineArtifactGuard
from src.orchestration.artifact_payload_store import (
    ArtifactPayloadCorruption,
    ArtifactPayloadStore,
)
from src.orchestration.completion import PersistentReceiptStore
from src.orchestration.plan_to_task_graph import plan_to_task_graph
from src.orchestration.providers import MainAgentRoutingProvider, RoutingProvider
from src.orchestration.resolver import ArtifactResolver
from src.orchestration.scheduler import TaskScheduler
from src.orchestration.store import ArtifactStore, ArtifactStoreCorruption

logger = logging.getLogger(__name__)

ExecuteStep = Callable[..., Awaitable[Any]]


def build_task_graph_from_state(state: dict) -> TaskGraph:
    """Resolve a :class:`TaskGraph` from state.

    Accepts an explicit ``state["task_graph"]`` (a ``TaskGraph`` or a dict) or
    falls back to converting ``state["planning_steps"]``.
    """
    tg = state.get("task_graph")
    if isinstance(tg, TaskGraph):
        return tg.validate_dag()
    if isinstance(tg, dict):
        return TaskGraph(**tg).validate_dag()
    steps = state.get("planning_steps") or []
    task_id = state.get("task_id") or state.get("workflow_id") or "task"
    return plan_to_task_graph(steps, task_id=task_id, subject=state.get("user_id"))


def has_task_graph(state: dict) -> bool:
    """True if state carries an explicit task graph (gates the scheduler path)."""
    return bool(state.get("task_graph"))


def _restore_outputs(state: dict, completed: set[str]) -> dict:
    """Rebuild ``{step_id: {param: ArtifactRef}}`` for completed steps on resume.

    Reads the serialized ``step_results`` persisted in a checkpoint and revives
    the ``ArtifactRef`` outputs so the scheduler can re-seed upstream data for
    resumed downstream steps.
    """
    step_results = state.get("step_results")
    if not isinstance(step_results, dict):
        return {}
    outputs: dict = {}
    for sid, result in step_results.items():
        if sid not in completed or not isinstance(result, dict):
            continue
        raw_outputs = result.get("outputs") or {}
        revived: dict = {}
        for param, ref in raw_outputs.items():
            if isinstance(ref, ArtifactRef):
                revived[param] = ref
            elif isinstance(ref, dict):
                try:
                    revived[param] = ArtifactRef(**ref)
                except Exception:  # noqa: BLE001 - skip malformed ref
                    continue
        if revived:
            outputs[sid] = revived
    return outputs


def unknown_operation_modes(graph: TaskGraph) -> list[str]:
    """Return step ids whose ``operation_mode`` could not be classified.

    A step is scheduler-ready only when every step is a known read/write/send.
    An ``"unknown"`` mode means a potential side effect was not classifiable, so
    the runtime must refuse to schedule it (fail closed) rather than default to
    read and risk running a write as a parallel read-only step.
    """
    return [
        s.step_id
        for s in graph.steps
        if str(getattr(s, "operation_mode", "read")).lower() == "unknown"
    ]


def scheduler_ready(state: dict) -> tuple[bool, str, str]:
    """Classify whether ``state`` may enter the TaskGraph scheduler.

    Returns ``(ready, category, detail)`` where ``category`` is one of:

    - ``"ok"``       -> a valid, fully-classified graph; enter the scheduler.
    - ``"no_graph"`` -> no explicit task graph yet (planning phase may proceed
      to the Planner on the legacy path; the production execution phase must
      fail closed).
    - ``"invalid"``  -> the graph exists but fails structural validation.
    - ``"unknown"``  -> a step has an unclassified (``"unknown"``) operation
      mode, i.e. a potential side effect that must never run as read-only.

    ``invalid`` / ``unknown`` must always fail closed regardless of phase.
    """
    if not has_task_graph(state):
        return False, "no_graph", "no explicit task graph"
    try:
        graph = build_task_graph_from_state(state)
    except Exception as exc:  # noqa: BLE001 - invalid graph -> fail closed
        return False, "invalid", f"invalid task graph: {exc}"
    unknown = unknown_operation_modes(graph)
    if unknown:
        return False, "unknown", f"unclassified operation mode: {unknown}"
    return True, "ok", "ok"


async def _list_agents_and_authorized(state: dict) -> tuple[list, set]:
    """Best-effort gather agents + authorized ids for real routing (lazy imports)."""
    try:
        from src.manager import agent_manager
        from config.s_abac_demo_users import get_user_available_agents

        await agent_manager.ensure_initialized()
        agents = await agent_manager.agent_registry.list()
        available = get_user_available_agents(state.get("user_id")) or []
        if available == ["*"]:
            authorized = {getattr(a, "agent_name", "") for a in agents}
        else:
            authorized = set(available)
        return list(agents), authorized
    except Exception as exc:  # noqa: BLE001 - routing can still fall back to preferred
        logger.warning("scheduler: could not list agents for routing: %s", exc)
        return [], set()


def _build_execution_context(state: dict, step, selected_agent):
    """Build a per-step ExecutionContext carrying acting user + producer agent.

    Isolated per step (never shared across concurrent steps) so captured
    artifacts get correct owner/producer/provenance metadata.
    """
    from src.manager.executor.base import ExecutionContext

    return ExecutionContext(
        user_id=state.get("user_id"),
        workflow_id=state.get("workflow_id"),
        workflow_mode=state.get("workflow_mode"),
        deep_thinking_mode=state.get("deep_thinking_mode", False),
        metadata={
            "task_id": state.get("task_id"),
            "node_name": "scheduler",
            "step_id": step.step_id,
            "operation_mode": step.operation_mode,
            "producer_agent_id": selected_agent,
            "selected_agent": selected_agent,
            "risk_profile": state.get("risk_profile", "LOW"),
            "task_profile": state.get("task_profile", {}),
            "scenario_tags": state.get("scenario_tags", []),
            "expected_capabilities": state.get("expected_capabilities", []),
        },
    )


def _make_context_factory(state: dict):
    """Return a ``factory(step, selected_agent) -> ExecutionContext`` bound to state."""

    def _factory(step, selected_agent):
        return _build_execution_context(state, step, selected_agent)

    return _factory


def _make_real_execute_step(state: dict) -> ExecuteStep:
    """Build the production ``execute_step`` mirroring ``agent_proxy_node``."""

    async def _execute_step(*, step, selected_agent, inputs, context) -> Any:
        from src.manager import agent_manager
        from src.manager.executor.base import ExecuteResult, ExecutionStatus
        from src.manager.executor.factory import execute_agent
        from src.security.enforcement import enforce_agent_dispatch

        if not selected_agent:
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error=f"no agent selected for step {step.step_id}",
            )

        await agent_manager.ensure_initialized()
        agent = await agent_manager.agent_registry.get(selected_agent)
        if agent is None:
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error=f"agent not found in registry: {selected_agent}",
            )

        # Reuse the per-step ExecutionContext built by the injected factory so
        # the same context drives dispatch enforcement and artifact capture.
        exec_ctx = context.get("execution_context") if isinstance(
            context, dict) else None
        if exec_ctx is None:
            exec_ctx = _build_execution_context(state, step, selected_agent)
        await enforce_agent_dispatch(agent, exec_ctx)

        brief = {
            "original_user_query": state.get("original_user_query")
            or state.get("USER_QUERY")
            or "",
            "assigned_agent": selected_agent,
            # Surfaced so an idempotency-aware tool/provider can dedupe an
            # external side effect (e.g. a message id / request key).
            "idempotency_key": (context.get("idempotency_key") if isinstance(context, dict) else None),
            "step": {
                "step_id": step.step_id,
                "title": getattr(step, "title", ""),
                "description": getattr(step, "description", ""),
            },
            "resolved_inputs": inputs,
            "instruction": (
                "Complete only this step using the resolved inputs and the "
                "original user query. Do not inspect unrelated local files."
            ),
        }
        messages = list(state.get("messages", [])) + [
            {
                "role": "user",
                "content": "EXECUTION_CONTEXT\n"
                + json.dumps(brief, ensure_ascii=False, default=str),
            }
        ]
        return await execute_agent(agent, messages, exec_ctx)

    return _execute_step


async def run_scheduler_workflow(
    state: dict,
    *,
    task_id: str,
    checkpoint_manager: Any = None,
    task_logger: Any = None,
    hook_engine: Any = None,
    execute_step: Optional[ExecuteStep] = None,
    routing_provider: Optional[RoutingProvider] = None,
) -> AsyncGenerator[dict, None]:
    """Drive the scheduler over the state's TaskGraph, yielding workflow events.

    Mirrors the legacy event stream so the frontend/consumers are unaffected.
    """
    workflow_id = state.get("workflow_id")
    graph = build_task_graph_from_state(state)

    yield {
        "event": "start_of_workflow",
        "data": {"workflow_id": workflow_id, "task_id": task_id, "mode": "scheduler"},
    }

    # Scenario used by the artifact guard to evaluate S-ABAC scenario fit.
    scenario_ctx = {
        "scenario_tags": state.get("scenario_tags", []),
        "expected_capabilities": state.get("expected_capabilities", []),
        "task_type": (state.get("task_profile", {}) or {}).get("task_type", "GENERAL"),
        "risk_profile": state.get("risk_profile", "LOW"),
        "scenario_fit_result": state.get("scenario_fit_result", {}),
    }

    store = ArtifactStore()
    # Dedicated protected payload store: full (possibly sensitive) artifact
    # payloads live here, NOT in the generic checkpoint. The checkpoint keeps
    # only a de-sensitized index (refs + checksum + logical name/sensitivity).
    payload_store = ArtifactPayloadStore(task_id)
    # Retention: prune sibling payload stores older than the configured TTL so
    # sensitive payloads do not linger indefinitely on disk. Best-effort -- a
    # cleanup failure must never block or fail a run.
    try:
        ttl = float(os.getenv("ARTIFACT_PAYLOAD_TTL_SECONDS",
                    str(7 * 24 * 3600)))
        payload_store.cleanup_expired(ttl_seconds=ttl)
    except Exception as exc:  # noqa: BLE001 - retention is best-effort
        logger.debug("scheduler: payload retention cleanup skipped: %s", exc)
    # Resume: rebuild the artifact payloads produced by already-completed steps
    # from the protected payload store using the checkpoint's index. Any
    # integrity failure (missing/tampered payload) fails closed with a terminal
    # event -- never silently continues with partial data.
    restored_index = state.get("artifacts")
    if isinstance(restored_index, dict) and restored_index:
        try:
            payloads = payload_store.load_index(restored_index)
            store.load_state(payloads)
        except (ArtifactStoreCorruption, ArtifactPayloadCorruption) as exc:
            logger.error(
                "scheduler: corrupt/missing restored artifacts: %s", exc)
            yield {
                "event": "end_of_workflow",
                "data": {
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "mode": "scheduler",
                    "status": WorkflowStatus.FAILED.value,
                    "error": f"corrupt artifact store on resume: {exc}",
                    "reason": "artifact_store_corruption",
                },
            }
            return
    resolver = ArtifactResolver(
        store, guard=PolicyEngineArtifactGuard(scenario=scenario_ctx))

    if routing_provider is None:
        routing = MainAgentRoutingProvider()
        agents, authorized = await _list_agents_and_authorized(state)
    else:
        routing = routing_provider
        agents, authorized = (), set()

    execute = execute_step or _make_real_execute_step(state)

    event_queue: asyncio.Queue[dict] = asyncio.Queue()
    counter = {"step": int(state.get("current_step") or 0)}

    async def on_step_start(*, step, selected_agent, inputs):
        if task_logger is not None:
            try:
                task_logger.log_agent_start(
                    node_name="scheduler", step=counter["step"], sub_agent_name=selected_agent
                )
            except Exception:  # noqa: BLE001
                pass
        await event_queue.put(
            {
                "event": "start_of_agent",
                "data": {
                    "agent_name": f"scheduler【{selected_agent}】",
                    "agent_id": f"{workflow_id}_{step.step_id}",
                    "sub_agent_name": selected_agent,
                },
            }
        )

    async def commit_step_result(*, step, result):
        """CRITICAL durable persistence for a completed step (crash-safe order).

        The checkpoint must record the completion ATOMICALLY with it becoming
        durable, otherwise a crash right after a step succeeds would restore a
        checkpoint that omits the step and re-schedule it on resume (a re-run;
        side effects are receipt-protected, but read-only queries would repeat).

        Order:
          1. write the Artifact payload;
          2. build a CANDIDATE state that already includes this step in
             ``completed_steps`` (+ updated ``step_results``/``artifacts``);
          3. save the checkpoint from the CANDIDATE state;
          4. only after the checkpoint succeeds, promote the candidate values
             into the live in-memory ``state``.
        On any failure the live ``state`` is left unchanged and the exception
        propagates so the scheduler marks the step FAILED (never SUCCEEDED).
        """
        succeeded = result.status == StepStatus.SUCCEEDED

        # (1) Candidate step_results (do not mutate live state yet).
        step_results = dict(state.get("step_results") or {})
        step_results[step.step_id] = result.model_dump()

        # (1) Persist artifact payloads to the PROTECTED payload store. Only a
        # de-sensitized index (refs + checksum) is carried in the checkpoint.
        artifacts_index = state.get("artifacts")
        artifacts_updated = False
        if succeeded and result.outputs:
            artifacts_index = payload_store.save_store_state(
                store.dump_state())
            artifacts_updated = True

        # (2) Candidate completion set INCLUDING this step, so a checkpoint
        # restored after a crash skips it (never re-schedules a done step).
        completed = list(state.get("completed_steps") or [])
        if succeeded and step.step_id not in completed:
            completed.append(step.step_id)

        candidate = dict(state)
        candidate["step_results"] = step_results
        if artifacts_updated:
            candidate["artifacts"] = artifacts_index
        candidate["completed_steps"] = completed

        # (3) Save the checkpoint FROM THE CANDIDATE (completion already applied).
        # A failure here propagates -> the step is not reported SUCCEEDED and the
        # live state is left untouched.
        if checkpoint_manager is not None:
            checkpoint_manager.save_checkpoint(
                workflow_id=workflow_id,
                task_id=task_id,
                step=counter["step"],
                node_name="scheduler",
                next_node="scheduler",
                state=candidate,
            )

        # (4) Durable write succeeded -> promote candidate values into live state.
        state["step_results"] = step_results
        if artifacts_updated:
            state["artifacts"] = artifacts_index
        state["completed_steps"] = completed

    async def on_step_end(*, step, result):
        # Non-critical hooks: logging + SSE event. Best effort (the scheduler
        # swallows exceptions here so a monitoring failure never fails a step).
        if task_logger is not None:
            try:
                task_logger.log_agent_end(
                    node_name="scheduler",
                    next_node="scheduler",
                    step=counter["step"],
                    sub_agent_name=getattr(step, "agent_name", None),
                )
            except Exception:  # noqa: BLE001
                pass
        counter["step"] += 1

        await event_queue.put(
            {
                "event": "end_of_agent",
                "data": {
                    "agent_name": f"scheduler【{step.step_id}】",
                    "agent_id": f"{workflow_id}_{step.step_id}",
                    "sub_agent_name": getattr(step, "agent_name", None),
                    "status": str(result.status),
                },
            }
        )

    scheduler = TaskScheduler(
        execute_step=execute,
        routing_provider=routing,
        store=store,
        resolver=resolver,
        receipt_store=PersistentReceiptStore(task_id),
    )
    ctx = {
        "user_query": state.get("USER_QUERY", "") or state.get("original_user_query", ""),
        "task_id": task_id,
        "workflow_id": workflow_id,
        "subject": state.get("user_id"),
        "scenario": scenario_ctx,
        "agents": agents,
        "authorized_agent_ids": authorized,
        "metadata": {"scenario_tags": state.get("scenario_tags", [])},
        # Per-step ExecutionContext builder so captured artifacts carry the
        # acting user (owner) and the producing agent.
        "context_factory": _make_context_factory(state),
    }
    initial_completed = set(state.get("completed_steps") or [])
    initial_outputs = _restore_outputs(state, initial_completed)

    run_task = asyncio.create_task(
        scheduler.run(
            graph,
            context=ctx,
            initial_completed=initial_completed,
            initial_outputs=initial_outputs,
            on_step_start=on_step_start,
            on_step_end=on_step_end,
            commit_step_result=commit_step_result,
        )
    )

    try:
        while True:
            if run_task.done():
                while not event_queue.empty():
                    yield await event_queue.get()
                break
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.05)
                yield event
            except asyncio.TimeoutError:
                continue
        results = await run_task
    except Exception as exc:  # noqa: BLE001 - guarantee end_of_workflow is emitted
        logger.exception("scheduler.run() raised unexpectedly")
        # Drain any events enqueued before the failure so nothing is lost.
        while not event_queue.empty():
            yield await event_queue.get()
        yield {
            "event": "end_of_workflow",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "mode": "scheduler",
                "status": WorkflowStatus.FAILED.value,
                "error": str(exc),
            },
        }
        return
    finally:
        if not run_task.done():
            run_task.cancel()

    # ``results`` is a WorkflowResult: it carries the authoritative workflow-level
    # terminal status so the frontend never infers success from the mere
    # presence of an end_of_workflow event.
    failed = [sid for sid, r in results.items() if r.status !=
              StepStatus.SUCCEEDED]
    clarifications = [c for c in (
        getattr(results, "clarifications", []) or []) if c]
    rejected = list(getattr(results, "rejected_steps", []) or [])
    needs_recon = list(getattr(results, "needs_reconciliation", []) or [])
    terminal = getattr(results, "terminal_status", None)
    status = str(getattr(terminal, "value", terminal)
                 or WorkflowStatus.SUCCEEDED.value)
    yield {
        "event": "end_of_workflow",
        "data": {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "mode": "scheduler",
            "status": status,
            "failed_steps": failed,
            "rejected_steps": rejected,
            "clarifications": clarifications,
            "needs_reconciliation": needs_recon,
            "results": {sid: str(r.status) for sid, r in results.items()},
        },
    }
