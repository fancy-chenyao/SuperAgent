from __future__ import annotations

import json
from pathlib import Path

from remote_agents.factory import AgentFactory


def test_remote_agent_factory_implements_every_advertised_agent() -> None:
    registry_path = Path(__file__).resolve().parents[1] / "mock_remote_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    advertised = {
        item["name"]
        for item in registry.get("resources", [])
        if item.get("type") == "agent"
    }

    assert advertised == set(AgentFactory._agents)


def test_calendar_agent_is_available_to_remote_execution_server() -> None:
    agent = AgentFactory.get_agent("RemoteHRCalendarAgent")

    assert agent.name == "RemoteHRCalendarAgent"
