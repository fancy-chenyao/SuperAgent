import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from collections import deque

import src.security.enforcement as enforcement
import src.security.scenario_analyzer as scenario_analyzer
from src.security.approval import ApprovalStore
from src.security.context import SecurityContextBuilder, UnknownSecurityUserError
from src.security.enforcement import PermissionDeniedError, enforce_tool_call
from src.security.policy import Action, Object, PolicyEngine, Scenario, Subject
from src.security.scenario_analyzer import analyze_object_fit, analyze_task_context
from src.workflow.coor_task import _extract_plan_steps, _fallback_plan_steps
from src.workflow.cache import WorkflowCache


def _raise_llm_unavailable(*_args, **_kwargs):
    raise RuntimeError("LLM disabled for deterministic offline test")


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


def test_policy_engine_denies_strong_mismatched_task_scenario():
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
    assert "capabilities" in result["reason"] or "object provides" in result["reason"]


def test_policy_engine_uncertain_high_sensitivity_requires_review():
    engine = PolicyEngine(policies=[])
    result = engine.evaluate(
        Subject(
            "user",
            "hr_manager",
            {
                "role": "HRAgent",
                "job_role": "hr_manager",
                "clearance_level": 3,
                "grants": ["salary_read"],
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
                "task_type": "HR",
                "risk_profile": "LOW",
                "scenario_tags": ["general"],
                "expected_capabilities": ["General"],
                "scenario_fit_result": {
                    "fit": "uncertain",
                    "reason": "Scenario evidence is weak",
                },
            }
        ),
        Action("execute", {"action_type": "call"}),
    )
    assert result["allowed"] is False
    assert result["human_review_required"] is True
    assert result["decision"] == "REVIEW_REQUIRED"


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


def test_scenario_analyzer_heuristic_task_profile(monkeypatch):
    monkeypatch.setattr(
        scenario_analyzer, "get_llm_by_type", _raise_llm_unavailable
    )
    profile = __import__("asyncio").run(
        analyze_task_context("Please send a batch notification email to all employees")
    )
    assert profile["task_type"] == "COMMUNICATION"
    assert "Communication" in profile["expected_capabilities"]
    assert "mass_notification" in profile["scenario_tags"]


def test_scenario_analyzer_detects_chinese_hr_salary_query(monkeypatch):
    monkeypatch.setattr(
        scenario_analyzer, "get_llm_by_type", _raise_llm_unavailable
    )
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


def test_scenario_analyzer_keeps_weak_overlap_uncertain():
    fit = __import__("asyncio").run(
        analyze_object_fit(
            "请确认并执行当前计划",
            object_id="RemoteHRAssistantAgent",
            object_type="agent",
            object_attrs={
                "capability_domain": "HR",
                "department_domain": "HR",
                "expected_capabilities": ["HR"],
                "scenario_tags": ["employee_info", "salary_query"],
                "sensitivity": "HIGH",
            },
            task_profile={
                "task_type": "GENERAL",
                "expected_capabilities": ["General"],
                "scenario_tags": ["general"],
            },
        )
    )
    assert fit["fit"] in {"uncertain", "mismatch"}


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


def test_production_state_should_keep_original_hr_task_profile():
    state = {
        "workflow_mode": "production",
        "USER_QUERY": "确认执行，按当前计划执行。",
        "task_profile": {
            "task_type": "HR",
            "business_goal": "查询员工 E001 的工资信息",
            "data_scope": "targeted",
            "operation_mode": "read",
            "scenario_tags": ["salary_query", "employee_info"],
            "expected_capabilities": ["HR"],
            "risk_profile": "LOW",
        },
        "business_goal": "查询员工 E001 的工资信息",
        "scenario_tags": ["salary_query", "employee_info"],
        "expected_capabilities": ["HR"],
        "operation_mode": "read",
        "risk_profile": "LOW",
    }
    scenario = SecurityContextBuilder.scenario_from_context(SimpleNamespace(metadata=state, workflow_mode="production"))
    assert scenario.task_scenario["task_type"] == "HR"
    assert scenario.task_scenario["operation_mode"] == "read"
    assert scenario.task_scenario["business_goal"] == "查询员工 E001 的工资信息"


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


def test_permission_payload_contains_scenario_fit_result(monkeypatch):
    # enforcement freezes S_ABAC_ENABLED at import time; pin it ON here so
    # the deny path is exercised even without a .env (fresh clone / CI).
    monkeypatch.setattr(enforcement, "S_ABAC_ENABLED", True)

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


def test_scenario_analyzer_preserves_hr_profile_over_llm_general(monkeypatch):
    class DummyStructured:
        async def ainvoke(self, _messages):
            class Result:
                def model_dump(self):
                    return {
                        "task_type": "GENERAL",
                        "business_goal": "confirm and execute plan",
                        "data_scope": "targeted",
                        "operation_mode": "read",
                        "scenario_tags": ["general"],
                        "expected_capabilities": ["General"],
                        "risk_profile": "LOW",
                        "reason": "llm downgrade",
                    }

            return Result()

    class DummyLLM:
        def with_structured_output(self, _model):
            return DummyStructured()

    monkeypatch.setattr(scenario_analyzer, "get_llm_by_type", lambda *_args, **_kwargs: DummyLLM())

    profile = __import__("asyncio").run(analyze_task_context("查询员工 E001 的工资信息"))
    assert profile["task_type"] == "HR"
    assert "HR" in profile["expected_capabilities"]
    assert "salary_query" in profile["scenario_tags"]


def test_scenario_analyzer_merges_coarse_and_fine_labels_in_order():
    fallback = {
        "task_type": "HR",
        "expected_capabilities": ["HR"],
        "scenario_tags": ["salary_query"],
    }
    llm_result = {
        "task_type": "HR",
        "expected_capabilities": ["HR_DATA_ACCESS"],
        "scenario_tags": ["employee_compensation_access"],
    }

    merged = scenario_analyzer._merge_task_profile(fallback, llm_result)

    assert merged["expected_capabilities"] == ["HR", "HR_DATA_ACCESS"]
    assert merged["scenario_tags"] == [
        "salary_query",
        "employee_compensation_access",
    ]


def test_scenario_analyzer_merges_labels_case_insensitively():
    fallback = {
        "task_type": "HR",
        "expected_capabilities": ["HR"],
        "scenario_tags": ["salary_query"],
    }
    llm_result = {
        "task_type": "HR",
        "expected_capabilities": ["hr", " HR_DATA_ACCESS ", ""],
        "scenario_tags": ["SALARY_QUERY", "employee_info"],
    }

    merged = scenario_analyzer._merge_task_profile(fallback, llm_result)

    assert merged["expected_capabilities"] == ["HR", "HR_DATA_ACCESS"]
    assert merged["scenario_tags"] == ["salary_query", "employee_info"]


def test_scenario_analyzer_keeps_conflicting_domain_protection_before_union():
    fallback = {
        "task_type": "HR",
        "business_goal": "查询工资",
        "expected_capabilities": ["HR"],
        "scenario_tags": ["salary_query"],
    }
    llm_result = {
        "task_type": "COMMUNICATION",
        "expected_capabilities": ["Communication"],
        "scenario_tags": ["mass_notification"],
    }

    merged = scenario_analyzer._merge_task_profile(fallback, llm_result)

    assert merged["task_type"] == "HR"
    assert merged["expected_capabilities"] == ["HR"]
    assert merged["scenario_tags"] == ["salary_query"]
    assert merged["reason"] == "heuristic domain preserved over conflicting llm result"


def test_scenario_analyzer_preserves_match_over_llm_mismatch():
    fallback = {
        "fit": "match",
        "confidence": 0.6,
        "reason": "Capability domain matches task profile",
    }
    llm_result = {
        "fit": "mismatch",
        "confidence": 0.91,
        "reason": "Execution-focused wording does not match HR domain",
    }

    merged = scenario_analyzer._merge_fit_result(fallback, llm_result)
    assert merged["fit"] == "match"
    assert "Capability domain matches" in merged["reason"]


def test_scenario_analyzer_promotes_uncertain_positive_reason_to_match():
    fallback = {
        "fit": "uncertain",
        "confidence": 0.35,
        "reason": "heuristic fallback",
    }
    llm_result = {
        "fit": "uncertain",
        "confidence": 0.5,
        "reason": "The task scenario aligns with the target agent's responsibility domain.",
    }

    merged = scenario_analyzer._merge_fit_result(fallback, llm_result)
    assert merged["fit"] == "match"


def test_enforcement_uses_business_goal_for_object_fit_query():
    class DummyContext:
        def __init__(self):
            self.user_id = "hr_manager"
            self.workflow_id = "wf-hr-1"
            self.workflow_mode = "production"
            self.metadata = {
                "USER_QUERY": "确认并执行既定计划",
                "business_goal": "查询员工 E001 的工资信息",
                "task_profile": {
                    "task_type": "HR",
                    "business_goal": "查询员工 E001 的工资信息",
                    "expected_capabilities": ["HR"],
                    "scenario_tags": ["salary_query", "employee_info"],
                    "operation_mode": "read",
                    "risk_profile": "LOW",
                },
                "scenario_fit_cache": {},
                "operation_mode": "read",
                "scenario_tags": ["salary_query", "employee_info"],
                "expected_capabilities": ["HR"],
                "risk_profile": "LOW",
                "network_zone": "internal",
                "time": "working_hours",
            }

    captured = {}

    async def fake_analyze_object_fit(user_query, **kwargs):
        captured["user_query"] = user_query
        return {
            "fit": "match",
            "confidence": 0.7,
            "reason": "hr scenario match",
        }

    original = scenario_analyzer.analyze_object_fit
    import src.security.enforcement as enforcement_module
    original_enforcement = enforcement_module.analyze_object_fit
    scenario_analyzer.analyze_object_fit = fake_analyze_object_fit
    enforcement_module.analyze_object_fit = fake_analyze_object_fit
    try:
        context = DummyContext()
        agent = SimpleNamespace(agent_name="RemoteHRAssistantAgent")
        __import__("asyncio").run(
            enforce_tool_call(
                agent=agent,
                tool_name="remote_salary_info_tool",
                arguments={"employee_id": "E001"},
                context=context,
            )
        )
    finally:
        scenario_analyzer.analyze_object_fit = original
        enforcement_module.analyze_object_fit = original_enforcement

    assert captured["user_query"] == "查询员工 E001 的工资信息"


def test_scenario_from_context_prefers_original_user_query_for_execution():
    context = SimpleNamespace(
        workflow_mode="production",
        metadata={
            "USER_QUERY": "确认执行，按当前计划执行。",
            "original_user_query": "查询员工 E001 的工资信息",
            "execution_user_query": "确认执行，按当前计划执行。",
            "task_profile": {
                "task_type": "HR",
                "business_goal": "查询员工 E001 的工资信息",
                "data_scope": "targeted",
                "operation_mode": "read",
                "scenario_tags": ["salary_query", "employee_info"],
                "expected_capabilities": ["HR"],
                "risk_profile": "LOW",
            },
            "business_goal": "查询员工 E001 的工资信息",
            "scenario_tags": ["salary_query", "employee_info"],
            "expected_capabilities": ["HR"],
            "operation_mode": "read",
            "risk_profile": "LOW",
        },
    )
    scenario = SecurityContextBuilder.scenario_from_context(context)
    assert scenario.task_scenario["goal"] == "查询员工 E001 的工资信息"
    assert scenario.task_scenario["business_goal"] == "查询员工 E001 的工资信息"
    assert scenario.task_scenario["task_type"] == "HR"

def test_fallback_plan_steps_generates_coder_step_for_engineering_task():
    state = {
        "task_type": "ENGINEERING",
        "expected_capabilities": ["Engineering"],
        "USER_QUERY": "写一个 Python 脚本，统计当前目录下所有 json 文件数量",
        "TEAM_MEMBERS": ["coder", "researcher", "reporter"],
    }

    steps = _fallback_plan_steps(state)

    assert steps is not None
    assert len(steps) == 1
    assert steps[0]["agent_name"] == "coder"
    assert "python" in steps[0]["title"].lower() or "python" in steps[0]["description"].lower()


def test_extract_plan_steps_accepts_empty_steps_array():
    content = '{"new_agents_needed": [], "steps": []}'
    steps = _extract_plan_steps(content)
    assert steps == []


def test_launch_init_cache_resets_old_planning_state():
    cache_dir = Path(".pytest_tmp_workflow_cache_test")
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache = WorkflowCache(cache_dir)
    workflow_id = "engineer:test_reset"
    user_id = "engineer"

    cache.cache[workflow_id] = {
        "workflow_id": workflow_id,
        "mode": "launch",
        "version": 1,
        "lap": 1,
        "user_input_messages": [{"role": "user", "content": "old"}],
        "deep_thinking_mode": False,
        "search_before_planning": False,
        "coor_agents": [],
        "planning_steps": [{"agent_name": "old_agent"}],
        "graph": [{"name": "old_graph"}],
        "nodes": {"old_agent": {"name": "old_agent"}},
        "instruction_history": [],
    }
    cache.queue[workflow_id] = deque([{"name": "old_queue"}])
    cache._lock_pool[user_id] = cache._lock_pool.get(user_id) or __import__("threading").Lock()

    cache.init_cache(
        user_id=user_id,
        lap=2,
        mode="launch",
        workflow_id=workflow_id,
        version=1,
        user_input_messages=[{"role": "user", "content": "new"}],
        deep_thinking_mode=False,
        search_before_planning=False,
        coor_agents=[],
    )

    workflow = cache.cache[workflow_id]
    assert workflow["planning_steps"] == []
    assert workflow["graph"] == []
    assert workflow["nodes"] == {}
    assert list(cache.queue.get(workflow_id, deque())) == []
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
