import asyncio
import json
from pathlib import Path

import pytest

from remote_agents.base_agent import (
    BaseRemoteAgent,
    bind_authorized_remote_tools,
    reset_authorized_remote_tools,
)
from src.security.remote_tool_gate import required_remote_tool_authorizations


def test_every_registered_remote_tool_has_security_attributes():
    from config.s_abac_config import RESOURCE_SECURITY_ATTRIBUTES

    registry_path = Path(__file__).parents[1] / "mock_remote_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    registered = {
        str(tool["name"])
        for resource in registry["resources"]
        if resource.get("type") == "agent"
        for tool in (resource.get("metadata") or {}).get("selected_tools", [])
    }

    assert registered
    assert registered <= set(RESOURCE_SECURITY_ATTRIBUTES)


def test_tool_enforcement_uses_trusted_taskgraph_operation_mode(monkeypatch):
    from types import SimpleNamespace

    from src.security.enforcement import ApprovalRequiredError, enforce_tool_call

    monkeypatch.setattr("src.security.enforcement.S_ABAC_ENABLED", True)
    context = SimpleNamespace(
        user_id="hr_manager",
        workflow_id="wf-salary",
        workflow_mode="production",
        metadata={
            "task_id": "task-salary-mode",
            "operation_mode": "read",
            "task_profile": {
                "task_type": "HR",
                "business_goal": "查询员工李娜的工资信息",
                "scenario_tags": ["hr_service", "salary_query"],
                "expected_capabilities": ["HR"],
                "operation_mode": "read",
            },
        },
    )

    with pytest.raises(ApprovalRequiredError) as raised:
        asyncio.run(
            enforce_tool_call(
                agent=object(),
                tool_name="remote_salary_info_tool",
                arguments={"employee_name": "李娜"},
                context=context,
            )
        )

    action = raised.value.payload["action"]["attributes"]
    assert action["operation_mode"] == "read"
    assert action["parameters"]["operation_mode"] == "read"


def test_hr_basic_query_authorizes_only_person_tool():
    resolved = required_remote_tool_authorizations(
        agent_name="RemoteHRAssistantAgent",
        intents=["employee_information_query"],
        task_profile={"entities": {"employee_name": "李娜"}},
    )

    assert [item.tool_name for item in resolved] == ["remote_person_info_tool"]
    assert resolved[0].arguments == {
        "employee_name": "李娜",
        "intent": "employee_information_query",
    }


def test_hr_salary_query_authorizes_person_and_salary_with_bound_entity():
    resolved = required_remote_tool_authorizations(
        agent_name="RemoteHRAssistantAgent",
        intents=["employee_information_query", "salary_query"],
        task_profile={"entities": {"employee_name": "李娜"}},
    )

    assert [item.tool_name for item in resolved] == [
        "remote_person_info_tool",
        "remote_salary_info_tool",
    ]
    assert resolved[1].arguments == {
        "employee_name": "李娜",
        "intent": "salary_query",
    }


def test_same_intent_on_wrong_agent_does_not_grant_hidden_tool():
    resolved = required_remote_tool_authorizations(
        agent_name="RemoteDocumentGeneratorAgent",
        intents=["salary_query"],
        task_profile={"entities": {"employee_name": "李娜"}},
    )

    assert resolved == []


def test_travel_and_calendar_choose_read_or_write_resource():
    profile = {"entities": {"employee_name": "李娜"}}

    travel_read = required_remote_tool_authorizations(
        agent_name="RemoteOfficeAssistantAgent",
        intents=["travel_service"],
        task_profile=profile,
        operation_mode="read",
    )
    travel_write = required_remote_tool_authorizations(
        agent_name="RemoteOfficeAssistantAgent",
        intents=["travel_service"],
        task_profile=profile,
        operation_mode="write",
    )
    calendar_read = required_remote_tool_authorizations(
        agent_name="RemoteHRCalendarAgent",
        intents=["schedule_management"],
        task_profile=profile,
        operation_mode="read",
    )
    calendar_write = required_remote_tool_authorizations(
        agent_name="RemoteHRCalendarAgent",
        intents=["meeting_arrangement"],
        task_profile=profile,
        operation_mode="write",
    )

    assert [item.tool_name for item in travel_read] == ["query_travel_record"]
    assert [item.tool_name for item in travel_write] == ["save_travel_record"]
    assert [item.tool_name for item in calendar_read] == ["get_calendar_events_tool"]
    assert [item.tool_name for item in calendar_write] == ["create_calendar_event_tool"]


def test_communication_step_authorizes_contact_lookup_and_send():
    resolved = required_remote_tool_authorizations(
        agent_name="RemoteCommunicationAgent",
        intents=["message_or_email_send"],
        task_profile={"entities": {"recipient": "王经理"}},
        operation_mode="send",
    )

    assert [item.tool_name for item in resolved] == [
        "remote_contact_query_tool",
        "remote_email_tool",
    ]


def test_remote_agent_rejects_tool_outside_request_manifest():
    token = bind_authorized_remote_tools(
        {
            "authorized_remote_tools": [
                {"tool_name": "remote_person_info_tool"}
            ]
        }
    )
    try:
        with pytest.raises(PermissionError, match="outside the platform-authorized"):
            asyncio.run(
                BaseRemoteAgent.call_tool(
                    object(),
                    "remote_salary_info_tool",
                    {"employee_name": "李娜"},
                )
            )
    finally:
        reset_authorized_remote_tools(token)
