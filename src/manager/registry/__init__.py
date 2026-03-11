from typing import Any

from .tool_identifier import ToolIdentifier, ToolScope, ToolServer
from .tool_registry import (
    ToolMetadata,
    ToolRegistry,
    create_tool_identifier,
    get_tool_registry,
)

__all__ = [
    "ToolIdentifier",
    "ToolScope",
    "ToolServer",
    "ToolRegistry",
    "ToolMetadata",
    "get_tool_registry",
    "create_tool_identifier",
    "ToolLoader",
    "load_tools_to_registry",
    "get_tools_for_agent",
    "AgentRegistry",
    "ResourceRegistry",
    "ResourceSpec",
    "ResourceKey",
    "RemoteRegistryGateway",
    "RemoteRegistrySource",
    "load_remote_registry_sources",
    "sync_local_resources",
    "sync_remote_agents",
]


def __getattr__(name: str) -> Any:
    if name == "AgentRegistry":
        from .agent_registry import AgentRegistry

        return AgentRegistry
    if name in {"ResourceRegistry", "ResourceSpec", "ResourceKey"}:
        from .resource_registry import ResourceKey, ResourceRegistry, ResourceSpec

        mapping = {
            "ResourceRegistry": ResourceRegistry,
            "ResourceSpec": ResourceSpec,
            "ResourceKey": ResourceKey,
        }
        return mapping[name]
    if name in {"RemoteRegistryGateway", "RemoteRegistrySource"}:
        from .resource_gateway import RemoteRegistryGateway, RemoteRegistrySource

        mapping = {
            "RemoteRegistryGateway": RemoteRegistryGateway,
            "RemoteRegistrySource": RemoteRegistrySource,
        }
        return mapping[name]
    if name == "load_remote_registry_sources":
        from .remote_registry_config import load_remote_registry_sources

        return load_remote_registry_sources
    if name in {"sync_local_resources", "sync_remote_agents"}:
        from .resource_sync import sync_local_resources, sync_remote_agents

        mapping = {
            "sync_local_resources": sync_local_resources,
            "sync_remote_agents": sync_remote_agents,
        }
        return mapping[name]
    if name in {"ToolLoader", "load_tools_to_registry", "get_tools_for_agent"}:
        from .tool_loader import ToolLoader, get_tools_for_agent, load_tools_to_registry

        mapping = {
            "ToolLoader": ToolLoader,
            "load_tools_to_registry": load_tools_to_registry,
            "get_tools_for_agent": get_tools_for_agent,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
