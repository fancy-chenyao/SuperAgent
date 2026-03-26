import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except Exception:  # pragma: no cover - optional dependency in lightweight test env
    class MultiServerMCPClient:  # type: ignore
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("langchain_mcp_adapters is required for MCP tools")

from src.manager.mcp import mcp_client_config
from src.manager.registry import ToolIdentifier, ToolRegistry, ToolScope, ToolServer
from src.service.env import USE_BROWSER, USE_MCP_TOOLS

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


def _get_builtin_tool_instances() -> List[Any]:
    try:
        from src.tools import (
            avatar_tool,
            bash_tool,
            browser_tool,
            crawl_tool,
            # person_info_query_tool,  # Disabled: use remote_person_info_tool instead
            python_repl_tool,
            tavily_tool,
            web_preview_tool,
            write_file_tool,
        )

        return [
            bash_tool,
            browser_tool,
            crawl_tool,
            python_repl_tool,
            tavily_tool,
            write_file_tool,
            avatar_tool,
            web_preview_tool,
            # person_info_query_tool,  # Disabled: use remote_person_info_tool instead
        ]
    except Exception as e:  # pragma: no cover - optional dependency in lightweight test env
        logger.warning("Failed to import builtin tools: %s", e)
        return []


class ToolLoader:
    """Load built-in and MCP tools into ToolRegistry."""

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        load_timeout: float = 10.0,
        cache_ttl_seconds: float = 5.0,
    ):
        self.registry = registry
        self.load_timeout = load_timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self._client_pool: Dict[str, Any] = {}
        self._mcp_tools_cache: Dict[str, Any] = {
            "tools": [],
            "loaded_at": 0.0,
            "config_hash": "",
        }
        self._mcp_lock = asyncio.Lock()

    async def get_registry(self) -> ToolRegistry:
        if self.registry is None:
            self.registry = await ToolRegistry.get_instance()
        return self.registry

    async def load_builtin_tools(self) -> int:
        registry = await self.get_registry()
        loaded_count = 0

        for tool_instance in _get_builtin_tool_instances():
            tool_name = getattr(tool_instance, "name", "")
            if not tool_name:
                continue
            if not USE_BROWSER and tool_name == "browser":
                continue

            identifier = ToolIdentifier(
                scope=ToolScope.GLOBAL,
                server=ToolServer.BUILTIN,
                name=tool_name,
            )
            await registry.register_tool(
                identifier=identifier,
                tool=tool_instance,
                description=getattr(tool_instance, "description", ""),
            )
            loaded_count += 1

        logger.info("Loaded %s builtin tools", loaded_count)
        return loaded_count

    async def load_mcp_tools(self) -> int:
        if not USE_MCP_TOOLS:
            logger.info("MCP tools disabled by configuration")
            return 0

        registry = await self.get_registry()

        try:
            config = mcp_client_config()
            if not config:
                logger.warning("No MCP configuration found")
                return 0

            async with self._mcp_lock:
                config_hash = self._hash_config(config)
                now = time.time()
                if (
                    self._mcp_tools_cache["config_hash"] == config_hash
                    and (now - float(self._mcp_tools_cache["loaded_at"])) < self.cache_ttl_seconds
                ):
                    mcp_tools = list(self._mcp_tools_cache["tools"])
                else:
                    client = await self._get_or_create_client(config, config_hash)
                    mcp_tools = await asyncio.wait_for(client.get_tools(), timeout=self.load_timeout)
                    self._mcp_tools_cache = {
                        "tools": list(mcp_tools),
                        "loaded_at": now,
                        "config_hash": config_hash,
                    }

            loaded_count = 0
            for mcp_tool in mcp_tools:
                tool_name = getattr(mcp_tool, "name", "")
                if not tool_name:
                    continue

                identifier = ToolIdentifier(
                    scope=ToolScope.GLOBAL,
                    server=self._get_mcp_server_name(tool_name),
                    name=tool_name,
                )
                await registry.register_tool(
                    identifier=identifier,
                    tool=mcp_tool,
                    description=getattr(mcp_tool, "description", "") or "",
                )
                loaded_count += 1

            logger.info("Loaded %s MCP tools", loaded_count)
            return loaded_count
        except asyncio.TimeoutError:
            logger.error("MCP tool loading timed out after %ss", self.load_timeout)
            return 0
        except Exception as e:
            logger.error("Failed to load MCP tools: %s", e)
            return 0

    async def load_agent_mcp_tools(self, agent_name: str, mcp_config: dict) -> int:
        registry = await self.get_registry()

        servers = (mcp_config or {}).get("mcp_servers")
        if not servers:
            return 0

        loaded_count = 0
        try:
            config_hash = self._hash_config(servers)
            mcp_client = await self._get_or_create_client(servers, config_hash)
            mcp_tools = await asyncio.wait_for(mcp_client.get_tools(), timeout=self.load_timeout)

            for mcp_tool in mcp_tools:
                tool_name = getattr(mcp_tool, "name", "")
                if not tool_name:
                    continue

                identifier = ToolIdentifier(
                    scope=ToolScope.AGENT,
                    server=self._get_mcp_server_name(tool_name),
                    name=tool_name,
                )
                await registry.register_agent_tool(
                    agent_name=agent_name,
                    identifier=identifier,
                    tool=mcp_tool,
                    description=getattr(mcp_tool, "description", "") or "",
                )
                loaded_count += 1

            logger.info("Loaded %s MCP tools for agent: %s", loaded_count, agent_name)
            return loaded_count
        except asyncio.TimeoutError:
            logger.error(
                "Loading MCP tools for agent %s timed out after %ss",
                agent_name,
                self.load_timeout,
            )
            return 0
        except Exception as e:
            logger.error("Failed to load MCP tools for agent %s: %s", agent_name, e)
            return 0

    async def load_all_tools(self) -> dict:
        builtin_count = await self.load_builtin_tools()
        mcp_count = await self.load_mcp_tools()
        return {"builtin": builtin_count, "mcp": mcp_count, "total": builtin_count + mcp_count}

    def _get_mcp_server_name(self, tool_name: str) -> str:
        if "_" in tool_name:
            return tool_name.split("_")[0]
        return "mcp"

    async def reload_mcp_tools(self) -> int:
        registry = await self.get_registry()
        self._mcp_tools_cache = {"tools": [], "loaded_at": 0.0, "config_hash": ""}
        global_tools = await registry.find_tools(scope=ToolScope.GLOBAL)
        mcp_tools = [m for m in global_tools if m.identifier.is_mcp]
        for meta in mcp_tools:
            await registry.unregister_tool(meta.identifier)
        return await self.load_mcp_tools()

    @staticmethod
    def _hash_config(config: Dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    async def _get_or_create_client(self, config: Dict[str, Any], config_hash: str):
        if config_hash in self._client_pool:
            return self._client_pool[config_hash]
        client = MultiServerMCPClient(config)
        self._client_pool[config_hash] = client
        return client

    async def cleanup(self) -> None:
        for client in self._client_pool.values():
            close_fn = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close_fn is None:
                continue
            try:
                maybe_coro = close_fn()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception:
                logger.warning("Failed to close MCP client during ToolLoader cleanup")
        self._client_pool.clear()
        self._mcp_tools_cache = {"tools": [], "loaded_at": 0.0, "config_hash": ""}


async def load_tools_to_registry(registry: Optional[ToolRegistry] = None) -> dict:
    loader = ToolLoader(registry)
    return await loader.load_all_tools()


async def get_tools_for_agent(agent_name: str) -> List[Any]:
    registry = await ToolRegistry.get_instance()
    return await registry.get_tools_for_agent(agent_name)
