import asyncio

import pytest

from src.interface.artifact import ArtifactRef
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.artifact_payload_store import ArtifactPayloadStore
from src.orchestration.runtime import run_scheduler_workflow
from src.orchestration.schema_registry import get_schema_registry
from src.orchestration.store import ArtifactStore
from src.robust.checkpoint import CheckpointManager


EMPLOYEE_SCHEMA = "acceptance.employee.v1"
POLICY_SCHEMA = "acceptance.policy.v1"
REPORT_SCHEMA = "acceptance.report.v1"


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ARTIFACT_PAYLOAD_STORE_DIR",
        str(tmp_path / "artifacts"),
    )
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))
    monkeypatch.setattr(
        "src.service.env.S_ABAC_ENABLED",
        False,
        raising=False,
    )
    registry = get_schema_registry()
    registry.register(
        EMPLOYEE_SCHEMA,
        {
            "required": ["employee_name", "department"],
            "properties": {
                "employee_name": {"type": "string"},
                "department": {"type": "string"},
            },
        },
    )
    registry.register(
        POLICY_SCHEMA,
        {
            "required": ["policy_name", "summary"],
            "properties": {
                "policy_name": {"type": "string"},
                "summary": {"type": "string"},
            },
        },
    )
    registry.register(
        REPORT_SCHEMA,
        {
            "required": ["title", "employee", "policy"],
            "properties": {
                "title": {"type": "string"},
                "employee": {"type": "string"},
                "policy": {"type": "string"},
            },
        },
    )


def _three_agent_graph() -> TaskGraph:
    return TaskGraph(
        spec=TaskSpec(task_id="three-agent", subject="u1"),
        steps=[
            TaskStep(
                step_id="step_1",
                agent_name="RemoteHRAssistantAgent",
                preferred_resource_id="RemoteHRAssistantAgent",
                operation_mode="read",
                expected_outputs=["employee"],
                expected_schema_ref=EMPLOYEE_SCHEMA,
            ),
            TaskStep(
                step_id="step_2",
                agent_name="RemoteKnowledgeAgent",
                preferred_resource_id="RemoteKnowledgeAgent",
                operation_mode="read",
                expected_outputs=["policy"],
                expected_schema_ref=POLICY_SCHEMA,
            ),
            TaskStep(
                step_id="step_3",
                agent_name="RemoteReportAgent",
                preferred_resource_id="RemoteReportAgent",
                operation_mode="read",
                depends_on=["step_1", "step_2"],
                expected_outputs=["report"],
                expected_schema_ref=REPORT_SCHEMA,
                input_bindings=[
                    {
                        "parameter_name": "employee_data",
                        "source_step": "step_1",
                        "source_output": "employee",
                    },
                    {
                        "parameter_name": "policy_data",
                        "source_step": "step_2",
                        "source_output": "policy",
                    },
                ],
            ),
        ],
    )


def test_three_agent_parallel_fan_in_checkpoint_resume_and_final_result(tmp_path):
    graph = _three_agent_graph()
    state = {
        "workflow_id": "wf-three-agent",
        "user_id": "u1",
        "task_graph": graph,
        "messages": [],
        "original_user_query": "查询王强信息和年假政策，并汇总报告",
    }
    checkpoint_manager = CheckpointManager(tmp_path / "checkpoints")
    parallel_gate = asyncio.Event()
    started: set[str] = set()
    reporter_inputs = {}

    async def _execute(*, step, selected_agent, inputs, context):
        if step.step_id in {"step_1", "step_2"}:
            started.add(step.step_id)
            if started == {"step_1", "step_2"}:
                parallel_gate.set()
            await asyncio.wait_for(parallel_gate.wait(), timeout=1)
        if step.step_id == "step_1":
            return ExecuteResult(
                status=ExecutionStatus.SUCCESS,
                result={"employee_name": "王强", "department": "研发部"},
            )
        if step.step_id == "step_2":
            return ExecuteResult(
                status=ExecutionStatus.SUCCESS,
                result={"policy_name": "年假制度", "summary": "按司龄享受年假"},
            )
        reporter_inputs.update(inputs)
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={
                "title": "员工政策汇总",
                "employee": inputs["employee_data"]["employee_name"],
                "policy": inputs["policy_data"]["policy_name"],
            },
        )

    async def _run(initial_state, execute):
        return [
            event
            async for event in run_scheduler_workflow(
                initial_state,
                task_id="task-three-agent",
                checkpoint_manager=checkpoint_manager,
                execute_step=execute,
                routing_provider=StubRoutingProvider(),
            )
        ]

    events = asyncio.run(_run(state, _execute))

    starts = [
        event["data"]["step_id"]
        for event in events
        if event["event"] == "start_of_agent"
    ]
    assert starts[:2] == ["step_1", "step_2"]
    assert reporter_inputs == {
        "employee_data": {"employee_name": "王强", "department": "研发部"},
        "policy_data": {"policy_name": "年假制度", "summary": "按司龄享受年假"},
    }
    assert events[-2]["event"] == "final_result"
    assert events[-2]["data"]["result"] == {
        "title": "员工政策汇总",
        "employee": "王强",
        "policy": "年假制度",
    }
    assert events[-2]["data"]["source_artifact_refs"][0]["step_id"] == "step_3"
    assert events[-1]["data"]["status"] == "SUCCEEDED"

    payloads = ArtifactPayloadStore("task-three-agent").load_index(
        state["artifacts"]
    )
    artifact_store = ArtifactStore()
    artifact_store.load_state(payloads)
    report_ref = state["step_results"]["step_3"]["outputs"]["report"]
    report_artifact = artifact_store.get(ArtifactRef(**report_ref))
    assert report_artifact.schema_ref == REPORT_SCHEMA
    assert report_artifact.schema_valid is True
    assert {
        ref.artifact_id for ref in report_artifact.derived_from
    } == {
        state["step_results"]["step_1"]["outputs"]["employee"]["artifact_id"],
        state["step_results"]["step_2"]["outputs"]["policy"]["artifact_id"],
    }

    pre_report_checkpoint = None
    for summary in checkpoint_manager.list_checkpoints(task_id="task-three-agent"):
        checkpoint = checkpoint_manager.load_checkpoint(
            task_id="task-three-agent",
            step=summary["step"],
        )
        if set(checkpoint.state.get("completed_steps") or []) == {
            "step_1",
            "step_2",
        }:
            pre_report_checkpoint = checkpoint
            break
    assert pre_report_checkpoint is not None

    resumed_calls = []

    async def _resume_execute(*, step, selected_agent, inputs, context):
        resumed_calls.append(step.step_id)
        assert step.step_id == "step_3"
        assert set(inputs) == {"employee_data", "policy_data"}
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={
                "title": "恢复后的员工政策汇总",
                "employee": inputs["employee_data"]["employee_name"],
                "policy": inputs["policy_data"]["policy_name"],
            },
        )

    fresh_state = dict(pre_report_checkpoint.state)
    resumed_events = asyncio.run(_run(fresh_state, _resume_execute))

    assert resumed_calls == ["step_3"]
    assert resumed_events[-2]["event"] == "final_result"
    assert resumed_events[-2]["data"]["result"]["title"] == "恢复后的员工政策汇总"
    assert resumed_events[-1]["data"]["status"] == "SUCCEEDED"
