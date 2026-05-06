import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from src.security.approval import ApprovalStore
from src.security.context import SecurityContextBuilder
from src.security.policy import Action, Object, PolicyEngine, Scenario, Subject


def test_policy_engine_allows_low_sensitivity_by_clearance():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject("agent", "researcher", {"role": "ResearchAgent", "clearance_level": 2}),
        Object("tool", "search", {"sensitivity": "LOW"}),
        Scenario(task_scenario={"risk_profile": "LOW"}),
        Action("execute", {"action_type": "call"}),
    )
    assert result["allowed"] is True
    assert result["human_review_required"] is False


def test_policy_engine_requires_review_for_insufficient_clearance():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject("agent", "low", {"role": "ResearchAgent", "clearance_level": 1}),
        Object("tool", "salary", {"sensitivity": "HIGH"}),
        Scenario(task_scenario={"risk_profile": "LOW"}),
        Action("execute", {"action_type": "call"}),
    )
    assert result["allowed"] is False
    assert result["human_review_required"] is True
    assert result["approval_level"] in {"MEDIUM", "HIGH"}


def test_policy_engine_denies_role_mismatch():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject("agent", "researcher", {"role": "ResearchAgent", "clearance_level": 4}),
        Object("tool", "salary", {"sensitivity": "HIGH", "allowed_roles": ["HRAgent"]}),
        Scenario(task_scenario={"risk_profile": "LOW"}),
        Action("execute", {"action_type": "call"}),
    )
    assert result["allowed"] is False
    assert result["human_review_required"] is False


def test_policy_engine_irreversible_operation_requires_review():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject("agent", "comm", {"role": "CommunicationAgent", "clearance_level": 4}),
        Object("tool", "email", {"sensitivity": "MEDIUM"}),
        Scenario(task_scenario={"risk_profile": "LOW"}),
        Action("execute", {"action_type": "call", "irreversible": True}),
    )
    assert result["allowed"] is False
    assert result["human_review_required"] is True


def test_approval_store_approve_and_consume_once():
    store_path = Path("store") / f"approvals_test_{uuid4().hex}"
    store = ApprovalStore(store_path)
    subject = {"id": "agent", "subject_type": "agent", "attributes": {}}
    object_ = {"id": "tool", "object_type": "tool", "attributes": {}}
    action = {"verb": "execute", "attributes": {"action_type": "call"}}
    approval = store.create(
        user_id="test",
        workflow_id="test:wf",
        task_id="task-1",
        resume_step=3,
        node_name="agent_proxy",
        subject=subject,
        object=object_,
        scenario={},
        action=action,
        policy_result={"human_review_required": True},
    )
    approved = store.approve(approval.approval_id, approver="alice")
    assert approved.status == "approved"

    signature = store.signature(subject, object_, action)
    consumed = store.consume_if_approved(task_id="task-1", signature=signature)
    assert consumed is not None
    assert store.consume_if_approved(task_id="task-1", signature=signature) is None
    shutil.rmtree(store_path, ignore_errors=True)


def test_approval_store_finds_rejected_decision():
    store_path = Path("store") / f"approvals_test_{uuid4().hex}"
    store = ApprovalStore(store_path)
    subject = {"id": "agent", "subject_type": "agent", "attributes": {}}
    object_ = {"id": "tool", "object_type": "tool", "attributes": {}}
    action = {"verb": "execute", "attributes": {"action_type": "call"}}
    approval = store.create(
        user_id="test",
        workflow_id="test:wf",
        task_id="task-2",
        resume_step=4,
        node_name="agent_proxy",
        subject=subject,
        object=object_,
        scenario={},
        action=action,
        policy_result={"human_review_required": True},
    )
    rejected = store.reject(approval.approval_id, approver="bob", comment="not allowed")
    signature = store.signature(subject, object_, action)

    assert rejected.status == "rejected"
    assert store.find_latest(task_id="task-2", signature=signature, statuses=["rejected"]) is not None
    shutil.rmtree(store_path, ignore_errors=True)


def test_security_context_builder_maps_agent_and_tool():
    agent = SimpleNamespace(agent_name="RemoteHRAssistantAgent")
    subject = SecurityContextBuilder.subject_for_agent(agent)
    tool_object = SecurityContextBuilder.object_for_tool("remote_salary_info_tool")
    action = SecurityContextBuilder.action_for_tool_call(
        "remote_salary_info_tool",
        {"employee_id": "001", "amount": 200000},
    )

    assert subject.attributes["role"] == "HRAgent"
    assert tool_object.attributes["sensitivity"] == "HIGH"
    assert action.attributes["amount"] == 200000
