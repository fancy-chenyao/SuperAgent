import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep, WorkflowStatus
from src.manager.executor.base import ExecuteResult, ExecutionContext, ExecutionStatus
from src.orchestration.completion import ReceiptStore
from src.orchestration.governance import (
    GovernanceEventStore,
    record_governance_event,
)
from src.orchestration.providers import StubRoutingProvider
from src.orchestration.reconciliation import get_reconciliation_store
from src.orchestration.runtime import run_scheduler_workflow
from src.orchestration.scheduler import TaskScheduler
from src.security.approval import ApprovalStore, get_approval_store
from src.security.enforcement import ApprovalRequiredError
from src.security.policy import Action, Object, Scenario, Subject


def _review_payload() -> dict:
    return {
        "subject": {
            "subject_type": "user",
            "id": "u1",
            "attributes": {},
        },
        "object": {
            "object_type": "agent",
            "id": "A",
            "attributes": {"requires_approval": True},
        },
        "scenario": {"task_scenario": {}, "environment": {}, "business_context": {}},
        "action": {
            "verb": "dispatch",
            "attributes": {"action_type": "write"},
        },
        "policy_result": {
            "allowed": False,
            "decision": "REVIEW_REQUIRED",
            "human_review_required": True,
            "reason": "Operation requires human approval",
        },
        "approval_signature": "sig-1",
    }


def test_write_authorization_pauses_before_receipt_claim():
    receipt_store = ReceiptStore()
    executor_calls = []

    async def execute_step(**kwargs):
        executor_calls.append(kwargs["step"].step_id)
        return ExecuteResult(
            status=ExecutionStatus.SUCCESS,
            result={"ok": True},
        )

    async def authorize_step(**_kwargs):
        raise ApprovalRequiredError(
            "Operation requires human approval",
            _review_payload(),
        )

    graph = TaskGraph(
        spec=TaskSpec(task_id="task-approval"),
        steps=[
            TaskStep(
                step_id="write-1",
                operation_mode="write",
                preferred_resource_id="A",
            )
        ],
    )
    scheduler = TaskScheduler(
        execute_step=execute_step,
        authorize_step=authorize_step,
        receipt_store=receipt_store,
    )
    result = asyncio.run(
        scheduler.run(graph, context={"task_id": "task-approval"})
    )

    assert result.terminal_status == WorkflowStatus.APPROVAL_REQUIRED
    assert result["write-1"].metrics["approval_required"] is True
    assert executor_calls == []
    assert receipt_store._receipts == {}


def test_runtime_emits_approval_and_governance_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    monkeypatch.setenv("ARTIFACT_PAYLOAD_STORE_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))

    async def execute_step(**_kwargs):
        raise AssertionError("executor must not run while approval is pending")

    authorization_contexts = []

    async def authorize_step(**_kwargs):
        authorization_contexts.append(_kwargs["context"])
        raise ApprovalRequiredError(
            "Operation requires human approval",
            _review_payload(),
        )

    graph = TaskGraph(
        spec=TaskSpec(task_id="task-approval"),
        steps=[
            TaskStep(
                step_id="write-1",
                operation_mode="write",
                preferred_resource_id="A",
                agent_name="A",
            )
        ],
    )
    state = {
        "workflow_id": "wf-approval",
        "user_id": "u1",
        "task_graph": graph,
        "messages": [],
    }

    async def collect():
        return [
            event
            async for event in run_scheduler_workflow(
                state,
                task_id="task-approval",
                execute_step=execute_step,
                authorize_step=authorize_step,
                routing_provider=StubRoutingProvider(),
            )
        ]

    events = asyncio.run(collect())
    assert any(event["event"] == "approval_required" for event in events)
    terminal = events[-1]
    assert terminal["event"] == "end_of_workflow"
    assert terminal["data"]["status"] == "APPROVAL_REQUIRED"
    assert terminal["data"]["approval_required_steps"] == ["write-1"]

    approvals = get_approval_store().list(
        status="pending", task_id="task-approval"
    )
    assert len(approvals) == 1
    assert approvals[0]["step_id"] == "write-1"
    assert approvals[0]["resume_step"] == 1
    assert state["task_id"] == "task-approval"
    assert authorization_contexts[0]["execution_context"].metadata["task_id"] == "task-approval"

    timeline = GovernanceEventStore(tmp_path / "governance").list(
        "task-approval"
    )
    event_types = [event["event_type"] for event in timeline]
    assert "WORKFLOW_STARTED" in event_types
    assert "APPROVAL_REQUIRED" in event_types
    assert "WORKFLOW_TERMINATED" in event_types


def test_runtime_persists_uncertain_side_effect_for_manual_reconciliation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RECONCILIATION_STORE_DIR", str(tmp_path / "reconciliations")
    )
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    monkeypatch.setenv("ARTIFACT_PAYLOAD_STORE_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))

    async def execute_step(**_kwargs):
        return ExecuteResult(
            status=ExecutionStatus.FAILED,
            error="remote response lost after dispatch",
        )

    graph = TaskGraph(
        spec=TaskSpec(task_id="task-reconciliation"),
        steps=[
            TaskStep(
                step_id="write-1",
                operation_mode="write",
                preferred_resource_id="A",
                agent_name="A",
            )
        ],
    )
    state = {
        "workflow_id": "wf-reconciliation",
        "user_id": "u1",
        "task_graph": graph,
        "messages": [],
    }

    async def collect():
        return [
            event
            async for event in run_scheduler_workflow(
                state,
                task_id="task-reconciliation",
                execute_step=execute_step,
                routing_provider=StubRoutingProvider(),
            )
        ]

    events = asyncio.run(collect())
    reconciliation_events = [
        event for event in events
        if event["event"] == "reconciliation_required"
    ]
    assert len(reconciliation_events) == 1
    assert events[-1]["data"]["status"] == "NEEDS_RECONCILIATION"

    queued = get_reconciliation_store().list(
        status="pending", task_id="task-reconciliation"
    )
    assert len(queued) == 1
    assert queued[0]["step_id"] == "write-1"
    assert queued[0]["idempotency_key"]
    assert queued[0]["claim_id"]


def test_approved_request_is_consumed_once(tmp_path, monkeypatch):
    import src.security.enforcement as enforcement

    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path / "approvals"))
    monkeypatch.setattr(enforcement, "S_ABAC_ENABLED", True)
    review_result = {
        "allowed": False,
        "decision": "REVIEW_REQUIRED",
        "human_review_required": True,
        "reason": "Operation requires human approval",
    }
    monkeypatch.setattr(
        enforcement,
        "get_policy_engine",
        lambda: SimpleNamespace(evaluate=lambda *_args: dict(review_result)),
    )

    subject = Subject("user", "u1", {})
    object_ = Object("agent", "A", {"requires_approval": True})
    scenario = Scenario()
    action = Action("dispatch", {"action_type": "write"})
    context = ExecutionContext(
        user_id="u1",
        workflow_id="wf-1",
        metadata={"task_id": "task-1", "step_id": "write-1"},
    )

    with pytest.raises(ApprovalRequiredError) as first:
        enforcement._enforce(
            subject, object_, scenario, action, context=context
        )

    payload = first.value.payload
    approval = get_approval_store().create(
        user_id="u1",
        workflow_id="wf-1",
        task_id="task-1",
        resume_step=1,
        node_name="A",
        step_id="write-1",
        subject=payload["subject"],
        object=payload["object"],
        scenario=payload["scenario"],
        action=payload["action"],
        policy_result=payload["policy_result"],
    )
    get_approval_store().approve(approval.approval_id, approver="admin")

    allowed = enforcement._enforce(
        subject, object_, scenario, action, context=context
    )
    assert allowed["allowed"] is True
    assert allowed["decision"] == "ALLOW_APPROVED"
    assert allowed["approval_id"] == approval.approval_id
    assert get_approval_store().get(approval.approval_id).status == "consumed"

    with pytest.raises(ApprovalRequiredError):
        enforcement._enforce(
            subject, object_, scenario, action, context=context
        )


def test_approved_request_is_consumed_once_across_store_instances(tmp_path):
    base_dir = tmp_path / "approvals"
    creator = ApprovalStore(base_dir)
    approval = creator.create(
        user_id="u1", workflow_id="wf-1", task_id="task-concurrent",
        resume_step=1, node_name="A", subject={"id": "u1"},
        object={"id": "A"}, scenario={}, action={"verb": "dispatch"},
        policy_result={"decision": "REVIEW_REQUIRED"},
    )
    creator.approve(approval.approval_id, approver="admin")
    signature = approval.signature
    stores = [ApprovalStore(base_dir), ApprovalStore(base_dir)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda store: store.consume_if_approved(
                task_id="task-concurrent", signature=signature
            ),
            stores,
        ))

    assert sum(result is not None for result in results) == 1
    assert creator.get(approval.approval_id).status == "consumed"


def test_concurrent_approval_creation_never_overwrites_another_task(
    tmp_path, monkeypatch
):
    import src.security.approval as approval_module

    class FixedDatetime:
        @classmethod
        def now(cls):
            return SimpleNamespace(
                isoformat=lambda: "2026-08-03T12:00:00",
                timestamp=lambda: 1785729600.0,
            )

    # Reproduce the old collision condition: identical policy signature and
    # exactly the same clock millisecond, but two different tasks/stores.
    monkeypatch.setattr(approval_module, "datetime", FixedDatetime)
    base_dir = tmp_path / "approvals"
    stores = [ApprovalStore(base_dir), ApprovalStore(base_dir)]

    def create(item):
        index, store = item
        return store.create(
            user_id="u1",
            workflow_id=f"wf-{index}",
            task_id=f"task-{index}",
            resume_step=1,
            node_name="A",
            subject={"id": "u1"},
            object={"id": "A"},
            scenario={},
            action={"verb": "dispatch"},
            policy_result={"decision": "REVIEW_REQUIRED"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = list(executor.map(create, enumerate(stores, start=1)))

    assert requests[0].approval_id != requests[1].approval_id
    persisted = ApprovalStore(base_dir).list()
    assert {request["task_id"] for request in persisted} == {"task-1", "task-2"}
    assert len(list(base_dir.glob("approval_*.json"))) == 2


def test_governance_event_store_filters_events(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "GOVERNANCE_EVENT_STORE_DIR", str(tmp_path / "governance")
    )
    record_governance_event(
        "STEP_STARTED",
        task_id="task-x",
        workflow_id="wf-x",
        step_id="s1",
    )
    record_governance_event(
        "STEP_FAILED",
        task_id="task-x",
        workflow_id="wf-x",
        step_id="s2",
    )

    store = GovernanceEventStore(tmp_path / "governance")
    assert len(store.list("task-x")) == 2
    assert [
        event["step_id"]
        for event in store.list("task-x", event_type="STEP_FAILED")
    ] == ["s2"]
