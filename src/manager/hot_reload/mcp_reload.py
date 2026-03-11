import asyncio
import copy
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except Exception:  # pragma: no cover - optional dependency in test env
    class MultiServerMCPClient:  # type: ignore
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("langchain_mcp_adapters is required for MCP reload")

from src.manager.registry import ToolIdentifier, ToolMetadata, ToolRegistry, ToolScope

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


@dataclass
class MCPVersionSnapshot:
    """Snapshot of MCP-related registry state for rollback."""

    version: int
    created_at: float
    config_hash: str
    file_mtime: float
    global_tools: List[ToolMetadata] = field(default_factory=list)
    agent_tools: Dict[str, List[ToolMetadata]] = field(default_factory=dict)
    source_config: Dict[str, Any] = field(default_factory=dict)

    def clone_deep(self) -> "MCPVersionSnapshot":
        """Deep-copy snapshot metadata while preserving runtime tool objects."""

        def _clone_meta(meta: ToolMetadata) -> ToolMetadata:
            return ToolMetadata(
                identifier=ToolIdentifier(
                    scope=meta.identifier.scope,
                    server=meta.identifier.server,
                    name=meta.identifier.name,
                ),
                tool=meta.tool,
                description=meta.description,
                version=meta.version,
                tags=list(meta.tags),
            )

        return MCPVersionSnapshot(
            version=self.version,
            created_at=self.created_at,
            config_hash=self.config_hash,
            file_mtime=self.file_mtime,
            global_tools=[_clone_meta(m) for m in self.global_tools],
            agent_tools={k: [_clone_meta(m) for m in v] for k, v in self.agent_tools.items()},
            source_config=copy.deepcopy(self.source_config),
        )


class MCPHotReloadManager:
    """Hot-reload manager for MCP tools with snapshot rollback."""

    def __init__(
        self,
        registry: ToolRegistry,
        config_path: str,
        max_retries: int = 3,
        retry_delay: float = 0.5,
        load_timeout: float = 10.0,
    ):
        self.registry = registry
        self.config_path = Path(config_path)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.load_timeout = load_timeout

        self._lock = asyncio.Lock()
        self._pool_lock = asyncio.Lock()
        self._version = 0
        self._last_hash = ""
        self._last_mtime = 0.0
        self._last_agent_hash = ""
        self._last_snapshot: Optional[MCPVersionSnapshot] = None
        self._agent_mcp_configs: Dict[str, Dict[str, Any]] = {}
        self._watch_task: Optional[asyncio.Task] = None
        self._client_pool: Dict[str, Any] = {}

    @property
    def version(self) -> int:
        return self._version

    def register_agent_mcp_config(self, agent_name: str, mcp_servers: Dict[str, Any]) -> None:
        self._agent_mcp_configs[agent_name] = copy.deepcopy(mcp_servers)

    def unregister_agent_mcp_config(self, agent_name: str) -> None:
        self._agent_mcp_configs.pop(agent_name, None)

    async def _load_config(self) -> Dict[str, Any]:
        def _safe_load(path: Path) -> Dict[str, Any]:
            try:
                with path.open("r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception:
                logger.error("Invalid MCP config JSON: %s", path)
                return {}

        def _merge_sources(data: Dict[str, Any], base_dir: Path) -> Dict[str, Any]:
            merged: Dict[str, Any] = {}
            for item in data.get("sources", []):
                if not isinstance(item, dict):
                    continue
                file_path = item.get("file")
                if not file_path:
                    continue
                path = Path(file_path)
                if not path.is_absolute():
                    path = base_dir / path
                if not path.exists():
                    continue
                sub = _safe_load(path)
                servers = sub.get("mcpServers")
                if isinstance(servers, dict):
                    merged.update(servers)
            inline = data.get("mcpServers")
            if isinstance(inline, dict):
                merged.update(inline)
            return merged

        if self.config_path.exists():
            data = _safe_load(self.config_path)
            if isinstance(data.get("sources"), list):
                return _merge_sources(data, self.config_path.parent)
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                return servers

        sources_path = self.config_path.parent / "mcp_sources.json"
        if sources_path.exists():
            data = _safe_load(sources_path)
            if isinstance(data.get("sources"), list):
                return _merge_sources(data, sources_path.parent)
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                return servers

        return {}

    def _build_client_config(self, servers: Dict[str, Any]) -> Dict[str, Any]:
        client_config: Dict[str, Any] = {}

        for server_name, value in servers.items():
            if not isinstance(value, dict):
                continue

            config = value.copy()
            env_config = config.get("env", {})
            if isinstance(env_config, dict):
                for env_key, env_value in env_config.items():
                    if env_value is not None:
                        os.environ[env_key] = str(env_value)

            if "url" in config:
                config["transport"] = "sse"
                key_value = None
                if isinstance(env_config, dict):
                    for val in env_config.values():
                        if val:
                            key_value = str(val)
                            break

                if key_value:
                    url = str(config["url"])
                    if "key=" not in url:
                        sep = "&" if "?" in url else "?"
                        config["url"] = f"{url}{sep}key={key_value}"
                client_config[server_name] = config
            elif "command" in config:
                config["transport"] = "stdio"
                config.setdefault("args", [])
                client_config[server_name] = config

        return client_config

    async def _load_mcp_tools(
        self,
        servers: Dict[str, Any],
        scope: str,
        agent_name: Optional[str] = None,
    ) -> List[ToolMetadata]:
        if not servers:
            return []

        client_config = self._build_client_config(servers)
        if not client_config:
            return []

        config_hash = hashlib.sha256(
            json.dumps(client_config, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        client = await self._get_or_create_client(config_hash, client_config)
        tools = await asyncio.wait_for(client.get_tools(), timeout=self.load_timeout)

        tool_metas: List[ToolMetadata] = []
        for tool in tools:
            tool_name = getattr(tool, "name", "")
            if not tool_name:
                continue
            server = tool_name.split("_")[0] if "_" in tool_name else "mcp"
            identifier = ToolIdentifier(scope=scope, server=server, name=tool_name)
            tool_metas.append(
                ToolMetadata(identifier=identifier, tool=tool, description=getattr(tool, "description", "") or "", tags=[f"server:{server}"])
            )

        return tool_metas

    async def _get_or_create_client(self, config_hash: str, client_config: Dict[str, Any]):
        if config_hash in self._client_pool:
            return self._client_pool[config_hash]
        async with self._pool_lock:
            if config_hash not in self._client_pool:
                self._client_pool[config_hash] = MultiServerMCPClient(client_config)
            return self._client_pool[config_hash]

    def _validate_tools(self, tools: List[ToolMetadata]) -> bool:
        names: Dict[str, str] = {}
        for meta in tools:
            name = meta.identifier.name
            if not name:
                return False
            server = meta.identifier.server
            if name in names and names[name] == server:
                return False
            names[name] = server
        return True

    def _agent_config_hash(self) -> str:
        payload = json.dumps(self._agent_mcp_configs, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _has_changes(self, force: bool = False) -> bool:
        if force:
            return True
        file_hash, mtime = self._compute_fingerprint()

        agent_hash = self._agent_config_hash()
        changed = (file_hash != self._last_hash) or (mtime != self._last_mtime) or (agent_hash != self._last_agent_hash)
        return changed

    def _compute_fingerprint(self) -> tuple[str, float]:
        def _safe_load(path: Path) -> Dict[str, Any]:
            try:
                with path.open("r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception:
                return {}

        def _extract_sources(data: Dict[str, Any]) -> List[Path]:
            files: List[Path] = []
            for item in data.get("sources", []):
                if not isinstance(item, dict):
                    continue
                file_path = item.get("file")
                if not file_path:
                    continue
                path = Path(file_path)
                if not path.is_absolute():
                    path = base_dir / path
                files.append(path)
            return files

        files: List[Path] = []
        base_dir = self.config_path.parent
        if self.config_path.exists():
            files.append(self.config_path)
            data = _safe_load(self.config_path)
            if isinstance(data.get("sources"), list):
                files.extend(_extract_sources(data))
            elif not isinstance(data.get("mcpServers"), dict):
                sources_path = base_dir / "mcp_sources.json"
                if sources_path.exists():
                    files.append(sources_path)
                    data = _safe_load(sources_path)
                    if isinstance(data.get("sources"), list):
                        files.extend(_extract_sources(data))
        else:
            sources_path = base_dir / "mcp_sources.json"
            if sources_path.exists():
                files.append(sources_path)
                data = _safe_load(sources_path)
                if isinstance(data.get("sources"), list):
                    files.extend(_extract_sources(data))

        fingerprint: List[Dict[str, Any]] = []
        max_mtime = 0.0
        for path in files:
            if not path.exists():
                continue
            raw = path.read_bytes()
            mtime = path.stat().st_mtime
            max_mtime = max(max_mtime, mtime)
            fingerprint.append(
                {
                    "path": str(path),
                    "hash": hashlib.sha256(raw).hexdigest(),
                    "mtime": mtime,
                }
            )

        payload = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest(), max_mtime

    async def _capture_snapshot(self, config_hash: str, file_mtime: float, source_config: Dict[str, Any]) -> MCPVersionSnapshot:
        global_tools = await self.registry.list_global_tools()
        global_mcp = [m for m in global_tools if m.identifier.is_mcp]

        agent_mcp: Dict[str, List[ToolMetadata]] = {}
        for agent_name in self._agent_mcp_configs.keys():
            metas = await self.registry.list_agent_tools(agent_name)
            agent_mcp[agent_name] = [m for m in metas if m.identifier.is_mcp]

        snapshot = MCPVersionSnapshot(
            version=self._version,
            created_at=time.time(),
            config_hash=config_hash,
            file_mtime=file_mtime,
            global_tools=global_mcp,
            agent_tools=agent_mcp,
            source_config=copy.deepcopy(source_config),
        )
        return snapshot.clone_deep()

    async def _clear_current_mcp_tools(self) -> None:
        global_tools = await self.registry.list_global_tools()
        for meta in global_tools:
            if meta.identifier.is_mcp:
                await self.registry.unregister_tool(meta.identifier)

        for agent_name in list(self._agent_mcp_configs.keys()):
            agent_tools = await self.registry.list_agent_tools(agent_name)
            for meta in agent_tools:
                if meta.identifier.is_mcp:
                    await self.registry.unregister_agent_tool(agent_name, meta.identifier)

    async def _apply_tools(
        self,
        global_tools: List[ToolMetadata],
        agent_tools: Dict[str, List[ToolMetadata]],
    ) -> None:
        for meta in global_tools:
            await self.registry.register_tool(
                identifier=meta.identifier,
                tool=meta.tool,
                description=meta.description,
                version=meta.version,
                tags=list(meta.tags),
            )

        for agent_name, metas in agent_tools.items():
            for meta in metas:
                await self.registry.register_agent_tool(
                    agent_name=agent_name,
                    identifier=meta.identifier,
                    tool=meta.tool,
                    description=meta.description,
                    version=meta.version,
                    tags=list(meta.tags),
                )

    async def _rollback(self, snapshot: MCPVersionSnapshot) -> None:
        logger.warning("MCP reload failed. Rolling back to version=%s", snapshot.version)
        await self._clear_current_mcp_tools()
        await self._apply_tools(snapshot.global_tools, snapshot.agent_tools)

        self._version = snapshot.version
        self._last_hash = snapshot.config_hash
        self._last_mtime = snapshot.file_mtime
        self._last_agent_hash = self._agent_config_hash()

    async def reload(self, force: bool = False) -> bool:
        async with self._lock:
            if not await self._has_changes(force=force):
                return False

            file_hash, file_mtime = self._compute_fingerprint()

            source_config = await self._load_config()
            snapshot = await self._capture_snapshot(file_hash, file_mtime, source_config)
            self._last_snapshot = snapshot

            for attempt in range(1, self.max_retries + 1):
                try:
                    global_tools = await self._load_mcp_tools(source_config, ToolScope.GLOBAL)
                    if not self._validate_tools(global_tools):
                        raise ValueError("Invalid global MCP tools")

                    agent_tools: Dict[str, List[ToolMetadata]] = {}
                    for agent_name, mcp_servers in self._agent_mcp_configs.items():
                        loaded = await self._load_mcp_tools(
                            mcp_servers,
                            ToolScope.AGENT,
                            agent_name=agent_name,
                        )
                        if not self._validate_tools(loaded):
                            raise ValueError(f"Invalid MCP tools for agent {agent_name}")
                        agent_tools[agent_name] = loaded

                    await self._clear_current_mcp_tools()
                    await self._apply_tools(global_tools, agent_tools)

                    self._version += 1
                    self._last_hash = file_hash
                    self._last_mtime = file_mtime
                    self._last_agent_hash = self._agent_config_hash()
                    logger.info("MCP reload succeeded. version=%s", self._version)
                    return True
                except Exception as e:
                    logger.error("MCP reload attempt %s/%s failed: %s", attempt, self.max_retries, e)
                    if attempt >= self.max_retries:
                        await self._rollback(snapshot)
                        raise
                    await asyncio.sleep(self.retry_delay * attempt)

            return False

    async def start_watch(self, interval_seconds: float = 2.0) -> None:
        """Optional file watch loop using polling."""
        if self._watch_task and not self._watch_task.done():
            return

        async def _watch_loop():
            while True:
                try:
                    await self.reload(force=False)
                except Exception as e:
                    logger.error("MCP watch reload failed: %s", e)
                await asyncio.sleep(interval_seconds)

        self._watch_task = asyncio.create_task(_watch_loop())

    async def stop_watch(self) -> None:
        if self._watch_task is None:
            return
        self._watch_task.cancel()
        try:
            await self._watch_task
        except asyncio.CancelledError:
            pass
        self._watch_task = None

    async def cleanup(self) -> None:
        await self.stop_watch()
        async with self._pool_lock:
            for client in self._client_pool.values():
                close_fn = getattr(client, "aclose", None) or getattr(client, "close", None)
                if close_fn is None:
                    continue
                try:
                    maybe_coro = close_fn()
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
                except Exception:
                    logger.warning("Failed to close MCP client during cleanup")
            self._client_pool.clear()


__all__ = ["MCPVersionSnapshot", "MCPHotReloadManager"]

