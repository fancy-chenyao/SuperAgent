import asyncio
import os
import tempfile
from pathlib import Path

from src.interface.artifact import Artifact, StepResult, StepStatus
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.manager.executor.remote import RemoteAgentResponse
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.runtime import run_scheduler_workflow
from src.skills.execution_evidence import (
    VerificationStatus,
    aggregate_evidence,
    build_step_evidence,
    evaluate_distillation_evidence,
)


def _success(result, metadata=None):
    return ExecuteResult(
        status=ExecutionStatus.SUCCESS,
        result=result,
        metadata=metadata or {},
    )


def test_read_success_is_distillable_without_business_verifier():
    step = build_step_evidence(
        step_id="query",
        agent_name="QueryAgent",
        operation_mode="read",
        execute_result=_success({"records": []}),
    )
    evidence = aggregate_evidence(
        task_id="task-1",
        workflow_status="SUCCEEDED",
        steps=[step],
    )

    decision = evaluate_distillation_evidence(evidence)
    assert step.verification_status == VerificationStatus.NOT_REQUIRED
    assert evidence.business_success is True
    assert decision.eligible is True
    assert decision.promotion_ready is True


def test_unverified_side_effect_can_only_contribute_a_candidate():
    step = build_step_evidence(
        step_id="send",
        agent_name="EmailAgent",
        operation_mode="send",
        execute_result=_success({"message_id": "msg-1"}),
    )
    evidence = aggregate_evidence(
        task_id="task-2",
        workflow_status="SUCCEEDED",
        steps=[step],
    )

    decision = evaluate_distillation_evidence(evidence)
    assert step.business_success is None
    assert step.verification_status == VerificationStatus.UNVERIFIED
    assert decision.eligible is True
    assert decision.promotion_ready is False
    assert "business_outcome_not_fully_verified" in decision.reasons


def test_platform_receipt_and_typed_business_identifier_verify_side_effect():
    artifact = Artifact(
        logical_name="send_receipt",
        payload={"message_id": "msg-1"},
        schema_valid=True,
    )
    result = StepResult(
        step_id="send",
        status=StepStatus.SUCCEEDED,
        metrics={
            "receipt_status": "SUCCEEDED",
            "idempotency_key": "idem-1",
            "external_op_id": "msg-1",
        },
    )
    step = build_step_evidence(
        step_id="send",
        operation_mode="send",
        artifact=artifact,
        step_result=result,
    )
    evidence = aggregate_evidence(
        task_id="task-3",
        workflow_status="SUCCEEDED",
        steps=[step],
    )

    assert step.business_success is True
    assert step.verification_status == VerificationStatus.VERIFIED
    assert evaluate_distillation_evidence(evidence).promotion_ready is True


def test_provider_receipt_verifies_standard_side_effect_even_if_payload_is_untyped():
    artifact = Artifact(
        logical_name="send_receipt",
        payload={"message_id": "msg-2"},
        schema_valid=False,
    )
    result = StepResult(
        step_id="send",
        status=StepStatus.SUCCEEDED,
        metrics={"receipt_status": "SUCCEEDED", "external_op_id": "msg-2"},
    )
    step = build_step_evidence(
        step_id="send",
        operation_mode="send",
        risk_level="MEDIUM",
        artifact=artifact,
        step_result=result,
    )

    assert step.business_success is True
    assert step.verification_status == VerificationStatus.VERIFIED
    assert step.schema_valid is False


def test_high_risk_side_effect_requires_trusted_verifier_not_only_receipt():
    result = StepResult(
        step_id="approve",
        status=StepStatus.SUCCEEDED,
        metrics={"receipt_status": "SUCCEEDED", "external_op_id": "approval-1"},
    )
    step = build_step_evidence(
        step_id="approve",
        operation_mode="approve",
        risk_level="HIGH",
        step_result=result,
    )

    assert step.business_success is None
    assert step.verification_status == VerificationStatus.UNVERIFIED
    assert step.verification_method == "trusted_verifier_required"


def test_high_risk_side_effect_accepts_platform_trusted_verifier_evidence():
    step = build_step_evidence(
        step_id="approve",
        operation_mode="approve",
        risk_level="HIGH",
        execute_result=_success(
            {"approval_id": "approval-2"},
            metadata={
                "verification_trusted": True,
                "business_outcome": {
                    "operation_status": "approved",
                    "verification": {
                        "verified": True,
                        "method": "approval_status_lookup",
                        "evidence_ref": "audit://approval-2",
                    },
                },
            },
        ),
    )

    assert step.business_success is True
    assert step.verification_status == VerificationStatus.VERIFIED
    assert step.verification_evidence_ref == "audit://approval-2"


def test_remote_outer_success_cannot_mask_nested_failure_dict_or_json():
    for payload in (
        {"business_outcome": {"operation_status": "failed"}},
        '{"result": {"status": "failed"}}',
    ):
        result = RemoteAgentResponse(status="success", result=payload).to_execute_result(
            0.01
        )
        assert result.status == ExecutionStatus.FAILED
        assert result.metadata["verification_trusted"] is False


def test_business_failure_is_not_eligible_for_distillation():
    step = build_step_evidence(
        step_id="submit",
        operation_mode="write",
        execute_result=_success(
            {"business_outcome": {"operation_status": "failed"}}
        ),
    )
    evidence = aggregate_evidence(
        task_id="task-4",
        workflow_status="SUCCEEDED",
        steps=[step],
    )

    decision = evaluate_distillation_evidence(evidence)
    assert evidence.technical_success is True
    assert evidence.business_success is False
    assert decision.eligible is False
    assert "business_outcome_failed" in decision.reasons


def test_empty_successful_workflow_has_no_distillable_trace():
    evidence = aggregate_evidence(
        task_id="task-empty",
        workflow_status="SUCCEEDED",
        steps=[],
    )

    decision = evaluate_distillation_evidence(evidence)
    assert evidence.technical_success is False
    assert decision.eligible is False
    assert "no_step_execution_evidence" in decision.reasons


def test_scheduler_persists_execution_evidence_in_state_and_terminal_event():
    async def execute_step(**_kwargs):
        return _success({"records": []})

    graph = TaskGraph(
        spec=TaskSpec(task_id="scheduler-task"),
        steps=[
            TaskStep(
                step_id="query",
                operation_mode="read",
                preferred_resource_id="QueryAgent",
                agent_name="QueryAgent",
            )
        ],
    )
    state = {
        "workflow_id": "workflow-1",
        "user_id": "alice",
        "task_graph": graph,
        "messages": [],
    }

    async def run():
        return [
            event
            async for event in run_scheduler_workflow(
                state,
                task_id="scheduler-task",
                execute_step=execute_step,
                routing_provider=StubRoutingProvider(),
            )
        ]

    old_artifacts = os.environ.get("ARTIFACT_PAYLOAD_STORE_DIR")
    old_receipts = os.environ.get("RECEIPT_STORE_DIR")
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        os.environ["ARTIFACT_PAYLOAD_STORE_DIR"] = str(root / "artifacts")
        os.environ["RECEIPT_STORE_DIR"] = str(root / "receipts")
        try:
            events = asyncio.run(run())
        finally:
            if old_artifacts is None:
                os.environ.pop("ARTIFACT_PAYLOAD_STORE_DIR", None)
            else:
                os.environ["ARTIFACT_PAYLOAD_STORE_DIR"] = old_artifacts
            if old_receipts is None:
                os.environ.pop("RECEIPT_STORE_DIR", None)
            else:
                os.environ["RECEIPT_STORE_DIR"] = old_receipts

    assert state["skill_execution_evidence"]["technical_success"] is True
    terminal = events[-1]
    assert terminal["event"] == "end_of_workflow"
    assert terminal["data"]["skill_execution_evidence"]["business_success"] is True
