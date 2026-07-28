"""Smoke tests for the scheduler runtime bridge (Plan Phase 3d).

Injects a fake ``execute_step`` + stub routing so the bridge is exercised without
the real agent/LLM stack. Verifies the emitted event stream and state updates.
"""

import asyncio
import json

import pytest

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.interface.artifact import ArtifactRef, StepResult, StepStatus
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.runtime import (
    _public_step_metrics,
    _required_step_outputs,
    build_task_graph_from_state,
    has_task_graph,
    run_scheduler_workflow,
)


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_PAYLOAD_STORE_DIR",
                       str(tmp_path / "artifacts"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    # Unit tests use synthetic user ``u1``; keep the runtime result-event
    # contract deterministic regardless of a developer's local S-ABAC .env.
    monkeypatch.setattr(
        "src.service.env.S_ABAC_ENABLED", False, raising=False
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
            TaskStep(step_id="s1", preferred_resource_id="A",
                     agent_name="A", expected_outputs=["out_a"]),
            TaskStep(step_id="s2", depends_on=[
                     "s1"], preferred_resource_id="B", agent_name="B"),
        ],
    )
    return {"workflow_id": "wf1", "user_id": "u1", "task_graph": graph, "messages": []}


def test_resume_requires_only_required_contract_outputs():
    contract = AgentContract(
        produces=[
            DataContractRef(name="employee.info", schema_ref="employee.info@v1"),
            DataContractRef(
                name="employee.salary",
                schema_ref="employee.salary@v1",
                required=False,
            ),
        ]
    )
    step = TaskStep(
        step_id="hr",
        expected_outputs=["employee.info", "employee.salary"],
        agent_contract=contract,
    )

    assert _required_step_outputs(step) == ["employee.info"]


def test_public_step_metrics_excludes_result_and_remote_diagnostics():
    public = _public_step_metrics(
        {
            "attempts": 2,
            "routing_decision": "DISPATCH",
            "result_error": "REMOTE_PRIVATE_CODE",
            "result_error_details": {
                "payload": {"salary": 1000},
                "traceback": "private",
            },
            "selected_agent": "private-provider-id",
            "external_op_id": "private-operation-id",
        }
    )

    assert public == {"attempts": 2, "routing_decision": "DISPATCH"}


def test_runtime_emits_workflow_and_agent_events_in_order():
    state = _two_step_state()
    events = _collect(state)

    assert events[0]["event"] == "start_of_workflow"
    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == "SUCCEEDED"

    # start/end per step, serialized (s1 fully before s2)
    kinds = [(e["event"], e["data"].get("sub_agent_name"))
             for e in events if e["event"].endswith("_of_agent")]
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


def test_runtime_emits_materialized_step_result_payload():
    state = _two_step_state()
    events = _collect(state)

    result_events = [event for event in events if event["event"] == "step_result"]
    assert [event["data"]["step_id"] for event in result_events] == ["s1", "s2"]
    assert result_events[0]["data"]["status"] == "SUCCEEDED"
    assert result_events[0]["data"]["agent_id"] == "wf1_s1"
    assert result_events[0]["data"]["agent_name"] == "A"
    assert isinstance(result_events[0]["data"]["metrics"], dict)
    assert result_events[0]["data"]["outputs"]["out_a"] == {"ok": "s1"}
    assert result_events[0]["data"]["output_refs"]["out_a"]["artifact_id"]


def test_runtime_emits_governed_leaf_final_result_before_terminal_event():
    events = _collect(_two_step_state())

    assert events[-2]["event"] == "final_result"
    assert events[-1]["event"] == "end_of_workflow"
    final = events[-2]["data"]
    assert final["workflow_status"] == "SUCCEEDED"
    assert final["available"] is True
    assert final["leaf_steps"] == ["s2"]
    assert final["result"] == {"ok": "s2"}
    assert final["source_artifact_refs"][0]["step_id"] == "s2"
    assert final["source_artifact_refs"][0]["artifact_ref"]["artifact_id"]


def test_permission_denial_never_leaks_step_or_final_payload(monkeypatch):
    import src.orchestration.runtime as runtime_mod

    class _DenyGuard:
        def __init__(self, **_kwargs):
            pass

        def can_read(self, **_kwargs):
            return False

    async def _secret_execute(*, step, selected_agent, inputs, context):
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={"secret_marker": "DO_NOT_LEAK"},
        )

    monkeypatch.setattr(runtime_mod, "PolicyEngineArtifactGuard", _DenyGuard)
    state = {
        "workflow_id": "wf-deny",
        "user_id": "u1",
        "task_graph": TaskGraph(
            spec=TaskSpec(task_id="deny"),
            steps=[
                TaskStep(
                    step_id="leaf",
                    agent_name="A",
                    preferred_resource_id="A",
                    expected_outputs=["secret"],
                )
            ],
        ),
        "messages": [],
    }

    async def _run():
        return [
            event
            async for event in run_scheduler_workflow(
                state,
                task_id="task-deny",
                execute_step=_secret_execute,
                routing_provider=StubRoutingProvider(),
            )
        ]

    events = asyncio.run(_run())
    step = next(event for event in events if event["event"] == "step_result")
    final = next(event for event in events if event["event"] == "final_result")

    assert step["data"]["outputs"] == {}
    assert step["data"]["unavailable_outputs"] == {
        "secret": "ArtifactAccessDenied"
    }
    assert final["data"]["available"] is False
    assert final["data"]["result"] is None
    assert "DO_NOT_LEAK" not in json.dumps(events, ensure_ascii=False)


def test_runtime_finalizes_task_logger_on_success():
    class RecordingTaskLogger:
        def __init__(self):
            self.workflow_end_calls = 0
            self.errors = []

        def log_agent_start(self, **_kwargs):
            return None

        def log_agent_end(self, **_kwargs):
            return None

        def log_workflow_end(self):
            self.workflow_end_calls += 1

        def log_error(self, **kwargs):
            self.errors.append(kwargs)

    state = _two_step_state()
    task_logger = RecordingTaskLogger()

    async def _run():
        events = []
        async for event in run_scheduler_workflow(
            state,
            task_id="task-1",
            task_logger=task_logger,
            execute_step=_fake_execute,
            routing_provider=StubRoutingProvider(),
        ):
            events.append(event)
        return events

    events = asyncio.run(_run())

    assert events[-1]["data"]["status"] == "SUCCEEDED"
    assert task_logger.workflow_end_calls == 1
    assert task_logger.errors == []


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
    # s1 succeeded, s2 failed -> partial failure at the workflow level.
    assert end["data"]["status"] == "PARTIAL_FAILED"
    assert "s2" in end["data"]["failed_steps"]
    assert end["data"]["blocked_steps"] == []
    assert end["data"]["failures"][0]["code"] == "AGENT_EXECUTION_FAILED"
    failed_step = next(
        event
        for event in events
        if event["event"] == "step_result"
        and event["data"]["step_id"] == "s2"
    )
    assert failed_step["data"]["failure"]["category"] == "execution"
    assert "boom" not in json.dumps(failed_step["data"])
    assert state["completed_steps"] == ["s1"]
    assert state["step_results"]["s2"]["failure"]["code"] == "AGENT_EXECUTION_FAILED"


def test_checkpoint_step_result_removes_raw_failure_diagnostics():
    import src.orchestration.runtime as runtime_mod
    from src.orchestration.failure_mapper import make_failure

    result = StepResult(
        step_id="unsafe",
        status=StepStatus.FAILED,
        error="<script>secret provider response</script>",
        metrics={
            "attempts": 1,
            "result_error_details": {"payload": "secret"},
            "selected_agent": "<img onerror=alert(1)>",
        },
        failure=make_failure("AGENT_EXECUTION_FAILED", step_id="unsafe"),
    )

    saved = runtime_mod._checkpoint_step_result(result)

    assert saved["error"] == result.failure.message
    assert saved["metrics"] == {"attempts": 1}
    assert "secret provider response" not in json.dumps(saved)


def test_resume_missing_leaf_artifact_cannot_report_success():
    graph = TaskGraph(
        spec=TaskSpec(task_id="leaf-task"),
        steps=[
            TaskStep(
                step_id="leaf",
                preferred_resource_id="A",
                expected_outputs=["result"],
            )
        ],
    )
    state = {
        "workflow_id": "leaf-workflow",
        "user_id": "u1",
        "task_graph": graph,
        "messages": [],
        "completed_steps": ["leaf"],
        "step_results": {
            "leaf": StepResult(
                step_id="leaf",
                status=StepStatus.SUCCEEDED,
                outputs={
                    "result": ArtifactRef(
                        artifact_id="missing-artifact",
                        version=1,
                    )
                },
            ).model_dump(mode="json")
        },
        "artifacts": {},
    }

    events = _collect(state)
    terminal = events[-1]["data"]
    failed = next(event for event in events if event["event"] == "step_result")

    assert terminal["status"] == "FAILED"
    assert terminal["failed_steps"] == ["leaf"]
    assert failed["data"]["failure"]["code"] == "ARTIFACT_NOT_FOUND"
    assert state["completed_steps"] == []


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
    assert events[-1]["data"]["status"] == "FAILED"
    assert events[-1]["data"]["failures"][0]["code"] == "INTERNAL_SCHEDULER_ERROR"
    assert "scheduler exploded" not in json.dumps(events[-1]["data"])


def test_runtime_reports_restored_artifact_corruption_without_leaking_details(
    monkeypatch,
):
    import src.orchestration.runtime as runtime_mod

    def _corrupt_index(self, _index):
        raise runtime_mod.ArtifactPayloadCorruption("private path and payload")

    monkeypatch.setattr(
        runtime_mod.ArtifactPayloadStore,
        "load_index",
        _corrupt_index,
    )
    state = _two_step_state()
    state["artifacts"] = {"corrupt": {"index": "value"}}

    events = _collect(state)
    terminal = events[-1]

    assert terminal["event"] == "end_of_workflow"
    assert terminal["data"]["status"] == "FAILED"
    assert terminal["data"]["failures"][0]["code"] == "ARTIFACT_STORE_CORRUPTION"
    assert "private path" not in json.dumps(terminal["data"])


def test_runtime_checkpoint_failure_does_not_report_success():
    """P1-3: a checkpoint save failure is CRITICAL -- the step must not be
    reported SUCCEEDED and must not be marked completed."""

    class _FailingCheckpoints:
        def save_checkpoint(self, **kwargs):
            raise IOError("checkpoint disk full")

    state = _two_step_state()

    async def _run():
        events = []
        async for ev in run_scheduler_workflow(
            state,
            task_id="task-1",
            checkpoint_manager=_FailingCheckpoints(),
            execute_step=_fake_execute,
            routing_provider=StubRoutingProvider(),
        ):
            events.append(ev)
        return events

    events = asyncio.run(_run())
    end = events[-1]
    assert end["event"] == "end_of_workflow"
    assert end["data"]["status"] != "SUCCEEDED"
    # s1's persistence failed -> not completed; s2 is blocked by the failed dep.
    assert not state.get("completed_steps")  # never marked complete
    assert "s1" in end["data"]["failed_steps"]
    assert end["data"]["blocked_steps"] == ["s2"]
    assert {failure["code"] for failure in end["data"]["failures"]} == {
        "PERSISTENCE_FAILED",
        "UPSTREAM_STEP_FAILED",
    }
    result = next(event for event in events if event["event"] == "step_result")
    assert result["data"]["status"] == "FAILED"
    assert result["data"]["outputs"] == {}
    blocked = next(
        event
        for event in events
        if event["event"] == "step_result"
        and event["data"]["step_id"] == "s2"
    )
    assert blocked["data"]["status"] == "SKIPPED"
    assert blocked["data"]["failure"]["blocked_by"] == ["s1"]


def test_runtime_artifact_persistence_failure_does_not_report_success(monkeypatch):
    import src.orchestration.runtime as runtime_mod

    def _fail_artifact_persistence(self, state):
        raise OSError("artifact payload store unavailable")

    monkeypatch.setattr(
        runtime_mod.ArtifactPayloadStore,
        "save_store_state",
        _fail_artifact_persistence,
    )
    state = _two_step_state()
    events = _collect(state)

    result = next(event for event in events if event["event"] == "step_result")
    assert result["data"]["step_id"] == "s1"
    assert result["data"]["status"] == "FAILED"
    assert result["data"]["outputs"] == {}
    assert not state.get("completed_steps")
    assert events[-1]["data"]["status"] == "FAILED"


def test_has_task_graph_gating():
    assert has_task_graph(
        {"task_graph": {"spec": {"task_id": "t"}, "steps": []}}) is True
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
