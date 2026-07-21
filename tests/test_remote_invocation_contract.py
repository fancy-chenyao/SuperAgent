import asyncio
import os
from types import SimpleNamespace

from src.manager.executor.base import ExecutionContext, ExecutionStatus
from src.manager.executor.remote import RemoteExecutor
from src.manager.executor.tool import RemoteToolExecutor
from src.manager.mcp import normalize_mcp_servers


def test_mcp_sse_api_key_is_exported_and_appended_to_url(monkeypatch):
    monkeypatch.delenv("DEMO_MCP_API_KEY", raising=False)

    config = normalize_mcp_servers(
        {
            "demo": {
                "url": "https://mcp.example.test/sse",
                "env": {"DEMO_MCP_API_KEY": "mcp-secret"},
            }
        }
    )

    assert os.environ["DEMO_MCP_API_KEY"] == "mcp-secret"
    assert config["demo"]["transport"] == "sse"
    assert config["demo"]["url"] == "https://mcp.example.test/sse?key=mcp-secret"


def test_remote_agent_uses_json_contract_and_bearer_auth():
    async def scenario():
        captured = {}
        executor = RemoteExecutor(max_retries=1)

        async def fake_send_request(endpoint, data, headers, retries=None):
            captured.update(endpoint=endpoint, data=data, headers=headers, retries=retries)
            return {
                "status": "success",
                "result": {"answer": 42},
                "metadata": {"server": "demo"},
            }

        executor._send_request = fake_send_request
        agent = SimpleNamespace(
            source="remote",
            agent_name="RemoteDemoAgent",
            endpoint="https://agents.example.test/agent",
            api_key="agent-secret",
            prompt="Handle the request.",
            selected_tools=[],
        )
        context = ExecutionContext(
            user_id="test-user",
            workflow_id="workflow-1",
            workflow_mode="production",
        )

        result = await executor.execute(
            agent,
            [{"role": "user", "content": "run demo"}],
            context,
        )

        assert result.status == ExecutionStatus.SUCCESS
        assert result.result == {"answer": 42}
        assert captured["endpoint"] == agent.endpoint
        assert captured["headers"]["Authorization"] == "Bearer agent-secret"
        assert captured["data"]["agent_name"] == "RemoteDemoAgent"
        assert captured["data"]["messages"] == [
            {"type": "user", "role": "user", "content": "run demo"}
        ]
        assert captured["data"]["context"]["workflow_id"] == "workflow-1"

    asyncio.run(scenario())


def test_remote_tool_uses_tool_arguments_contract_and_bearer_auth():
    async def scenario():
        captured = {}
        executor = RemoteToolExecutor(max_retries=0)

        async def fake_send_request(endpoint, payload, headers):
            captured.update(endpoint=endpoint, payload=payload, headers=headers)
            return {"status": "success", "result": {"temperature": 26}}

        executor._send_request = fake_send_request
        result = await executor.execute(
            endpoint="https://tools.example.test/tool",
            tool_name="remote_weather_tool",
            arguments={"location": "Beijing"},
            auth={"api_key": "tool-secret"},
        )

        assert result.status == ExecutionStatus.SUCCESS
        assert result.result == {"temperature": 26}
        assert captured["headers"]["Authorization"] == "Bearer tool-secret"
        assert captured["payload"] == {
            "tool": "remote_weather_tool",
            "arguments": {"location": "Beijing"},
        }

    asyncio.run(scenario())


def test_remote_agent_does_not_receive_long_term_memory_by_default():
    executor = RemoteExecutor()
    agent = SimpleNamespace(agent_name="RemoteDemoAgent", prompt="", selected_tools=[])
    context = ExecutionContext(
        user_id="test-user",
        workflow_id="workflow-1",
        workflow_mode="production",
    )

    request = executor._build_request(
        agent,
        [
            {
                "role": "assistant",
                "content": "private remembered preference",
                "metadata": {"memory_type": "long_term_reference"},
            },
            {"role": "user", "content": "run demo"},
        ],
        context,
    )

    assert request["messages"] == [
        {"type": "user", "role": "user", "content": "run demo"}
    ]
