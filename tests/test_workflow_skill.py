from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.skills.workflow_skill import (
    WorkflowSkillManager,
    WorkflowSkillSettings,
    WorkflowSkillStatus,
    WorkflowSkillStore,
)


def test_workflow_skill_types_are_exported_from_skills_package():
    from src.skills import WorkflowSkillManager as ExportedWorkflowSkillManager

    assert ExportedWorkflowSkillManager is WorkflowSkillManager


def test_task_logger_persists_execution_plan_snapshot(tmp_path, monkeypatch):
    import src.robust.task_logger as task_logger_module

    monkeypatch.setattr(task_logger_module, "_get_task_logs_dir", lambda: tmp_path)
    logger = task_logger_module.TaskLogger(
        task_id="task-snapshot",
        workflow_id="alice:wf",
        user_query="请假",
    )
    logger.set_execution_phase("execution")
    logger.set_workflow_snapshot(
        [{"agent_name": "leave_writer", "description": "执行请假"}],
        {"task_type": "HR", "scenario_tags": ["leave_request"]},
    )
    logger.log_workflow_end()

    restored = task_logger_module.TaskLogger.load("task-snapshot")

    assert restored is not None
    assert restored.planning_steps[0]["agent_name"] == "leave_writer"
    assert restored.task_profile["task_type"] == "HR"


def _manager(tmp_path: Path, **overrides) -> WorkflowSkillManager:
    values = {
        "enabled": True,
        "reuse_enabled": True,
        "match_threshold": 0.45,
        "match_margin": 0.05,
        "promotion_success_threshold": 2,
        "failure_disable_threshold": 2,
        "store_path": tmp_path / "workflow-skills.sqlite3",
    }
    values.update(overrides)
    settings = WorkflowSkillSettings(**values)
    return WorkflowSkillManager(settings=settings, store=WorkflowSkillStore(settings.store_path))


def _profile():
    return {
        "task_type": "HR",
        "scenario_tags": ["leave_request", "hr_service"],
        "expected_capabilities": ["leave management"],
        "risk_profile": "MEDIUM",
    }


def test_distill_rejects_secrets_and_parameterizes_request(tmp_path):
    manager = _manager(tmp_path)
    fake_secret = "sk-test-" + "a" * 26
    card = manager.distill(
        user_id="alice",
        task_id="task-1",
        user_query="Please submit my leave request for Monday",
        planning_steps=[
            {
                "agent_name": "reporter",
                "description": "Process: Please submit my leave request for Monday",
            }
        ],
        task_profile=_profile(),
    )

    assert card.status == WorkflowSkillStatus.CANDIDATE
    assert card.planning_steps[0]["request_context"] == "{{user_request}}"
    assert "{{user_request}}" in card.planning_steps[0]["description"]
    assert card.intent_examples != ["Please submit my leave request for Monday"]

    with pytest.raises(ValueError, match="secret"):
        manager.distill(
            user_id="alice",
            task_id="task-secret",
            user_query="submit leave",
            planning_steps=[{"agent_name": "reporter", "description": f"api_key={fake_secret}"}],
            task_profile=_profile(),
        )

    with pytest.raises(ValueError, match="secret"):
        manager.distill(
            user_id="alice",
            task_id="task-secret-profile",
            user_query="submit leave",
            planning_steps=[{"agent_name": "reporter", "description": "Handle current request"}],
            task_profile={**_profile(), "expected_capabilities": [f"api_key={fake_secret}"]},
        )

    with pytest.raises(ValueError, match="secret"):
        manager.distill(
            user_id="alice",
            task_id="task-secret-query",
            user_query=f"Use api_key={fake_secret}",
            planning_steps=[{"agent_name": "reporter", "description": "Handle current request"}],
            task_profile=_profile(),
        )


def test_distillation_removes_split_task_values_and_preserves_data_flow(tmp_path):
    manager = _manager(tmp_path)
    source_steps = [
        {
            "agent_name": "hr_lookup",
            "title": "查询员工 E001",
            "description": "查询张三的员工信息，准备 2026-08-03 的年假申请",
            "note": "请假原因为家庭事务",
            "inputs": [],
        },
        {
            "agent_name": "leave_writer",
            "description": "为 E001 保存 2026-08-03 的请假记录",
            "inputs": [
                {
                    "parameter_name": "employee.id",
                    "source_step": "hr_lookup",
                    "source_output": "employee.id",
                    "description": "张三的员工编号 E001",
                }
            ],
        },
    ]

    card = manager.distill(
        user_id="alice",
        task_id="task-split-values",
        user_query="张三申请 2026-08-03 年假，原因为家庭事务",
        planning_steps=source_steps,
        task_profile=_profile(),
    )
    serialized = str(card.planning_steps)

    assert "张三" not in serialized
    assert "E001" not in serialized
    assert "2026-08-03" not in serialized
    assert "家庭事务" not in serialized
    assert card.planning_steps[1]["inputs"][0] == {
        "parameter_name": "employee.id",
        "source_step": "hr_lookup",
        "source_output": "employee.id",
        "description": "将 hr_lookup.employee.id 映射到 employee.id",
    }

    manager.store.activate("alice", card.skill_id)
    match = manager.match(
        user_id="alice",
        query="李四申请 2026-09-10 病假，原因为就医",
        task_profile=_profile(),
        available_agents=["hr_lookup", "leave_writer"],
    )
    assert match is not None
    rebound = str(match.bound_planning_steps)
    assert "李四" in rebound
    assert "2026-09-10" in rebound
    assert "张三" not in rebound
    assert "2026-08-03" not in rebound

def test_repeated_success_promotes_and_is_user_scoped(tmp_path):
    manager = _manager(tmp_path)
    first = manager.distill(
        user_id="alice",
        task_id="task-1",
        user_query="Please request leave",
        planning_steps=[{"agent_name": "reporter", "description": "Handle {{user_request}}"}],
        task_profile=_profile(),
    )
    second = manager.distill(
        user_id="alice",
        task_id="task-2",
        user_query="Please request leave",
        planning_steps=[{"agent_name": "reporter", "description": "Handle {{user_request}}"}],
        task_profile=_profile(),
    )

    assert first.status == WorkflowSkillStatus.CANDIDATE
    assert second.status == WorkflowSkillStatus.ACTIVE
    assert second.evidence_count == 2
    assert manager.store.list("bob", include_shared=False) == []


def test_first_evidence_activates_when_promotion_threshold_is_one(tmp_path):
    manager = _manager(tmp_path, promotion_success_threshold=1)

    card = manager.distill(
        user_id="alice",
        task_id="task-1",
        user_query="Please request leave",
        planning_steps=[{"agent_name": "reporter", "description": "Handle leave"}],
        task_profile=_profile(),
    )

    assert card.status == WorkflowSkillStatus.ACTIVE


def test_concurrent_store_instances_preserve_all_evidence(tmp_path):
    worker_count = 8
    managers = [_manager(tmp_path, promotion_success_threshold=worker_count) for _ in range(worker_count)]

    def distill(index):
        return managers[index].distill(
            user_id="alice",
            task_id=f"task-{index}",
            user_query="Please request leave",
            planning_steps=[{"agent_name": "reporter", "description": "Handle leave"}],
            task_profile=_profile(),
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(distill, range(worker_count)))

    cards = managers[0].store.list("alice", include_shared=False)
    assert len(cards) == 1
    assert cards[0].evidence_count == worker_count
    assert set(cards[0].provenance.source_task_ids) == {
        f"task-{index}" for index in range(worker_count)
    }
    assert cards[0].status == WorkflowSkillStatus.ACTIVE


def test_concurrent_outcomes_do_not_lose_failure_counts(tmp_path):
    worker_count = 8
    manager = _manager(tmp_path)
    card = manager.bootstrap_leave_request("alice")
    manager.store.activate("alice", card.skill_id)
    stores = [WorkflowSkillStore(manager.settings.store_path) for _ in range(worker_count)]

    def record_failure(index):
        return stores[index].record_outcome(
            "alice",
            card.skill_id,
            success=False,
            failure_threshold=worker_count,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(record_failure, range(worker_count)))

    updated = manager.store.get("alice", card.skill_id)
    assert updated is not None
    assert updated.failure_count == worker_count
    assert updated.consecutive_failures == worker_count
    assert updated.status == WorkflowSkillStatus.DISABLED


def test_duplicate_source_task_is_idempotent(tmp_path):
    manager = _manager(tmp_path)
    kwargs = {
        "user_id": "alice",
        "task_id": "task-1",
        "user_query": "Please submit my leave request",
        "planning_steps": [{"agent_name": "reporter", "description": "Handle {{user_request}}"}],
        "task_profile": _profile(),
    }

    first = manager.distill(**kwargs)
    duplicate = manager.distill(**kwargs)

    assert duplicate.skill_id == first.skill_id
    assert duplicate.evidence_count == 1
    assert duplicate.status == WorkflowSkillStatus.CANDIDATE


def test_new_procedure_creates_next_version_and_activation_retires_previous(tmp_path):
    manager = _manager(tmp_path)
    first = manager.distill(
        user_id="alice",
        task_id="task-v1",
        user_query="Please request leave",
        planning_steps=[{"agent_name": "reporter", "description": "Handle leave"}],
        task_profile=_profile(),
    )
    manager.store.activate("alice", first.skill_id)

    second = manager.distill(
        user_id="alice",
        task_id="task-v2",
        user_query="Please request leave",
        planning_steps=[
            {"agent_name": "hr_lookup", "description": "Find employee"},
            {"agent_name": "leave_writer", "description": "Save leave"},
        ],
        task_profile=_profile(),
    )

    assert second.family_signature == first.family_signature
    assert second.signature != first.signature
    assert second.version == 2
    assert second.status == WorkflowSkillStatus.CANDIDATE

    manager.store.activate("alice", second.skill_id)
    assert manager.store.get("alice", first.skill_id).status == WorkflowSkillStatus.DISABLED
    assert manager.store.get("alice", second.skill_id).status == WorkflowSkillStatus.ACTIVE


def test_builtin_leave_skill_matches_chinese_request_at_default_threshold(tmp_path):
    manager = _manager(tmp_path, match_threshold=0.62)
    card = manager.bootstrap_leave_request("alice")
    manager.store.activate("alice", card.skill_id)

    match = manager.match(
        user_id="alice",
        query="我想申请年假，下周三请一天",
        task_profile={
            "task_type": "HR",
            "scenario_tags": [],
            "expected_capabilities": ["HR"],
        },
        available_agents=["RemoteHRAssistantAgent", "RemoteOfficeAssistantAgent"],
    )

    assert match is not None
    assert "下周三" in match.bound_planning_steps[0]["request_context"]


def test_leave_skill_matches_current_request_and_failure_disables(tmp_path):
    manager = _manager(tmp_path)
    card = manager.bootstrap_leave_request("alice")
    manager.store.activate("alice", card.skill_id)

    match = manager.match(
        user_id="alice",
        query="I need to request leave from Tuesday to Thursday because of a family matter",
        task_profile=_profile(),
        available_agents=["RemoteHRAssistantAgent", "RemoteOfficeAssistantAgent"],
    )
    assert match is not None
    assert "Tuesday" in match.bound_planning_steps[0]["description"]
    assert "family matter" in match.bound_planning_steps[0]["request_context"]

    manager.store.record_outcome("alice", card.skill_id, success=False, failure_threshold=2)
    disabled = manager.store.record_outcome("alice", card.skill_id, success=False, failure_threshold=2)
    assert disabled is not None
    assert disabled.status == WorkflowSkillStatus.DISABLED
    assert manager.match(
        user_id="alice",
        query="I need to request leave from Tuesday to Thursday",
        task_profile=_profile(),
        available_agents=["RemoteHRAssistantAgent", "RemoteOfficeAssistantAgent"],
    ) is None


def test_broad_hr_terms_do_not_match_leave_skill(tmp_path):
    manager = _manager(tmp_path, match_threshold=0.62)
    card = manager.bootstrap_leave_request("alice")
    manager.store.activate("alice", card.skill_id)

    match = manager.match(
        user_id="alice",
        query="HR salary information for employee E001",
        task_profile={
            "task_type": "HR",
            "scenario_tags": ["salary_query", "hr_service"],
            "expected_capabilities": ["HR"],
        },
        available_agents=["RemoteHRAssistantAgent", "RemoteOfficeAssistantAgent"],
    )

    assert match is None


def test_missing_agent_and_ambiguous_match_fall_back(tmp_path):
    manager = _manager(tmp_path)
    card = manager.bootstrap_leave_request(
        "alice",
        lookup_agent_name="hr_lookup",
        action_agent_name="leave_writer",
    )
    manager.store.activate("alice", card.skill_id)
    assert manager.match(
        user_id="alice",
        query="I need to request leave",
        task_profile=_profile(),
        available_agents=["reporter"],
    ) is None


def test_shared_skill_is_not_automatically_matched_for_user(tmp_path):
    manager = _manager(tmp_path)
    card = manager.bootstrap_leave_request("share")
    manager.store.activate("share", card.skill_id)

    match = manager.match(
        user_id="alice",
        query="I need to request leave",
        task_profile=_profile(),
        available_agents=["RemoteHRAssistantAgent", "RemoteOfficeAssistantAgent"],
    )

    assert match is None


def test_ambiguous_skills_fall_back_instead_of_guessing(tmp_path):
    manager = _manager(tmp_path, match_threshold=0.6, match_margin=0.08)
    for task_id, tag, agent in (
        ("task-a", "leave_variant_a", "agent_a"),
        ("task-b", "leave_variant_b", "agent_b"),
    ):
        card = manager.distill(
            user_id="alice",
            task_id=task_id,
            user_query="time away",
            planning_steps=[{"agent_name": agent, "description": "Handle time away"}],
            task_profile={
                "task_type": "HR",
                "scenario_tags": [tag],
                "expected_capabilities": ["HR"],
            },
            intent_examples=["time away"],
        )
        manager.store.activate("alice", card.skill_id)

    match = manager.match(
        user_id="alice",
        query="time away",
        task_profile={"task_type": "HR", "scenario_tags": []},
        available_agents=["agent_a", "agent_b"],
    )

    assert match is None


def test_configuration_can_disable_skill_matching(tmp_path):
    manager = _manager(tmp_path, reuse_enabled=False)
    card = manager.bootstrap_leave_request("alice")
    manager.store.activate("alice", card.skill_id)

    assert manager.match(
        user_id="alice",
        query="I need to request leave",
        task_profile=_profile(),
        available_agents=["RemoteHRAssistantAgent", "RemoteOfficeAssistantAgent"],
    ) is None
