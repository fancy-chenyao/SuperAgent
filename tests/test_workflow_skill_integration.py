import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.security.enforcement import PermissionDeniedError
from src.skills.workflow_skill import (
    WorkflowSkillManager,
    WorkflowSkillSettings,
    WorkflowSkillStatus,
    WorkflowSkillStore,
    set_workflow_skill_manager,
)
from src.workflow.graph import CompiledWorkflow


def _manager(tmp_path, **overrides):
    values = {
        "enabled": True,
        "reuse_enabled": True,
        "auto_distill_enabled": True,
        "match_threshold": 0.45,
        "match_margin": 0.05,
        "promotion_success_threshold": 2,
        "failure_disable_threshold": 2,
        "store_path": tmp_path / "workflow-skills.sqlite3",
    }
    values.update(overrides)
    settings = WorkflowSkillSettings(**values)
    return WorkflowSkillManager(settings=settings, store=WorkflowSkillStore(settings.store_path))


def _leave_profile():
    return {
        "task_type": "HR",
        "business_goal": "Submit employee leave",
        "data_scope": "self",
        "operation_mode": "write",
        "scenario_tags": ["leave_request", "hr_service"],
        "expected_capabilities": ["leave management"],
        "risk_profile": "MEDIUM",
        "reason": "test profile",
    }


class _FakeCache:
    def __init__(self, workflow_id="alice:wf"):
        self.steps = []
        self.cache = {workflow_id: {"planning_steps": [], "graph": [], "nodes": {}}}
        self.updated = False

    def restore_planning_steps(self, workflow_id, steps, user_id):
        self.steps = steps
        self.cache[workflow_id]["planning_steps"] = steps

    def get_planning_steps(self, workflow_id):
        return self.steps

    def restore_system_node(self, workflow_id, node, user_id):
        return None

    def update_stack(self, workflow_id, user_id):
        self.updated = True

    def dump(self, workflow_id, mode):
        return None


class _FakeTaskLogger:
    def __init__(self, task_id, workflow_id, user_query=""):
        self.task_id = task_id
        self.workflow_id = workflow_id
        self.user_query = user_query
        self.history = []
        self.status = "running"
        self.execution_phase = "initial_planning"
        self.planning_steps = []
        self.task_profile = {}

    def set_execution_phase(self, phase):
        self.execution_phase = phase

    def set_workflow_snapshot(self, planning_steps, task_profile=None):
        self.planning_steps = list(planning_steps)
        self.task_profile = dict(task_profile or {})

    def log_workflow_start(self, user_query=""):
        self.history.append({"event": "workflow_start"})

    def log_workflow_end(self):
        self.status = "completed"
        self.history.append({"event": "workflow_end"})

    def log_agent_start(self, **kwargs):
        self.history.append({"event": "start_of_agent", **kwargs})

    def log_agent_end(self, **kwargs):
        self.history.append({"event": "end_of_agent", **kwargs})

    def log_message(self, **kwargs):
        self.history.append({"event": "message", **kwargs})

    def log_error(self, **kwargs):
        self.status = "failed"
        self.history.append({"event": "error", **kwargs})


class _FakeCheckpointManager:
    def save_checkpoint(self, **kwargs):
        return SimpleNamespace(**kwargs)


def test_leave_launch_reuses_plan_without_coordinator_or_planner_llm(tmp_path, monkeypatch):
    import src.workflow.coor_task as coor_task
    import src.workflow.process as process

    manager = _manager(tmp_path)
    card = manager.bootstrap_leave_request("alice")
    manager.store.activate("alice", card.skill_id)
    fake_cache = _FakeCache()
    llm_calls = []

    async def scenario_profile(_query, _metadata):
        return _leave_profile()

    def forbidden_llm(kind):
        llm_calls.append(kind)
        raise AssertionError(f"LLM should not be called for matched workflow skill: {kind}")

    monkeypatch.setattr(process, "cache", fake_cache)
    monkeypatch.setattr(coor_task, "cache", fake_cache)
    monkeypatch.setattr(process, "get_workflow_skill_manager", lambda: manager)
    monkeypatch.setattr(process, "analyze_task_context", scenario_profile)
    monkeypatch.setattr(process, "TaskLogger", _FakeTaskLogger)
    monkeypatch.setattr(process, "CheckpointManager", _FakeCheckpointManager)
    monkeypatch.setattr(process, "AUTO_RECOVERY_ENABLED", False)
    monkeypatch.setattr(process, "get_llm_by_type", lambda _kind: SimpleNamespace())
    monkeypatch.setattr(coor_task, "get_llm_by_type", forbidden_llm)

    workflow = CompiledWorkflow(
        nodes={
            "coordinator": coor_task.coordinator_node,
            "planner": coor_task.planner_node,
        },
        edges={},
        start_node="coordinator",
    )
    query = "I need to request leave from Tuesday to Thursday for a family matter"
    initial_state = {
        "user_id": "alice",
        "TEAM_MEMBERS": ["RemoteHRAssistantAgent", "RemoteOfficeAssistantAgent"],
        "TEAM_MEMBERS_DESCRIPTION": "HR and office assistants",
        "TOOLS": "",
        "RESOURCE_CATALOG": "",
        "USER_QUERY": query,
        "execution_user_query": query,
        "original_user_query": query,
        "messages": [{"role": "user", "content": query}],
        "deep_thinking_mode": False,
        "search_before_planning": False,
        "workflow_id": "alice:wf",
        "workflow_mode": "launch",
        "initialized": False,
        "stop_after_planner": True,
        "instruction_history": [query],
        "memory_session_id": "",
        "memory_context": {},
        "skill_reuse_enabled": True,
        "reused_skill_id": "",
        "reused_skill_owner_id": "",
        "workflow_skill_match": {},
    }

    async def run():
        return [
            event
            async for event in process._process_workflow(
                workflow,
                initial_state,
                task_id="task-launch",
            )
        ]

    events = asyncio.run(run())
    event_names = [event["event"] for event in events]
    assert "skill_matched" in event_names
    assert event_names[-1] == "end_of_workflow"
    assert llm_calls == []
    assert fake_cache.steps[0]["agent_name"] == "RemoteHRAssistantAgent"
    assert fake_cache.steps[1]["agent_name"] == "RemoteOfficeAssistantAgent"
    assert fake_cache.steps[1]["inputs"][0]["source_step"] == "RemoteHRAssistantAgent"
    assert "Tuesday" in fake_cache.steps[0]["description"]


def test_reused_plan_still_passes_agentproxy_authorization(tmp_path, monkeypatch):
    import src.workflow.coor_task as coor_task

    checks = []
    fake_cache = _FakeCache()
    agent = SimpleNamespace(agent_name="reporter")

    class Registry:
        async def get(self, name):
            return agent if name == "reporter" else None

    class AgentManager:
        agent_registry = Registry()

        async def ensure_initialized(self):
            return None

    async def enforce(target, context):
        checks.append((target.agent_name, context.user_id, context.workflow_id))

    execution_status = [ExecutionStatus.SUCCESS]

    async def execute(target, messages, context):
        if execution_status[0] == ExecutionStatus.SUCCESS:
            return ExecuteResult(status=execution_status[0], result="approved")
        return ExecuteResult(status=execution_status[0], error="remote Agent failed")

    monkeypatch.setattr(coor_task, "agent_manager", AgentManager())
    monkeypatch.setattr(coor_task, "enforce_agent_dispatch", enforce)
    monkeypatch.setattr(coor_task, "execute_agent", execute)
    monkeypatch.setattr(coor_task, "cache", fake_cache)

    state = {
        "user_id": "alice",
        "workflow_id": "alice:wf",
        "workflow_mode": "production",
        "next": "reporter",
        "messages": [{"role": "user", "content": "submit leave"}],
        "deep_thinking_mode": False,
        "task_id": "task-production",
        "current_step": 2,
        "workflow_skill_match": {"skill_id": "wskill-1"},
    }
    command = asyncio.run(coor_task.agent_proxy_node(state))
    assert checks == [("reporter", "alice", "alice:wf")]
    assert command.goto == "publisher"
    assert command.update["workflow_execution_failed"] is False
    assert fake_cache.updated is True

    execution_status[0] = ExecutionStatus.FAILED
    failed_command = asyncio.run(coor_task.agent_proxy_node(state))
    assert failed_command.update["workflow_execution_failed"] is True


def test_workflow_skill_backend_api_lifecycle_and_manual_distillation(tmp_path, monkeypatch):
    import src.service.web_app as web_app

    manager = _manager(tmp_path)
    set_workflow_skill_manager(manager)
    monkeypatch.setattr(web_app, "WORKFLOW_SKILL_ADMIN_API_KEY", "test-key")
    headers = {"Authorization": "Bearer test-key"}
    fake_task = SimpleNamespace(
        status="completed",
        execution_phase="execution",
        workflow_id="alice:wf",
        user_query="Please submit my leave request",
        planning_steps=[{"agent_name": "reporter", "description": "Handle leave"}],
        task_profile=_leave_profile(),
    )
    monkeypatch.setattr(web_app.TaskLogger, "load", lambda task_id: fake_task)

    try:
        with TestClient(web_app.app) as client:
            distilled = client.post(
                "/api/workflow-skills/distill",
                json={"user_id": "alice", "task_id": "task-1", "workflow_id": "alice:wf"},
                headers=headers,
            )
            assert distilled.status_code == 200
            skill_id = distilled.json()["skill"]["skill_id"]

            activated = client.post(
                f"/api/workflow-skills/{skill_id}/activate",
                json={"user_id": "alice"},
                headers=headers,
            )
            assert activated.status_code == 200
            assert activated.json()["skill"]["status"] == "active"

            listed = client.get("/api/workflow-skills", params={"user_id": "alice"}, headers=headers)
            assert listed.status_code == 200
            assert listed.json()[0]["skill_id"] == skill_id

            disabled = client.post(
                f"/api/workflow-skills/{skill_id}/disable",
                json={"user_id": "alice"},
                headers=headers,
            )
            assert disabled.status_code == 200
            assert disabled.json()["event"] == "skill_disabled"

            forbidden = client.get(
                f"/api/workflow-skills/{skill_id}",
                params={"user_id": "bob"},
                headers=headers,
            )
            assert forbidden.status_code == 404
    finally:
        set_workflow_skill_manager(None)


def test_production_distills_success_and_disables_reused_skill_after_permission_failures(tmp_path, monkeypatch):
    import src.workflow.process as process

    manager = _manager(tmp_path)
    fake_cache = _FakeCache()
    fake_cache.steps = [
        {
            "agent_name": "reporter",
            "description": "Process the current leave request",
        }
    ]
    fake_cache.cache["alice:wf"]["planning_steps"] = fake_cache.steps

    monkeypatch.setattr(process, "cache", fake_cache)
    monkeypatch.setattr(process, "get_workflow_skill_manager", lambda: manager)
    monkeypatch.setattr(process, "TaskLogger", _FakeTaskLogger)
    monkeypatch.setattr(process, "CheckpointManager", _FakeCheckpointManager)
    monkeypatch.setattr(process, "AUTO_RECOVERY_ENABLED", False)
    monkeypatch.setattr(process, "get_llm_by_type", lambda _kind: SimpleNamespace())

    async def finish(_state):
        return SimpleNamespace(goto="__end__", update={})

    workflow = CompiledWorkflow(nodes={"publisher": finish}, edges={}, start_node="publisher")
    base_state = {
        "user_id": "alice",
        "TEAM_MEMBERS": ["reporter"],
        "TEAM_MEMBERS_DESCRIPTION": "reporter",
        "TOOLS": "",
        "RESOURCE_CATALOG": "",
        "USER_QUERY": "Please submit my leave request",
        "execution_user_query": "Confirm execution",
        "original_user_query": "Please submit my leave request",
        "messages": [{"role": "user", "content": "Confirm execution"}],
        "deep_thinking_mode": False,
        "search_before_planning": False,
        "workflow_id": "alice:wf",
        "workflow_mode": "production",
        "initialized": True,
        "stop_after_planner": False,
        "instruction_history": ["Please submit my leave request"],
        "memory_session_id": "",
        "memory_context": {},
        "skill_reuse_enabled": True,
        "reused_skill_id": "",
        "reused_skill_owner_id": "",
        "workflow_skill_match": {},
        "task_profile": _leave_profile(),
    }

    async def run_success():
        return [
            event
            async for event in process._process_workflow(
                workflow,
                dict(base_state),
                task_id="task-production-success",
                execution_phase="execution",
            )
        ]

    success_events = asyncio.run(run_success())
    assert "skill_distilled" in [event["event"] for event in success_events]
    candidate = manager.store.list("alice", include_shared=False)[0]
    assert candidate.status == WorkflowSkillStatus.CANDIDATE

    active = manager.store.activate("alice", candidate.skill_id)

    async def deny(_state):
        raise PermissionDeniedError(
            "policy denied",
            {"policy_result": {"reason": "role mismatch"}},
        )

    denied_workflow = CompiledWorkflow(nodes={"agent_proxy": deny}, edges={}, start_node="agent_proxy")
    denied_state = {
        **base_state,
        "reused_skill_id": active.skill_id,
        "reused_skill_owner_id": "alice",
        "workflow_skill_match": {"skill_id": active.skill_id, "owner_user_id": "alice"},
    }

    async def run_denied(task_id):
        return [
            event
            async for event in process._process_workflow(
                denied_workflow,
                dict(denied_state),
                task_id=task_id,
                execution_phase="execution",
            )
        ]

    first_denied = asyncio.run(run_denied("task-denied-1"))
    second_denied = asyncio.run(run_denied("task-denied-2"))
    assert "skill_execution_failed" in [event["event"] for event in first_denied]
    assert "skill_disabled" in [event["event"] for event in second_denied]
    assert manager.store.get("alice", active.skill_id).status == WorkflowSkillStatus.DISABLED


def test_non_success_agent_status_is_not_distilled(tmp_path, monkeypatch):
    import src.workflow.process as process

    manager = _manager(tmp_path)
    fake_cache = _FakeCache()
    fake_cache.steps = [{"agent_name": "reporter", "description": "Process leave"}]
    fake_cache.cache["alice:wf"]["planning_steps"] = fake_cache.steps

    monkeypatch.setattr(process, "cache", fake_cache)
    monkeypatch.setattr(process, "get_workflow_skill_manager", lambda: manager)
    monkeypatch.setattr(process, "TaskLogger", _FakeTaskLogger)
    monkeypatch.setattr(process, "CheckpointManager", _FakeCheckpointManager)
    monkeypatch.setattr(process, "AUTO_RECOVERY_ENABLED", False)
    monkeypatch.setattr(process, "get_llm_by_type", lambda _kind: SimpleNamespace())

    async def failed_agent(_state):
        return SimpleNamespace(goto="__end__", update={"workflow_execution_failed": True})

    workflow = CompiledWorkflow(nodes={"agent_proxy": failed_agent}, edges={}, start_node="agent_proxy")
    state = {
        "user_id": "alice",
        "TEAM_MEMBERS": ["reporter"],
        "TEAM_MEMBERS_DESCRIPTION": "reporter",
        "TOOLS": "",
        "RESOURCE_CATALOG": "",
        "USER_QUERY": "Please submit my leave request",
        "execution_user_query": "Confirm execution",
        "original_user_query": "Please submit my leave request",
        "messages": [{"role": "user", "content": "Confirm execution"}],
        "deep_thinking_mode": False,
        "search_before_planning": False,
        "workflow_id": "alice:wf",
        "workflow_mode": "production",
        "initialized": True,
        "stop_after_planner": False,
        "instruction_history": ["Please submit my leave request"],
        "memory_session_id": "",
        "memory_context": {},
        "skill_reuse_enabled": True,
        "reused_skill_id": "",
        "reused_skill_owner_id": "",
        "workflow_skill_match": {},
        "workflow_execution_failed": False,
        "task_profile": _leave_profile(),
    }

    async def run():
        return [
            event
            async for event in process._process_workflow(
                workflow,
                state,
                task_id="task-production-failed",
                execution_phase="execution",
            )
        ]

    events = asyncio.run(run())
    assert "skill_distilled" not in [event["event"] for event in events]
    assert manager.store.list("alice", include_shared=False) == []
    end_event = next(event for event in events if event["event"] == "end_of_workflow")
    assert end_event["data"]["status"] == "failed"
    assert end_event["data"]["messages"][0]["content"] == "workflow failed"


def test_request_flag_disables_reuse_and_runs_normal_graph(tmp_path, monkeypatch):
    import src.workflow.process as process

    manager = _manager(tmp_path)
    card = manager.bootstrap_leave_request("alice")
    manager.store.activate("alice", card.skill_id)
    fake_cache = _FakeCache()
    node_calls = []

    async def scenario_profile(_query, _metadata):
        return _leave_profile()

    async def normal_coordinator(_state):
        node_calls.append("coordinator")
        return SimpleNamespace(goto="__end__", update={})

    monkeypatch.setattr(process, "cache", fake_cache)
    monkeypatch.setattr(process, "get_workflow_skill_manager", lambda: manager)
    monkeypatch.setattr(process, "analyze_task_context", scenario_profile)
    monkeypatch.setattr(process, "TaskLogger", _FakeTaskLogger)
    monkeypatch.setattr(process, "CheckpointManager", _FakeCheckpointManager)
    monkeypatch.setattr(process, "AUTO_RECOVERY_ENABLED", False)
    monkeypatch.setattr(process, "get_llm_by_type", lambda _kind: SimpleNamespace())

    workflow = CompiledWorkflow(
        nodes={"coordinator": normal_coordinator},
        edges={},
        start_node="coordinator",
    )
    query = "I need to request leave"
    state = {
        "user_id": "alice",
        "TEAM_MEMBERS": ["RemoteHRAssistantAgent", "RemoteOfficeAssistantAgent"],
        "TEAM_MEMBERS_DESCRIPTION": "HR and office assistants",
        "TOOLS": "",
        "RESOURCE_CATALOG": "",
        "USER_QUERY": query,
        "execution_user_query": query,
        "original_user_query": query,
        "messages": [{"role": "user", "content": query}],
        "deep_thinking_mode": False,
        "search_before_planning": False,
        "workflow_id": "alice:wf",
        "workflow_mode": "launch",
        "initialized": False,
        "stop_after_planner": True,
        "instruction_history": [query],
        "memory_session_id": "",
        "memory_context": {},
        "skill_reuse_enabled": False,
        "reused_skill_id": "",
        "reused_skill_owner_id": "",
        "workflow_skill_match": {},
        "workflow_execution_failed": False,
    }

    async def run():
        return [
            event
            async for event in process._process_workflow(
                workflow,
                state,
                task_id="task-reuse-disabled",
            )
        ]

    events = asyncio.run(run())
    assert node_calls == ["coordinator"]
    assert "skill_matched" not in [event["event"] for event in events]
