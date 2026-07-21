from fastapi.testclient import TestClient

from src.memory.models import MemoryContextMetadata, PreparedMemoryContext
from src.service.server import Server
from src.service.web_app import app


def test_web_aligned_launch_request_uses_memory_and_captures_stream(monkeypatch):
    captured = {}

    class FakeMemoryManager:
        async def prepare_context(self, **kwargs):
            captured["prepare"] = kwargs
            return PreparedMemoryContext(
                messages=(
                    {"role": "assistant", "content": "prior memory"},
                    {"role": "user", "content": "current request"},
                ),
                metadata=MemoryContextMetadata(
                    session_id="thread",
                    token_estimate=10,
                ),
            )

        async def record_assistant_outputs(self, **kwargs):
            captured["outputs"] = kwargs
            return []

    async def fake_initialize():
        return None

    async def fake_reload(force=False):
        return None

    async def fake_workflow(**kwargs):
        captured["workflow"] = kwargs
        yield {
            "event": "start_of_workflow",
            "data": {"workflow_id": "alice:wf", "task_id": "task"},
        }
        yield {
            "event": "messages",
            "agent_name": "planner",
            "data": {"delta": {"content": "plan "}},
        }
        yield {
            "event": "messages",
            "agent_name": "planner",
            "data": {"delta": {"content": "done"}},
        }
        yield {"event": "end_of_workflow", "data": {"workflow_id": "alice:wf"}}

    monkeypatch.setattr("src.service.server.get_memory_manager", lambda: FakeMemoryManager())
    monkeypatch.setattr("src.service.server.agent_manager.ensure_initialized", fake_initialize)
    monkeypatch.setattr(Server, "_trigger_mcp_reload", staticmethod(fake_reload))
    monkeypatch.setattr("src.service.server.run_agent_workflow", fake_workflow)

    instruction = "Analyze quarterly sales data and create an execution plan"
    payload = {
        "user_id": "alice",
        "lang": "zh",
        "workmode": "launch",
        "stop_after_planner": True,
        "instruction": instruction,
        "instruction_history": [instruction],
        "original_user_query": instruction,
        "messages": [{"role": "user", "content": instruction}],
        "debug": False,
        "deep_thinking_mode": False,
        "search_before_planning": False,
        "coor_agents": None,
        "workflow_id": None,
        "memory_enabled": True,
        "memory_session_id": "thread",
    }

    with TestClient(app) as client:
        response = client.post("/api/workflows/run", json=payload)

    assert response.status_code == 200
    assert "event: start_of_workflow" in response.text
    assert "event: end_of_workflow" in response.text
    assert captured["prepare"]["incoming_messages"] == [
        {
            "role": "user",
            "content": instruction,
            "message_id": None,
            "metadata": {},
        }
    ]
    assert captured["prepare"]["session_id"] == "thread"
    assert captured["prepare"]["request_enabled"] is True
    assert captured["prepare"]["retrieval_query"] == instruction
    assert captured["prepare"]["attachments"]["current_plan"] == [instruction]
    assert captured["workflow"]["user_input_messages"][0]["content"] == "prior memory"
    assert captured["workflow"]["request_input_messages"] == [
        {"role": "user", "content": instruction}
    ]
    assert captured["workflow"]["workmode"].value == "launch"
    assert captured["workflow"]["stop_after_planner"] is True
    assert captured["workflow"]["instruction"] == instruction
    assert captured["workflow"]["instruction_history"] == [instruction]
    assert captured["workflow"]["original_user_query"] == instruction
    assert captured["workflow"]["memory_session_id"] == "thread"
    assert captured["outputs"]["workflow_id"] == "alice:wf"
    assert captured["outputs"]["outputs"] == [
        {"agent_name": "planner", "content": "plan done"}
    ]
