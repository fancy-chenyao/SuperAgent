import asyncio
from pathlib import Path
from typing import Any, Dict, Iterable, List

from src.interface.agent import Agent, AgentSource, LLMType
from src.interface.mcp import Tool
from src.manager.registry.resource_registry import ResourceRegistry, ResourceSpec


def _safe_attr(obj: Any, name: str, default=None):
    return getattr(obj, name, default)


async def sync_local_resources(
    agent_registry,
    tool_registry,
    skills_manager,
    resource_registry: ResourceRegistry,
) -> int:
    """Sync local agents/tools/skills into ResourceRegistry."""

    count = 0

    # Agents
    agents = await agent_registry.list() if hasattr(agent_registry, "list") else []
    for agent in agents:
        spec = ResourceSpec(
            type="agent",
            name=_safe_attr(agent, "agent_name", ""),
            server_id="local",
            version="1.0.0",
            endpoint=_safe_attr(agent, "endpoint", None),
            protocol=_safe_attr(agent, "source", "local"),
            metadata={
                "description": _safe_attr(agent, "description", ""),
                "llm_type": str(_safe_attr(agent, "llm_type", LLMType.BASIC)),
                "prompt": _safe_attr(agent, "prompt", ""),
            },
        )
        await resource_registry.register(spec, persist=False)
        count += 1

    # Tools
    if tool_registry is not None and hasattr(tool_registry, "list_global_tools"):
        tool_metas = await tool_registry.list_global_tools()
        for meta in tool_metas:
            tool_obj = _safe_attr(meta, "tool", None)
            tool_name = _safe_attr(meta, "identifier", None)
            name = _safe_attr(tool_obj, "name", None) or _safe_attr(tool_name, "name", "")
            if not name:
                continue
            spec = ResourceSpec(
                type="tool",
                name=name,
                server_id="local",
                version=_safe_attr(meta, "version", "1.0.0"),
                protocol="local",
                metadata={
                    "description": _safe_attr(meta, "description", "") or _safe_attr(tool_obj, "description", ""),
                },
            )
            await resource_registry.register(spec, persist=False)
            count += 1

    # Skills
    skills = skills_manager.list_skills() if skills_manager else []
    for skill in skills:
        name = _safe_attr(skill, "name", "")
        if not name:
            continue
        spec = ResourceSpec(
            type="skill",
            name=name,
            server_id="local",
            version="1.0.0",
            protocol="local",
            metadata={
                "description": _safe_attr(skill, "description", ""),
                "category": _safe_attr(skill, "category", ""),
            },
        )
        await resource_registry.register(spec, persist=False)
        count += 1

    return count


async def sync_remote_agents(resource_registry: ResourceRegistry, agent_registry, default_user_id: str = "share") -> int:
    """Create in-memory Agent entries from remote ResourceSpec entries."""

    count = 0
    remote_specs = await resource_registry.list(type="agent")

    for spec in remote_specs:
        if spec.server_id == "local":
            continue
        if not spec.endpoint:
            continue

        agent_name = spec.name
        existing = await agent_registry.get(agent_name)
        if existing is not None:
            continue

        metadata = dict(spec.metadata or {})
        description = metadata.get("description", f"Remote agent from {spec.server_id}")
        llm_type = metadata.get("llm_type", LLMType.BASIC)
        prompt = metadata.get("prompt", "remote agent")
        tools = metadata.get("selected_tools", [])
        selected_tools: List[Tool] = []
        for item in tools:
            if isinstance(item, Tool):
                selected_tools.append(item)
            elif isinstance(item, dict):
                selected_tools.append(Tool(name=item.get("name", ""), description=item.get("description", "")))

        agent = Agent(
            user_id=default_user_id,
            agent_name=agent_name,
            nick_name=agent_name,
            description=description,
            llm_type=llm_type,
            selected_tools=selected_tools,
            prompt=prompt,
            source=AgentSource.REMOTE,
            endpoint=spec.endpoint,
            api_key=(spec.auth or {}).get("api_key"),
        )

        await agent_registry.register(agent, persist=False)
        count += 1

    return count
