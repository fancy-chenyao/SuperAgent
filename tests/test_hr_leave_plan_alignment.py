from __future__ import annotations

import json
from pathlib import Path

from src.workflow.coor_task import _validate_plan_against_task_profile


PROFILE = {
    "subtasks": [
        {
            "id": "subtask_1",
            "intent": "employee_information_query",
            "depends_on": [],
        },
        {
            "id": "subtask_2",
            "intent": "leave_record_query",
            "depends_on": ["subtask_1"],
        },
        {
            "id": "subtask_3",
            "intent": "report_generation",
            "depends_on": ["subtask_1", "subtask_2"],
        },
    ]
}


def test_hr_step_cannot_claim_leave_record_query_for_office_agent() -> None:
    steps = [
        {
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询李娜基础信息和请假记录",
            "description": "查询员工基础信息及请假记录",
        },
        {
            "agent_name": "RemoteReportAgent",
            "title": "生成人事情况汇总",
            "description": "生成报告",
        },
    ]

    errors = _validate_plan_against_task_profile(steps, {"task_profile": PROFILE})

    assert any("leave_record_query" in error for error in errors)


def test_three_agent_plan_matches_profile_and_dependency_order() -> None:
    steps = [
        {
            "agent_name": "RemoteHRAssistantAgent",
            "title": "查询李娜基础信息",
            "description": "查询员工基础信息",
        },
        {
            "agent_name": "RemoteOfficeAssistantAgent",
            "title": "查询李娜请假记录",
            "description": "使用请假记录查询工具",
        },
        {
            "agent_name": "RemoteReportAgent",
            "title": "生成人事情况汇总",
            "description": "生成报告",
        },
    ]

    assert _validate_plan_against_task_profile(
        steps, {"task_profile": PROFILE}
    ) == []


def test_office_agent_registry_exposes_leave_query_tool_and_output() -> None:
    root = Path(__file__).resolve().parents[1]
    registry = json.loads((root / "mock_remote_registry.json").read_text(encoding="utf-8-sig"))
    office = next(
        item
        for item in registry["resources"]
        if item.get("type") == "agent" and item.get("name") == "RemoteOfficeAssistantAgent"
    )
    tools = {item.get("name") for item in office["metadata"]["selected_tools"]}

    assert "query_leave_record" in tools
    assert "employee.leave_records" in office["metadata"]["produces"]


def test_risk_report_and_send_plan_matches_profile() -> None:
    profile = {
        "subtasks": [
            {"id": "subtask_1", "intent": "risk_analysis", "depends_on": []},
            {
                "id": "subtask_2",
                "intent": "report_generation",
                "depends_on": ["subtask_1"],
            },
            {
                "id": "subtask_3",
                "intent": "message_or_email_send",
                "depends_on": ["subtask_2"],
            },
        ]
    }
    steps = [
        {
            "agent_name": "RemoteBusinessRiskAgent",
            "title": "查询客户授信风险",
            "description": "查询授信与信用风险数据",
        },
        {
            "agent_name": "RemoteReportAgent",
            "title": "生成风险分析报告",
            "description": "基于风险数据生成报告",
        },
        {
            "agent_name": "RemoteEmailDispatchAgent",
            "title": "发送风险分析报告",
            "description": "将报告发送给合规负责人",
        },
    ]

    assert _validate_plan_against_task_profile(
        steps, {"task_profile": profile}
    ) == []


def test_public_information_research_and_report_plan_matches_profile() -> None:
    profile = {
        "subtasks": [
            {"id": "subtask_1", "intent": "information_research", "depends_on": []},
            {
                "id": "subtask_2",
                "intent": "report_generation",
                "depends_on": ["subtask_1"],
            },
        ]
    }
    steps = [
        {
            "agent_name": "researcher",
            "title": "搜索李娜公开信息",
            "description": "检索并核验公开资料",
        },
        {
            "agent_name": "RemoteReportAgent",
            "title": "生成简短报告",
            "description": "整理公开资料生成报告",
        },
    ]

    assert _validate_plan_against_task_profile(
        steps, {"task_profile": profile}
    ) == []


def test_weather_and_travel_agents_cover_their_profile_intents() -> None:
    profile = {
        "subtasks": [
            {"id": "subtask_1", "intent": "weather_query", "depends_on": []},
            {"id": "subtask_2", "intent": "travel_service", "depends_on": []},
        ]
    }
    steps = [
        {
            "agent_name": "RemoteWeatherAgent",
            "title": "查询北京明天天气",
            "description": "查询天气状况、气温和温度范围",
        },
        {
            "agent_name": "RemoteOfficeAssistantAgent",
            "title": "查询员工出差行程",
            "description": "查询出差申请和差旅行程",
        },
    ]

    assert _validate_plan_against_task_profile(
        steps, {"task_profile": profile}
    ) == []
