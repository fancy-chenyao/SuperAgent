from __future__ import annotations

import asyncio

import pytest

from src.manager.registry.agent_registry import AgentRegistry
from src.manager.registry.resource_registry import ResourceRegistry, ResourceSpec
from src.manager.registry.resource_sync import sync_remote_agents
from src.orchestrator.department_router import build_agent_cards


def test_remote_contract_fields_survive_resource_sync(tmp_path) -> None:
    resources = ResourceRegistry()
    agents = AgentRegistry(tmp_path / "agents", tmp_path / "prompts")
    spec = ResourceSpec(
        type="agent",
        name="RemoteKnowledgeAgent",
        server_id="remote-demo",
        endpoint="http://127.0.0.1:8010/agent",
        metadata={
            "contract_version": "1.0",
            "requires": [],
            "produces": ["policy.info"],
            "input_schema_refs": {},
            "output_schema_refs": {"policy.info": "policy.info@v1"},
        },
    )

    async def scenario():
        await resources.register(spec, persist=False)
        assert await sync_remote_agents(resources, agents) == 1
        return await agents.get("RemoteKnowledgeAgent")

    agent = asyncio.run(scenario())

    assert agent is not None
    assert agent.contract_version == "1.0"
    assert agent.produces == ["policy.info"]
    assert agent.output_schema_refs == {"policy.info": "policy.info@v1"}
    assert agent.agent_contract.produces[0].schema_ref == "policy.info@v1"
    card = build_agent_cards([agent])[0]
    assert card.contract_version == "1.0"
    assert card.produces[0].name == "policy.info"
    assert card.output_schema_refs == {"policy.info": "policy.info@v1"}


def test_legacy_remote_agent_still_registers_without_contract(tmp_path) -> None:
    resources = ResourceRegistry()
    agents = AgentRegistry(tmp_path / "agents", tmp_path / "prompts")
    spec = ResourceSpec(
        type="agent",
        name="LegacyRemoteAgent",
        server_id="remote-demo",
        endpoint="http://127.0.0.1:8010/agent",
        metadata={"description": "legacy"},
    )

    async def scenario():
        await resources.register(spec, persist=False)
        assert await sync_remote_agents(resources, agents) == 1
        return await agents.get("LegacyRemoteAgent")

    agent = asyncio.run(scenario())

    assert agent is not None
    assert agent.agent_contract is None
    assert agent.requires == []
    assert agent.produces == []


def test_remote_contract_with_missing_schema_ref_fails_closed(tmp_path) -> None:
    resources = ResourceRegistry()
    agents = AgentRegistry(tmp_path / "agents", tmp_path / "prompts")
    spec = ResourceSpec(
        type="agent",
        name="BrokenContractAgent",
        server_id="remote-demo",
        endpoint="http://127.0.0.1:8010/agent",
        metadata={
            "contract_version": "1.0",
            "produces": ["missing.output"],
            "output_schema_refs": {},
        },
    )

    async def scenario():
        await resources.register(spec, persist=False)
        await sync_remote_agents(resources, agents)

    with pytest.raises(ValueError, match="missing schema refs"):
        asyncio.run(scenario())
