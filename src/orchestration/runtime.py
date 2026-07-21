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
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

from src.interface.artifact import StepStatus
from src.interface.task_graph import TaskGraph
from src.orchestration.plan_to_task_graph import plan_to_task_graph
from src.orchestration.providers import MainAgentRoutingProvider, RoutingProvider
from src.orchestration.resolver import ArtifactResolver
from src.orchestration.scheduler import TaskScheduler
from src.orchestration.store import ArtifactStore

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


def _make_real_execute_step(state: dict) -> ExecuteStep:
    """Build the production ``execute_step`` mirroring ``agent_proxy_node``."""

    async def _execute_step(*, step, selected_agent, inputs, context) -> Any:
        from src.manager import agent_manager
        from src.manager.executor.base import ExecuteResult, ExecutionContext, ExecutionStatus
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

        exec_ctx = ExecutionContext(
            user_id=state.get("user_id"),
            workflow_id=state.get("workflow_id"),
            workflow_mode=state.get("workflow_mode"),
            deep_thinking_mode=state.get("deep_thinking_mode", False),
            metadata={
                "task_id": state.get("task_id"),
                "node_name": "scheduler",
                "step_id": step.step_id,
                "operation_mode": step.operation_mode,
                "risk_profile": state.get("risk_profile", "LOW"),
                "task_profile": state.get("task_profile", {}),
                "scenario_tags": state.get("scenario_tags", []),
                "expected_capabilities": state.get("expected_capabilities", []),
            },
        )
        await enforce_agent_dispatch(agent, exec_ctx)

        brief = {
            "original_user_query": state.get("original_user_query")
            or state.get("USER_QUERY")
            or "",
            "assigned_agent": selected_agent,
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

    store = ArtifactStore()
    resolver = ArtifactResolver(store)

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

    async def on_step_end(*, step, result):
        # Persist captured artifacts/results onto state (DAG-style checkpoint data).
        step_results = state.get("step_results")
        if not isinstance(step_results, dict):
            step_results = {}
        step_results[step.step_id] = result.model_dump()
        state["step_results"] = step_results

        completed = state.get("completed_steps")
        if not isinstance(completed, list):
            completed = []
        if result.status == StepStatus.SUCCEEDED and step.step_id not in completed:
            completed.append(step.step_id)
        state["completed_steps"] = completed

        if checkpoint_manager is not None:
            try:
                checkpoint_manager.save_checkpoint(
                    workflow_id=workflow_id,
                    task_id=task_id,
                    step=counter["step"],
                    node_name="scheduler",
                    next_node="scheduler",
                    state=state,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduler checkpoint save failed: %s", exc)
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
        execute_step=execute, routing_provider=routing, store=store, resolver=resolver
    )
    ctx = {
        "user_query": state.get("USER_QUERY", "") or state.get("original_user_query", ""),
        "task_id": task_id,
        "workflow_id": workflow_id,
        "subject": state.get("user_id"),
        "agents": agents,
        "authorized_agent_ids": authorized,
        "metadata": {"scenario_tags": state.get("scenario_tags", [])},
    }
    initial_completed = set(state.get("completed_steps") or [])

    run_task = asyncio.create_task(
        scheduler.run(
            graph,
            context=ctx,
            initial_completed=initial_completed,
            on_step_start=on_step_start,
            on_step_end=on_step_end,
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
    finally:
        if not run_task.done():
            run_task.cancel()

    failed = [sid for sid, r in results.items() if r.status != StepStatus.SUCCEEDED]
    yield {
        "event": "end_of_workflow",
        "data": {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "mode": "scheduler",
            "status": "failed" if failed else "completed",
            "failed_steps": failed,
            "results": {sid: str(r.status) for sid, r in results.items()},
        },
    }
