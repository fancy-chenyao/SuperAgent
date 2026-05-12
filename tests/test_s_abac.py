import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from src.security.approval import ApprovalStore
from src.security.context import SecurityContextBuilder, UnknownSecurityUserError
from src.security.enforcement import PermissionDeniedError, enforce_tool_call
from src.security.policy import Action, Object, PolicyEngine, Scenario, Subject
from src.security.scenario_analyzer import analyze_object_fit, analyze_task_context


def test_policy_engine_allows_low_sensitivity_by_clearance():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject(
            "agent",
            "researcher",
            {
                "role": "ResearchAgent",
                "job_role": "research_analyst",
                "clearance_level": 2,
                "grants": ["research_read"],
            },
        ),
        Object(
            "tool",
            "search",
            {
                "sensitivity": "LOW",
                "allowed_job_roles": ["research_analyst"],
                "expected_capabilities": ["Research"],
                "scenario_tags": ["market_research"],
                "allowed_operation_modes": ["call", "read"],
            },
        ),
        Scenario(
            task_scenario={
                "task_type": "RESEARCH",
                "risk_profile": "LOW",
                "scenario_tags": ["market_research"],
                "expected_capabilities": ["Research"],
            }
        ),
        Action("execute", {"action_type": "call"}),
    )
    assert result["allowed"] is True
    assert result["human_review_required"] is False


def test_policy_engine_requires_review_for_insufficient_clearance():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject(
            "agent",
            "low",
            {
                "role": "HRAgent",
                "job_role": "hr_manager",
                "clearance_level": 1,
                "grants": ["salary_read"],
            },
        ),
        Object(
            "tool",
            "salary",
            {
                "sensitivity": "HIGH",
                "allowed_job_roles": ["hr_manager"],
                "expected_capabilities": ["HR"],
                "scenario_tags": ["salary_query"],
                "allowed_operation_modes": ["call", "read"],
                "requires_approval": True,
            },
        ),
        Scenario(
            task_scenario={
                "task_type": "HR",
                "risk_profile": "LOW",
                "scenario_tags": ["salary_query"],
                "expected_capabilities": ["HR"],
            }
        ),
        Action("execute", {"action_type": "call"}),
    )
    assert result["allowed"] is False
    assert result["human_review_required"] is True
    assert result["approval_level"] in {"MEDIUM", "HIGH"}


def test_policy_engine_denies_job_role_mismatch():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject(
            "agent",
            "researcher",
            {
                "role": "ResearchAgent",
                "job_role": "research_analyst",
                "clearance_level": 4,
                "grants": ["research_read"],
            },
        ),
        Object(
            "tool",
            "salary",
            {
                "sensitivity": "HIGH",
                "allowed_job_roles": ["hr_manager"],
                "expected_capabilities": ["HR"],
                "scenario_tags": ["salary_query"],
            },
        ),
        Scenario(
            task_scenario={
                "task_type": "HR",
                "risk_profile": "LOW",
                "scenario_tags": ["salary_query"],
                "expected_capabilities": ["HR"],
            }
        ),
        Action("execute", {"action_type": "call"}),
    )
    assert result["allowed"] is False
    assert result["human_review_required"] is False


def test_policy_engine_irreversible_operation_requires_review():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject(
            "agent",
            "comm",
            {
                "role": "CommunicationAgent",
                "job_role": "communication_officer",
                "clearance_level": 4,
                "grants": ["external_send"],
            },
        ),
        Object(
            "tool",
            "email",
            {
                "sensitivity": "MEDIUM",
                "allowed_job_roles": ["communication_officer"],
                "expected_capabilities": ["Communication"],
                "scenario_tags": ["notification_send"],
                "allowed_operation_modes": ["send"],
            },
        ),
        Scenario(
            task_scenario={
                "task_type": "COMMUNICATION",
                "risk_profile": "LOW",
                "scenario_tags": ["notification_send"],
                "expected_capabilities": ["Communication"],
            }
        ),
        Action("execute", {"action_type": "send", "irreversible": True}),
    )
    assert result["allowed"] is False
    assert result["human_review_required"] is True


def test_policy_engine_denies_mismatched_task_scenario():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject(
            "user",
            "communication_officer",
            {
                "role": "CommunicationAgent",
                "job_role": "communication_officer",
                "clearance_level": 3,
                "grants": ["external_send"],
            },
        ),
        Object(
            "tool",
            "remote_salary_info_tool",
            {
                "sensitivity": "HIGH",
                "allowed_job_roles": ["hr_manager"],
                "expected_capabilities": ["HR"],
                "scenario_tags": ["salary_query"],
                "allowed_operation_modes": ["call", "read"],
            },
        ),
        Scenario(
            task_scenario={
                "task_type": "COMMUNICATION",
                "risk_profile": "LOW",
                "scenario_tags": ["notification_send"],
                "expected_capabilities": ["Communication"],
            }
        ),
        Action("execute", {"action_type": "call"}),
    )
    assert result["allowed"] is False
    assert "capabilities" in result["reason"] or "tags" in result["reason"] or "Scenario" in result["reason"]


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
    assert subject.attributes["job_role"] == "hr_service_agent"
    assert tool_object.attributes["owner_agent"] == "RemoteHRAssistantAgent"
    assert tool_object.attributes["sensitivity"] == "HIGH"
    assert "HR" in tool_object.attributes["expected_capabilities"]
    assert action.attributes["amount"] == 200000


def test_scenario_analyzer_heuristic_task_profile():
    profile = __import__("asyncio").run(
        analyze_task_context("Please send a batch notification email to all employees")
    )
    assert profile["task_type"] == "COMMUNICATION"
    assert "Communication" in profile["expected_capabilities"]
    assert "mass_notification" in profile["scenario_tags"]


def test_scenario_analyzer_detects_chinese_hr_salary_query():
    profile = __import__("asyncio").run(analyze_task_context("查询员工 E001 的工资信息"))
    assert profile["task_type"] == "HR"
    assert "HR" in profile["expected_capabilities"]
    assert "salary_query" in profile["scenario_tags"]


def test_scenario_analyzer_detects_object_fit_mismatch():
    fit = __import__("asyncio").run(
        analyze_object_fit(
            "Please send a batch notification email to all employees",
            object_id="remote_salary_info_tool",
            object_type="tool",
            object_attrs={
                "expected_capabilities": ["HR"],
                "scenario_tags": ["salary_query"],
            },
            task_profile={
                "task_type": "COMMUNICATION",
                "expected_capabilities": ["Communication"],
                "scenario_tags": ["mass_notification"],
            },
        )
    )
    assert fit["fit"] == "mismatch"


def test_scenario_from_context_prefers_task_profile_over_runtime_text():
    context = SimpleNamespace(
        workflow_mode="execution",
        metadata={
            "USER_QUERY": "请确认并执行既定计划",
            "business_goal": "确认并执行既定计划",
            "task_profile": {
                "task_type": "HR",
                "business_goal": "查询员工 E001 的工资信息",
                "data_scope": "targeted",
                "operation_mode": "read",
                "scenario_tags": ["salary_query", "employee_info"],
                "expected_capabilities": ["HR"],
                "risk_profile": "LOW",
            },
        },
    )
    scenario = SecurityContextBuilder.scenario_from_context(context)
    assert scenario.task_scenario["task_type"] == "HR"
    assert scenario.task_scenario["business_goal"] == "查询员工 E001 的工资信息"
    assert "HR" in scenario.task_scenario["expected_capabilities"]


def test_subject_for_unknown_user_raises_explicit_error():
    try:
        SecurityContextBuilder.subject_for_user("test")
    except UnknownSecurityUserError as exc:
        assert "Unknown S-ABAC demo user" in str(exc)
    else:
        raise AssertionError("Expected UnknownSecurityUserError for unknown demo user")


def test_enforcement_populates_scenario_fit_result_in_context():
    class DummyContext:
        def __init__(self):
            self.user_id = "communication_officer"
            self.workflow_id = "wf-1"
            self.workflow_mode = "production"
            self.metadata = {
                "USER_QUERY": "Please send a batch notification email to all employees",
                "task_profile": {
                    "task_type": "COMMUNICATION",
                    "expected_capabilities": ["Communication"],
                    "scenario_tags": ["mass_notification", "notification_send"],
                    "operation_mode": "send",
                    "risk_profile": "LOW",
                },
                "scenario_fit_cache": {},
                "operation_mode": "send",
                "scenario_tags": ["mass_notification", "notification_send"],
                "expected_capabilities": ["Communication"],
                "risk_profile": "LOW",
                "network_zone": "internal",
                "time": "working_hours",
            }

    context = DummyContext()
    agent = SimpleNamespace(agent_name="RemoteCommunicationAgent")
    try:
        __import__("asyncio").run(
            enforce_tool_call(
                agent=agent,
                tool_name="remote_email_tool",
                arguments={"subject": "Notice", "body": "Hello"},
                context=context,
            )
        )
    except PermissionDeniedError:
        pass
    fit_result = context.metadata.get("scenario_fit_result", {})
    assert fit_result
    assert fit_result["fit"] in {"match", "uncertain"}


def test_permission_payload_contains_scenario_fit_result():
    class DummyContext:
        def __init__(self):
            self.user_id = "communication_officer"
            self.workflow_id = "wf-2"
            self.workflow_mode = "production"
            self.metadata = {
                "USER_QUERY": "Please send a batch notification email to all employees",
                "task_profile": {
                    "task_type": "COMMUNICATION",
                    "expected_capabilities": ["Communication"],
                    "scenario_tags": ["mass_notification", "notification_send"],
                    "operation_mode": "send",
                    "risk_profile": "LOW",
                },
                "scenario_fit_cache": {},
                "operation_mode": "send",
                "scenario_tags": ["mass_notification", "notification_send"],
                "expected_capabilities": ["Communication"],
                "risk_profile": "LOW",
                "network_zone": "external",
                "time": "working_hours",
            }

    context = DummyContext()
    agent = SimpleNamespace(agent_name="RemoteCommunicationAgent")
    try:
        __import__("asyncio").run(
            enforce_tool_call(
                agent=agent,
                tool_name="remote_email_tool",
                arguments={"subject": "Notice", "body": "Hello"},
                context=context,
            )
        )
    except PermissionDeniedError as exc:
        scenario_fit = (
            exc.payload.get("scenario", {})
            .get("task_scenario", {})
            .get("scenario_fit_result", {})
        )
        assert scenario_fit
        assert scenario_fit["fit"] in {"match", "uncertain"}
    else:
        raise AssertionError("Expected PermissionDeniedError")
