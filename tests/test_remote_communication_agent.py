from __future__ import annotations

import asyncio
import json

import mock_remote_tool_skill as tool_server
from remote_agents.communication_agent import RemoteCommunicationAgent


def test_contact_tool_resolves_all_participants_from_internal_directory() -> None:
    request = tool_server.ToolRequest(
        tool="remote_contact_query_tool",
        arguments={"names": ["王经理", "李娜"]},
    )

    result = asyncio.run(tool_server.tool(request))["result"]

    assert result["status"] == "success"
    assert result["unresolved_names"] == []
    assert {item["email"] for item in result["contacts"]} == {
        "wangjing@ccb.com",
        "lina@ccb.com",
    }


def test_communication_agent_queries_contacts_then_sends_notification() -> None:
    class Extractor:
        async def extract(self, **kwargs):
            assert kwargs["tool"]["name"] == "remote_email_tool"
            return {"subject": "会议通知", "body": "会议时间为 2026-07-27 10:00。"}

    agent = RemoteCommunicationAgent()
    calls = []

    async def fake_call_tool(*, tool_name, arguments, **_kwargs):
        calls.append((tool_name, arguments))
        if tool_name == "remote_contact_query_tool":
            return {
                "status": "success",
                "contacts": [
                    {"name": "王静", "email": "wangjing@ccb.com"},
                    {"name": "李娜", "email": "lina@ccb.com"},
                ],
                "unresolved_names": [],
            }
        return {"status": "success", "sent": arguments}

    agent.call_tool = fake_call_tool
    execution_brief = {
        "assigned_steps": [{"intent": "message_or_email_send", "title": "通知参会人"}],
        "task_profile": {
            "entities": {
                "recipient": "参会人",
                "people": ["王经理", "李娜"],
            }
        },
    }
    messages = [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n" + json.dumps(execution_brief, ensure_ascii=False),
        }
    ]
    tools = [
        {"name": "remote_contact_query_tool", "parameters": {}},
        {"name": "remote_email_tool", "parameters": {}},
    ]

    result = asyncio.run(agent.execute(tools, messages, {}, Extractor()))

    assert result["status"] == "success"
    assert result["recipients"] == ["王经理", "李娜"]
    assert [item[0] for item in calls] == [
        "remote_contact_query_tool",
        "remote_email_tool",
    ]
    assert calls[1][1]["to"] == "wangjing@ccb.com,lina@ccb.com"
