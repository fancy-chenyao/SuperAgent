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
from src.security.trusted_recipients import (
    AmbiguousTrustedRecipientError,
    resolve_trusted_recipient_addresses,
)


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


def test_email_authorization_uses_platform_trusted_recipient_resolution():
    resolved = required_remote_tool_authorizations(
        agent_name="RemoteEmailDispatchAgent",
        intents=["message_or_email_send"],
        task_profile={"entities": {"recipient": "行长秘书"}},
        operation_mode="send",
    )

    assert resolved[0].arguments["recipient"] == "行长秘书"
    assert resolved[0].arguments["resolved_recipient_addresses"] == [
        "limishu@ccb.com"
    ]


def test_email_dispatch_accepts_trusted_name_to_email_resolution(monkeypatch):
    import httpx

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"status": "success", "message_id": "mail-1"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    authorizations = required_remote_tool_authorizations(
        agent_name="RemoteEmailDispatchAgent",
        intents=["message_or_email_send"],
        task_profile={"entities": {"recipient": "行长秘书"}},
        operation_mode="send",
    )
    token = bind_authorized_remote_tools({
        "authorized_remote_tools": [
            {"tool_name": item.tool_name, "arguments": item.arguments}
            for item in authorizations
        ]
    })
    try:
        result = asyncio.run(BaseRemoteAgent.call_tool(
            object(), "remote_email_tool",
            {"to": "limishu@ccb.com", "subject": "报告", "body": "内容"},
        ))
        assert result["message_id"] == "mail-1"
        with pytest.raises(PermissionError, match="arguments do not match"):
            asyncio.run(BaseRemoteAgent.call_tool(
                object(), "remote_email_tool",
                {"to": "attacker@example.com", "subject": "报告", "body": "内容"},
            ))
    finally:
        reset_authorized_remote_tools(token)


def test_email_dispatch_rejects_conflicting_authorized_and_forwarded_recipient(
    monkeypatch,
):
    """Every recipient-shaped field must agree with the trusted manifest."""

    import httpx

    forwarded = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"status": "success", "message_id": "mail-1"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **kwargs):
            forwarded.append(kwargs["json"]["arguments"])
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    token = bind_authorized_remote_tools(
        {
            "authorized_remote_tools": [
                {
                    "tool_name": "remote_email_tool",
                    "arguments": {
                        "resolved_recipient_addresses": ["limishu@ccb.com"],
                    },
                }
            ]
        }
    )
    try:
        with pytest.raises(PermissionError, match="arguments do not match"):
            asyncio.run(
                BaseRemoteAgent.call_tool(
                    object(),
                    "remote_email_tool",
                    {
                        "resolved_recipient_addresses": ["limishu@ccb.com"],
                        "to": "attacker@example.com",
                        "subject": "report",
                        "body": "content",
                    },
                )
            )
    finally:
        reset_authorized_remote_tools(token)

    assert forwarded == []


def test_trusted_recipient_accepts_exact_directory_email():
    assert resolve_trusted_recipient_addresses("limishu@ccb.com") == [
        "limishu@ccb.com"
    ]


def test_trusted_recipient_rejects_ambiguous_position():
    with pytest.raises(AmbiguousTrustedRecipientError, match="ambiguous"):
        resolve_trusted_recipient_addresses("综合处处长")


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


@pytest.mark.parametrize("context", [{}, {"authorized_remote_tools": []}])
def test_remote_agent_fails_closed_for_missing_or_empty_manifest(context):
    token = bind_authorized_remote_tools(context)
    try:
        with pytest.raises(PermissionError, match="outside the platform-authorized"):
            asyncio.run(
                BaseRemoteAgent.call_tool(
                    object(),
                    "remote_person_info_tool",
                    {"keyword": "Alice"},
                )
            )
    finally:
        reset_authorized_remote_tools(token)


def test_remote_agent_rejects_salary_arguments_changed_after_approval():
    token = bind_authorized_remote_tools(
        {
            "authorized_remote_tools": [
                {
                    "tool_name": "remote_salary_info_tool",
                    "arguments": {
                        "employee_name": "Alice",
                        "intent": "salary_query",
                    },
                }
            ]
        }
    )
    try:
        with pytest.raises(PermissionError, match="arguments do not match"):
            asyncio.run(
                BaseRemoteAgent.call_tool(
                    object(),
                    "remote_salary_info_tool",
                    {"employee_name": "Bob"},
                )
            )
    finally:
        reset_authorized_remote_tools(token)


def test_remote_agent_accepts_normalized_bound_salary_arguments(monkeypatch):
    import httpx

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"status": "success", "employee_name": "Alice"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    token = bind_authorized_remote_tools(
        {
            "authorized_remote_tools": [
                {
                    "tool_name": "remote_salary_info_tool",
                    "arguments": {"employee_name": " Alice ", "intent": "salary_query"},
                }
            ]
        }
    )
    try:
        result = asyncio.run(
            BaseRemoteAgent.call_tool(
                object(),
                "remote_salary_info_tool",
                {"employee_name": "alice"},
            )
        )
        assert result["status"] == "success"
    finally:
        reset_authorized_remote_tools(token)
