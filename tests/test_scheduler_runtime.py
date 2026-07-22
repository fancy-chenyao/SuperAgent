"""Smoke tests for the scheduler runtime bridge (Plan Phase 3d).

Injects a fake ``execute_step`` + stub routing so the bridge is exercised without
the real agent/LLM stack. Verifies the emitted event stream and state updates.
"""

import asyncio

from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.runtime import (
    build_task_graph_from_state,
    has_task_graph,
    run_scheduler_workflow,
)


async def _fake_execute(*, step, selected_agent, inputs, context):
    return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"ok": step.step_id})


def _collect(state):
    async def _run():
        events = []
        async for ev in run_scheduler_workflow(
            state,
            task_id="task-1",
            execute_step=_fake_execute,
            routing_provider=StubRoutingProvider(),
        ):
            events.append(ev)
        return events

    return asyncio.run(_run())


def _two_step_state():
    graph = TaskGraph(
        spec=TaskSpec(task_id="task-1"),
        steps=[
            TaskStep(step_id="s1", preferred_resource_id="A", agent_name="A", expected_outputs=["out_a"]),
            TaskStep(step_id="s2", depends_on=["s1"], preferred_resource_id="B", agent_name="B"),
        ],
    )
    return {"workflow_id": "wf1", "user_id": "u1", "task_graph": graph, "messages": []}


def test_runtime_emits_workflow_and_agent_events_in_order():
    state = _two_step_state()
    events = _collect(state)

    assert events[0]["event"] == "start_of_workflow"
    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == "completed"

    # start/end per step, serialized (s1 fully before s2)
    kinds = [(e["event"], e["data"].get("sub_agent_name")) for e in events if e["event"].endswith("_of_agent")]
    assert kinds == [
        ("start_of_agent", "A"),
        ("end_of_agent", "A"),
        ("start_of_agent", "B"),
        ("end_of_agent", "B"),
    ]


def test_runtime_updates_state_completed_and_results():
    state = _two_step_state()
    _collect(state)
    assert state["completed_steps"] == ["s1", "s2"]
    assert set(state["step_results"].keys()) == {"s1", "s2"}


def test_runtime_reports_failure_status():
    async def _fail_second(*, step, selected_agent, inputs, context):
        if step.step_id == "s2":
            return ExecuteResult(status=ExecutionStatus.FAILED, error="boom")
        return ExecuteResult(status=ExecutionStatus.SUCCESS, result={"ok": step.step_id})

    state = _two_step_state()

    async def _run():
        events = []
        async for ev in run_scheduler_workflow(
            state,
            task_id="task-1",
            execute_step=_fail_second,
            routing_provider=StubRoutingProvider(),
        ):
            events.append(ev)
        return events

    events = asyncio.run(_run())
    end = events[-1]
    assert end["event"] == "end_of_workflow"
    assert end["data"]["status"] == "failed"
    assert "s2" in end["data"]["failed_steps"]
    assert state["completed_steps"] == ["s1"]


def test_runtime_emits_end_of_workflow_when_scheduler_crashes():
    """An unexpected error inside scheduler.run() must still close the stream."""

    async def _boom(*, step, selected_agent, inputs, context):
        raise RuntimeError("routing exploded")

    # Force run_scheduler_workflow to fail *outside* per-step handling by using a
    # routing provider that raises: routing is invoked before any try guard the
    # step-level fix adds is irrelevant here because we monkeypatch run() below.
    state = _two_step_state()

    class _ExplodingScheduler:
        def __init__(self, *a, **k):
            pass

        async def run(self, *a, **k):
            raise RuntimeError("scheduler exploded")

    import src.orchestration.runtime as runtime_mod

    original = runtime_mod.TaskScheduler
    runtime_mod.TaskScheduler = _ExplodingScheduler
    try:
        async def _run():
            events = []
            async for ev in run_scheduler_workflow(
                state,
                task_id="task-1",
                execute_step=_boom,
                routing_provider=StubRoutingProvider(),
            ):
                events.append(ev)
            return events

        events = asyncio.run(_run())
    finally:
        runtime_mod.TaskScheduler = original

    assert events[0]["event"] == "start_of_workflow"
    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == "error"
    assert "scheduler exploded" in events[-1]["data"]["error"]


def test_has_task_graph_gating():
    assert has_task_graph({"task_graph": {"spec": {"task_id": "t"}, "steps": []}}) is True
    assert has_task_graph({"planning_steps": [{"agent_name": "A"}]}) is False
    assert has_task_graph({}) is False


def test_build_task_graph_from_planning_steps_fallback():
    state = {
        "workflow_id": "wf",
        "user_id": "u",
        "planning_steps": [
            {"agent_name": "A"},
            {
                "agent_name": "B",
                "inputs": [{"parameter_name": "x", "source_step": "A", "source_output": "o"}],
            },
        ],
    }
    graph = build_task_graph_from_state(state)
    smap = graph.step_map()
    assert smap["step_2"].depends_on == ["step_1"]


def test_build_task_graph_from_dict():
    state = {
        "task_graph": {
            "spec": {"task_id": "t"},
            "steps": [{"step_id": "only", "operation_mode": "read"}],
        }
    }
    graph = build_task_graph_from_state(state)
    assert list(graph.step_map().keys()) == ["only"]
