from typing import Any

__all__ = [
    "agent_manager",
    "get_agent_manager",
    "get_tool_registry",
    "get_mcp_hot_reload_manager",
    "get_resource_registry",
    "refresh_remote_resources",
]
_AGENT_MANAGER = None


def get_agent_manager():
    """Lazily import and return the global AgentManager instance."""
    global _AGENT_MANAGER
    if _AGENT_MANAGER is None:
        from .agents import create_agent_manager

        _AGENT_MANAGER = create_agent_manager()
    return _AGENT_MANAGER


async def get_tool_registry():
    from .registry import ToolRegistry

    return await ToolRegistry.get_instance()


async def get_mcp_hot_reload_manager(config_path: str | None = None):
    from .mcp import get_mcp_hot_reload_manager as _get_hot_reload_manager

    if config_path:
        return await _get_hot_reload_manager(config_path=config_path)
    return await _get_hot_reload_manager()


async def get_resource_registry():
    from .resource import get_resource_registry as _get_resource_registry

    return await _get_resource_registry()


async def refresh_remote_resources():
    from .resource import refresh_remote_resources as _refresh_remote_resources

    return await _refresh_remote_resources()


def __getattr__(name: str) -> Any:
    if name == "agent_manager":
        return get_agent_manager()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
