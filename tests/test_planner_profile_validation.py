from src.workflow.coor_task import (
    _infer_step_intents,
    _scheduler_profile_validation_state,
    _validate_plan_against_task_profile,
)
from src.orchestration.plan_to_task_graph import plan_to_task_graph


def test_intent_evidence_matches_normal_chinese_planner_text():
    intents = _infer_step_intents(
        {
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询李娜员工基础信息和薪资收入信息",
        }
    )

    assert intents == {"employee_information_query", "salary_query"}


def test_separate_hr_steps_are_rejected_as_duplicate_execution():
    state = {
        "task_profile": {
            "subtasks": [
                {
                    "id": "subtask_1",
                    "intent": "employee_information_query",
                    "depends_on": [],
                },
                {
                    "id": "subtask_2",
                    "intent": "salary_query",
                    "depends_on": ["subtask_1"],
                },
                {
                    "id": "subtask_3",
                    "intent": "document_generation",
                    "depends_on": ["subtask_1", "subtask_2"],
                },
                {
                    "id": "subtask_4",
                    "intent": "message_or_email_send",
                    "depends_on": ["subtask_3"],
                },
            ]
        }
    }
    steps = [
        {
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询李娜员工基础信息",
        },
        {
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询李娜薪资收入信息",
        },
        {
            "agent_name": "RemoteDocumentGeneratorAgent",
            "title": "生成李娜收入证明文档",
        },
        {
            "agent_name": "RemoteEmailDispatchAgent",
            "title": "将收入证明发送给王经理",
        },
    ]

    errors = _validate_plan_against_task_profile(steps, state)
    assert (
        "Agent RemoteHRAssistantAgent 被拆成了 2 个执行步骤；"
        "当前执行器按 Agent 调用，请合并为一个步骤"
    ) in errors


def test_structured_steps_can_group_compatible_hr_subtasks():
    state = {
        "task_profile": {
            "subtasks": [
                {
                    "id": "subtask_1",
                    "intent": "employee_information_query",
                    "depends_on": [],
                },
                {
                    "id": "subtask_2",
                    "intent": "salary_query",
                    "depends_on": ["subtask_1"],
                },
                {
                    "id": "subtask_3",
                    "intent": "document_generation",
                    "depends_on": ["subtask_1", "subtask_2"],
                },
                {
                    "id": "subtask_4",
                    "intent": "message_or_email_send",
                    "depends_on": ["subtask_3"],
                },
            ]
        }
    }
    steps = [
        {
            "step_id": "step_hr",
            "subtask_ids": ["subtask_1", "subtask_2"],
            "intents": ["employee_information_query", "salary_query"],
            "depends_on": [],
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询李娜员工基础信息和薪资收入信息",
        },
        {
            "step_id": "step_document",
            "subtask_ids": ["subtask_3"],
            "intents": ["document_generation"],
            "depends_on": ["subtask_1", "subtask_2"],
            "agent_name": "RemoteDocumentGeneratorAgent",
            "title": "生成收入证明",
        },
        {
            "step_id": "step_send",
            "subtask_ids": ["subtask_4"],
            "intents": ["message_or_email_send"],
            "depends_on": ["subtask_3"],
            "agent_name": "RemoteEmailDispatchAgent",
            "title": "将收入证明发送给王经理",
        },
    ]

    assert _validate_plan_against_task_profile(steps, state) == []

    graph = plan_to_task_graph(steps, task_id="task-1")
    by_id = {step.step_id: step for step in graph.steps}
    assert len(graph.steps) == 3
    assert by_id["step_hr"].depends_on == []
    assert by_id["step_document"].depends_on == ["step_hr"]
    assert by_id["step_send"].depends_on == ["step_document"]


def test_structured_plan_accepts_agent_name_dependency_aliases():
    state = {
        "task_profile": {
            "subtasks": [
                {"id": "subtask_1", "intent": "employee_information_query", "depends_on": []},
                {"id": "subtask_2", "intent": "leave_record_query", "depends_on": ["subtask_1"]},
                {"id": "subtask_3", "intent": "report_generation", "depends_on": ["subtask_2"]},
            ]
        }
    }
    steps = [
        {
            "step_id": "step_hr",
            "subtask_ids": ["subtask_1"],
            "intents": ["employee_information_query"],
            "depends_on": [],
            "agent_name": "RemoteHRAssistantAgent",
        },
        {
            "step_id": "step_leave",
            "subtask_ids": ["subtask_2"],
            "intents": ["leave_record_query"],
            "depends_on": ["RemoteHRAssistantAgent"],
            "agent_name": "RemoteOfficeAssistantAgent",
        },
        {
            "step_id": "step_report",
            "subtask_ids": ["subtask_3"],
            "intents": ["report_generation"],
            "depends_on": ["step_leave"],
            "agent_name": "RemoteReportAgent",
        },
    ]

    assert _validate_plan_against_task_profile(steps, state) == []


def test_structured_plan_rejects_missing_subtask_coverage():
    state = {
        "task_profile": {
            "subtasks": [
                {
                    "id": "subtask_1",
                    "intent": "employee_information_query",
                    "depends_on": [],
                },
                {
                    "id": "subtask_2",
                    "intent": "salary_query",
                    "depends_on": ["subtask_1"],
                },
            ]
        }
    }
    merged_steps = [
        {
            "step_id": "step_hr",
            "subtask_ids": ["subtask_1"],
            "intents": ["employee_information_query"],
            "depends_on": [],
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询员工基础信息和薪资",
        }
    ]

    errors = _validate_plan_against_task_profile(merged_steps, state)
    assert "缺少对子任务 subtask_2 的执行覆盖" in errors


def test_structured_plan_rejects_duplicate_subtask_coverage():
    state = {
        "task_profile": {
            "subtasks": [
                {
                    "id": "subtask_1",
                    "intent": "employee_information_query",
                    "depends_on": [],
                }
            ]
        }
    }
    steps = [
        {
            "step_id": "step_hr_1",
            "subtask_ids": ["subtask_1"],
            "intents": ["employee_information_query"],
            "depends_on": [],
            "agent_name": "RemoteHRAssistantAgent",
        },
        {
            "step_id": "step_hr_2",
            "subtask_ids": ["subtask_1"],
            "intents": ["employee_information_query"],
            "depends_on": [],
            "agent_name": "RemoteHRAssistantAgent",
        },
    ]

    errors = _validate_plan_against_task_profile(steps, state)
    assert "子任务 subtask_1 被多个执行步骤重复覆盖" in errors


def test_legacy_merged_step_allows_internal_dependency():
    state = {
        "task_profile": {
            "subtasks": [
                {
                    "id": "subtask_1",
                    "intent": "employee_information_query",
                    "depends_on": [],
                },
                {
                    "id": "subtask_2",
                    "intent": "salary_query",
                    "depends_on": ["subtask_1"],
                },
            ]
        }
    }
    steps = [
        {
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询李娜员工基础信息和薪资收入信息",
        }
    ]

    assert _validate_plan_against_task_profile(steps, state) == []


def test_scheduler_rejects_steps_without_trusted_subtask_bindings(monkeypatch):
    monkeypatch.setattr(
        "src.service.env.ORCHESTRATION_SCHEDULER_ENABLED",
        True,
        raising=False,
    )
    state = {
        "task_profile": {
            "subtasks": [
                {
                    "id": "subtask_1",
                    "intent": "salary_query",
                    "depends_on": [],
                }
            ]
        }
    }
    steps = [
        {
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询员工工资",
        }
    ]

    errors = _validate_plan_against_task_profile(
        steps,
        _scheduler_profile_validation_state(state),
    )

    assert any(
        "每个执行步骤必须包含可验证的 subtask_ids" in error
        for error in errors
    )
