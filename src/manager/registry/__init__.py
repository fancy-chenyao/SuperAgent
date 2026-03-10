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
]


def __getattr__(name: str) -> Any:
    if name == "AgentRegistry":
        from .agent_registry import AgentRegistry

        return AgentRegistry
    if name in {"ToolLoader", "load_tools_to_registry", "get_tools_for_agent"}:
        from .tool_loader import ToolLoader, get_tools_for_agent, load_tools_to_registry

        mapping = {
            "ToolLoader": ToolLoader,
            "load_tools_to_registry": load_tools_to_registry,
            "get_tools_for_agent": get_tools_for_agent,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
