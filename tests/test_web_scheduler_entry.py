"""T12/T13: production request enters the scheduler via a PlanSnapshot, and the
legacy path is used (scheduler NOT entered) when the flag is off.

These drive the real ``_process_workflow`` (the code path behind the Web/API
``run_agent_workflow``) rather than only the conversion helpers.
"""

import asyncio
from types import SimpleNamespace

import pytest

import src.orchestration.runtime as runtime_mod
import src.workflow.process as proc
from src.orchestration.plan_snapshot import save_plan_snapshot
from src.orchestration.plan_to_task_graph import plan_to_task_graph
from src.skills.workflow_skill import (
    WorkflowSkillManager,
    WorkflowSkillSettings,
    WorkflowSkillStore,
)

_STEPS = [{"agent_name": "RemoteHRAssistantAgent", "title": "查询王强信息"}]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAN_SNAPSHOT_DIR", str(tmp_path / "plan_snapshots"))
    monkeypatch.setenv("ARTIFACT_PAYLOAD_STORE_DIR",
                       str(tmp_path / "artifacts"))
    monkeypatch.setenv("RECEIPT_STORE_DIR", str(tmp_path / "receipts"))


def _drive(workflow, state, *, task_id, execution_phase):
    async def _run():
        events = []
        async for ev in proc._process_workflow(
            workflow, state, task_id=task_id, execution_phase=execution_phase
        ):
            events.append(ev)
        return events

    return asyncio.run(_run())


def _production_state():
    return {
        "user_id": "u1",
        "workflow_id": "wf-t12",
        "workflow_mode": "production",
        "USER_QUERY": "查询王强信息",
        "original_user_query": "查询王强信息",
        "messages": [{"role": "user", "content": "查询王强信息"}],
        "task_profile": {"task_type": "GENERAL"},
        "risk_profile": "LOW",
    }


def test_t12_production_enters_scheduler_via_snapshot(monkeypatch):
    # A validated snapshot exists for this workflow (as the Planner would have
    # persisted), and the current plan matches it.
    tg = plan_to_task_graph(
        _STEPS,
        task_id="wf-t12",
        subject="u1",
        goal="查询王强信息",
    ).model_dump()
    save_plan_snapshot(
        workflow_id="wf-t12", user_id="u1", planning_steps=_STEPS, task_graph=tg
    )
    monkeypatch.setattr(
        proc, "orchestration_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(proc.cache, "get_planning_steps",
                        lambda wf: _STEPS, raising=False)

    entered = {"v": False}

    async def _fake_scheduler(state, *, task_id, **kwargs):
        entered["v"] = True
        # The snapshot's TaskGraph must have been injected before the gate.
        assert state.get("task_graph") is not None
        yield {
            "event": "end_of_workflow",
            "data": {"workflow_id": state.get("workflow_id"), "task_id": task_id,
                     "mode": "scheduler", "status": "SUCCEEDED"},
        }

    monkeypatch.setattr(runtime_mod, "run_scheduler_workflow",
                        _fake_scheduler, raising=False)

    workflow = SimpleNamespace(start_node="coordinator", nodes={})
    events = _drive(workflow, _production_state(),
                    task_id="task-t12", execution_phase="execution")

    assert entered["v"] is True  # real path entered run_scheduler_workflow
    assert events[-1]["event"] == "end_of_workflow"
    assert events[-1]["data"]["status"] == "SUCCEEDED"


def test_scheduler_distills_before_terminal_event(tmp_path, monkeypatch):
    task_id = "task-scheduler-distill"
    graph = plan_to_task_graph(
        _STEPS,
        task_id="wf-t12",
        subject="u1",
        goal=_production_state()["original_user_query"],
    ).model_dump()
    save_plan_snapshot(
        workflow_id="wf-t12",
        user_id="u1",
        planning_steps=_STEPS,
        task_graph=graph,
    )
    settings = WorkflowSkillSettings(
        store_path=tmp_path / "workflow-skills.sqlite3",
        promotion_success_threshold=2,
    )
    manager = WorkflowSkillManager(
        settings=settings,
        store=WorkflowSkillStore(settings.store_path),
    )
    monkeypatch.setattr(proc, "get_workflow_skill_manager", lambda: manager)
    monkeypatch.setattr(proc, "orchestration_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(proc.cache, "get_planning_steps", lambda _wf: _STEPS)

    async def fake_scheduler(state, *, task_id, **_kwargs):
        evidence = {
            "task_id": task_id,
            "workflow_id": state["workflow_id"],
            "execution_mode": "scheduler",
            "workflow_status": "SUCCEEDED",
            "technical_success": True,
            "business_success": True,
            "business_outcome_coverage": 1.0,
            "planning_steps": _STEPS,
            "steps": [
                {
                    "step_id": "step_1",
                    "agent_name": "RemoteHRAssistantAgent",
                    "operation_mode": "read",
                    "technical_success": True,
                    "business_success": True,
                    "verification_status": "not_required",
                }
            ],
        }
        state["skill_execution_evidence"] = evidence
        yield {
            "event": "end_of_workflow",
            "data": {
                "workflow_id": state["workflow_id"],
                "task_id": task_id,
                "mode": "scheduler",
                "status": "SUCCEEDED",
                "skill_execution_evidence": evidence,
            },
        }

    monkeypatch.setattr(runtime_mod, "run_scheduler_workflow", fake_scheduler)
    workflow = SimpleNamespace(start_node="coordinator", nodes={})
    events = _drive(
        workflow,
        _production_state(),
        task_id=task_id,
        execution_phase="execution",
    )

    names = [event["event"] for event in events]
    assert "skill_distilled" in names
    assert names[-1] == "end_of_workflow"
    assert names.index("skill_distilled") < names.index("end_of_workflow")
    assert manager.store.list("u1", include_shared=False)[0].evidence_count == 1


def test_prepopulated_graph_cannot_bypass_missing_snapshot(monkeypatch):
    state = _production_state()
    state["task_graph"] = {
        "apiVersion": "cooragent/v1",
        "kind": "TaskGraph",
        "metadata": {"name": "injected"},
        "spec": {"task_id": "injected", "subject": "u1", "steps": []},
    }
    monkeypatch.setattr(
        proc.cache, "get_planning_steps", lambda _wf: _STEPS, raising=False
    )

    loaded, reason = proc.load_production_task_graph(state, "execution")

    assert loaded is False
    assert reason == "no_snapshot"
    assert "task_graph" not in state


def test_prepopulated_graph_is_replaced_by_approved_snapshot(monkeypatch):
    state = _production_state()
    state["task_graph"] = {
        "apiVersion": "cooragent/v1",
        "kind": "TaskGraph",
        "metadata": {"name": "injected"},
        "spec": {"task_id": "injected", "subject": "u1", "steps": []},
    }
    approved = plan_to_task_graph(
        _STEPS,
        task_id="wf-t12",
        subject="u1",
        goal="查询王强信息",
    ).model_dump()
    save_plan_snapshot(
        workflow_id="wf-t12",
        user_id="u1",
        planning_steps=_STEPS,
        task_graph=approved,
    )
    monkeypatch.setattr(
        proc.cache, "get_planning_steps", lambda _wf: _STEPS, raising=False
    )

    loaded, reason = proc.load_production_task_graph(state, "execution")

    assert loaded is True
    assert reason == "loaded"
    assert state["task_graph"] == approved


def test_t13_scheduler_disabled_uses_legacy_and_does_not_enter_scheduler(monkeypatch):
    from langgraph.types import Command

    monkeypatch.setattr(
        proc, "orchestration_scheduler_enabled", False, raising=False)
    monkeypatch.setattr(proc.cache, "dump", lambda *a,
                        **k: None, raising=False)

    entered = {"v": False}

    async def _fake_scheduler(*a, **k):
        entered["v"] = True
        if False:  # pragma: no cover - make it an async generator
            yield {}

    monkeypatch.setattr(runtime_mod, "run_scheduler_workflow",
                        _fake_scheduler, raising=False)

    async def _end_node(state):
        return Command(update={}, goto="__end__")

    workflow = SimpleNamespace(start_node="only", nodes={"only": _end_node})
    # A valid task_graph is present, yet the disabled flag must keep the legacy
    # path and never enter the scheduler.
    state = _production_state()
    state["task_graph"] = plan_to_task_graph(
        _STEPS, task_id="wf-t12", subject="u1").model_dump()

    events = _drive(workflow, state, task_id="task-t13",
                    execution_phase="execution")

    assert entered["v"] is False  # scheduler NOT entered
    end = events[-1]
    assert end["event"] == "end_of_workflow"
    # Legacy end_of_workflow carries no canonical status field.
    assert "status" not in end["data"]


def test_t13_code_default_is_off(monkeypatch):
    # Safety baseline = the CODE default when the env var is unset. A developer's
    # local .env may opt in for gray-release verification, so assert the default
    # rather than the loaded runtime value.
    monkeypatch.delenv("ORCHESTRATION_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("ARTIFACT_CAPTURE_ENABLED", raising=False)
    from src.service.env import _parse_bool

    assert _parse_bool("ORCHESTRATION_SCHEDULER_ENABLED", False) is False
    assert _parse_bool("ARTIFACT_CAPTURE_ENABLED", False) is False


def test_scheduler_gate_failure_uses_safe_structured_protocol(monkeypatch):
    monkeypatch.setattr(proc, "orchestration_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(proc.cache, "get_planning_steps", lambda _wf: _STEPS)

    workflow = SimpleNamespace(start_node="coordinator", nodes={})
    events = _drive(
        workflow,
        _production_state(),
        task_id="task-gate-safe",
        execution_phase="execution",
    )

    terminal = events[-1]["data"]
    assert terminal["status"] == "FAILED"
    assert terminal["failures"][0]["code"] == "TASK_GRAPH_MISSING"
    assert terminal["failed_steps"] == []
    assert terminal["blocked_steps"] == []
    assert "no explicit task graph" not in str(terminal)
