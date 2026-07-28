from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
    """A broken contract rejects that Agent only, never the whole batch."""
    resources = ResourceRegistry()
    agents = AgentRegistry(tmp_path / "agents", tmp_path / "prompts")
    broken = ResourceSpec(
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
    healthy = ResourceSpec(
        type="agent",
        name="HealthyLegacyAgent",
        server_id="remote-demo",
        endpoint="http://127.0.0.1:8011/agent",
        metadata={"description": "legacy"},
    )

    async def scenario():
        await resources.register(broken, persist=False)
        await resources.register(healthy, persist=False)
        count = await sync_remote_agents(resources, agents)
        return (
            count,
            await agents.get("BrokenContractAgent"),
            await agents.get("HealthyLegacyAgent"),
        )

    count, broken_agent, healthy_agent = asyncio.run(scenario())

    assert count == 1
    assert broken_agent is None
    assert healthy_agent is not None


def test_legacy_produces_coexist_with_contract(tmp_path) -> None:
    """legacy_produces survive next to the contract without schema refs."""
    resources = ResourceRegistry()
    agents = AgentRegistry(tmp_path / "agents", tmp_path / "prompts")
    spec = ResourceSpec(
        type="agent",
        name="RemoteHRAssistantAgent",
        server_id="remote-demo",
        endpoint="http://127.0.0.1:8010/agent",
        metadata={
            "contract_version": "1.0",
            "requires": [],
            "produces": ["employee.info", "employee.salary"],
            "optional_produces": ["employee.salary"],
            "legacy_produces": ["employee.id", "employee.name"],
            "input_schema_refs": {},
            "output_schema_refs": {
                "employee.info": "employee.info@v1",
                "employee.salary": "employee.salary@v1",
            },
        },
    )

    async def scenario():
        await resources.register(spec, persist=False)
        assert await sync_remote_agents(resources, agents) == 1
        return await agents.get("RemoteHRAssistantAgent")

    agent = asyncio.run(scenario())

    assert agent is not None
    # Planner-visible produces keep the legacy dependency chain alive.
    assert agent.produces == [
        "employee.info",
        "employee.salary",
        "employee.id",
        "employee.name",
    ]
    # The strict contract only covers schema-backed outputs.
    assert [ref.name for ref in agent.agent_contract.produces] == [
        "employee.info",
        "employee.salary",
    ]
    assert [ref.required for ref in agent.agent_contract.produces] == [True, False]
    card = build_agent_cards([agent])[0]
    assert [ref.name for ref in card.produces] == [
        "employee.info",
        "employee.salary",
    ]
    assert [ref.required for ref in card.produces] == [True, False]


def test_unknown_optional_contract_name_fails_closed(tmp_path) -> None:
    resources = ResourceRegistry()
    agents = AgentRegistry(tmp_path / "agents", tmp_path / "prompts")
    spec = ResourceSpec(
        type="agent",
        name="BrokenOptionalContractAgent",
        server_id="remote-demo",
        endpoint="http://127.0.0.1:8010/agent",
        metadata={
            "contract_version": "1.0",
            "produces": ["policy.info"],
            "optional_produces": ["missing.output"],
            "output_schema_refs": {"policy.info": "policy.info@v1"},
        },
    )

    async def scenario():
        await resources.register(spec, persist=False)
        count = await sync_remote_agents(resources, agents)
        return count, await agents.get("BrokenOptionalContractAgent")

    count, agent = asyncio.run(scenario())

    assert count == 0
    assert agent is None


def test_mock_registry_requires_are_satisfiable() -> None:
    """Regression (M-1): every agent's requires must be produced by another
    agent in mock_remote_registry.json, or legacy plans become unsolvable.
    Synthetic fan-in inputs are assembled by the planner from multiple
    upstream Artifacts, so no single agent declares them in produces."""
    synthetic_fanin_inputs = {"report.sources"}
    registry_path = Path(__file__).resolve().parents[1] / "mock_remote_registry.json"
    entries = json.loads(registry_path.read_text(encoding="utf-8-sig"))["resources"]
    agents = [item for item in entries if item.get("type") == "agent"]

    all_produced: set[str] = set(synthetic_fanin_inputs)
    for item in agents:
        metadata = item.get("metadata", {})
        all_produced.update(metadata.get("produces", []))
        all_produced.update(metadata.get("legacy_produces", []))

    unsatisfiable = {
        item["name"]: sorted(
            set(item.get("metadata", {}).get("requires", [])) - all_produced
        )
        for item in agents
        if set(item.get("metadata", {}).get("requires", [])) - all_produced
    }
    assert not unsatisfiable, (
        f"registry declares requires nobody produces: {unsatisfiable}"
    )
