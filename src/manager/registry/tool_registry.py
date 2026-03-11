import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .tool_identifier import ToolIdentifier, ToolScope

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


@dataclass
class ToolMetadata:
    """Metadata for a registered tool."""

    identifier: ToolIdentifier
    tool: Any
    description: str = ""
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)


class ToolRegistry:
    """Unified tool registry for global and agent-scoped tools."""

    _instance: Optional["ToolRegistry"] = None
    _class_lock: Optional[asyncio.Lock] = None

    def __init__(self):
        self._lock = asyncio.Lock()
        self._initialized = False
        self._tools: Dict[ToolIdentifier, ToolMetadata] = {}
        self._global_tools: Dict[ToolIdentifier, ToolMetadata] = {}
        self._agent_tools: Dict[str, Dict[ToolIdentifier, ToolMetadata]] = {}
        self._version: int = 0

    @classmethod
    def _get_class_lock(cls) -> asyncio.Lock:
        if cls._class_lock is None:
            cls._class_lock = asyncio.Lock()
        return cls._class_lock

    @classmethod
    async def get_instance(cls) -> "ToolRegistry":
        """Get singleton instance in a concurrency-safe way."""
        if cls._instance is None:
            async with cls._get_class_lock():
                if cls._instance is None:
                    cls._instance = cls()
                    await cls._instance._initialize()
        return cls._instance

    @classmethod
    def get_instance_sync(cls) -> "ToolRegistry":
        """Synchronous singleton accessor for compatibility."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def _initialize(self):
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return
            await self._load_builtin_tools()
            self._initialized = True
            logger.info("ToolRegistry initialized with %s built-in tools", len(self._global_tools))

    async def _load_builtin_tools(self):
        """Hook for loading built-ins during initialization."""
        return

    async def register_tool(
        self,
        identifier: ToolIdentifier,
        tool: Any,
        description: str = "",
        version: str = "1.0.0",
        tags: Optional[List[str]] = None,
    ) -> ToolMetadata:
        async with self._lock:
            metadata = ToolMetadata(
                identifier=identifier,
                tool=tool,
                description=description,
                version=version,
                tags=tags or [],
            )
            self._tools[identifier] = metadata
            self._global_tools[identifier] = metadata
            self._version += 1
            return metadata

    async def register_agent_tool(
        self,
        agent_name: str,
        identifier: ToolIdentifier,
        tool: Any,
        description: str = "",
        version: str = "1.0.0",
        tags: Optional[List[str]] = None,
    ) -> ToolMetadata:
        if identifier.scope != ToolScope.AGENT:
            raise ValueError(
                f"Agent tool must have scope='agent', got scope='{identifier.scope}'"
            )

        async with self._lock:
            if agent_name not in self._agent_tools:
                self._agent_tools[agent_name] = {}

            metadata = ToolMetadata(
                identifier=identifier,
                tool=tool,
                description=description,
                version=version,
                tags=tags or [],
            )
            self._tools[identifier] = metadata
            self._agent_tools[agent_name][identifier] = metadata
            self._version += 1
            return metadata

    async def unregister_tool(self, identifier: ToolIdentifier) -> bool:
        async with self._lock:
            if identifier not in self._tools:
                return False

            del self._tools[identifier]
            self._global_tools.pop(identifier, None)

            for tools in self._agent_tools.values():
                tools.pop(identifier, None)

            self._version += 1
            return True

    async def unregister_agent_tool(self, agent_name: str, identifier: ToolIdentifier) -> bool:
        async with self._lock:
            if agent_name not in self._agent_tools:
                return False
            if identifier not in self._agent_tools[agent_name]:
                return False

            del self._agent_tools[agent_name][identifier]
            self._tools.pop(identifier, None)
            self._version += 1
            return True

    async def get_tool(self, identifier: ToolIdentifier) -> Optional[Any]:
        metadata = self._tools.get(identifier)
        return metadata.tool if metadata else None

    async def get_tool_metadata(self, identifier: ToolIdentifier) -> Optional[ToolMetadata]:
        return self._tools.get(identifier)

    async def get_tools_for_agent(self, agent_name: str) -> List[Any]:
        async with self._lock:
            merged_by_name: Dict[str, Any] = {}

            for identifier, metadata in self._global_tools.items():
                if identifier.name in merged_by_name:
                    logger.warning(
                        "Tool name conflict: %s (server=%s) already registered, skipping",
                        identifier.name,
                        identifier.server,
                    )
                    continue
                merged_by_name[identifier.name] = metadata.tool

            if agent_name in self._agent_tools:
                for identifier, metadata in self._agent_tools[agent_name].items():
                    if identifier.name in merged_by_name:
                        logger.warning(
                            "Tool name conflict: %s (server=%s) already registered, skipping",
                            identifier.name,
                            identifier.server,
                        )
                        continue
                    merged_by_name[identifier.name] = metadata.tool

            return list(merged_by_name.values())

    async def get_tools_metadata_for_agent(self, agent_name: str) -> List[ToolMetadata]:
        async with self._lock:
            merged: Dict[ToolIdentifier, ToolMetadata] = {}

            for identifier, metadata in self._global_tools.items():
                merged[identifier] = metadata

            if agent_name in self._agent_tools:
                for identifier, metadata in self._agent_tools[agent_name].items():
                    merged[identifier] = metadata

            return list(merged.values())

    async def list_global_tools(self) -> List[ToolMetadata]:
        async with self._lock:
            return list(self._global_tools.values())

    async def list_agent_tools(self, agent_name: str) -> List[ToolMetadata]:
        async with self._lock:
            if agent_name not in self._agent_tools:
                return []
            return list(self._agent_tools[agent_name].values())

    async def list_all_tools(self) -> List[ToolMetadata]:
        async with self._lock:
            return list(self._tools.values())

    async def find_tools(
        self,
        scope: Optional[str] = None,
        server: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> List[ToolMetadata]:
        async with self._lock:
            results: List[ToolMetadata] = []
            for metadata in self._tools.values():
                if scope and metadata.identifier.scope != scope:
                    continue
                if server and metadata.identifier.server != server:
                    continue
                if tags and not any(tag in metadata.tags for tag in tags):
                    continue
                results.append(metadata)
            return results

    async def reload(self):
        async with self._lock:
            self._tools.clear()
            self._global_tools.clear()
            self._agent_tools.clear()
            self._version = 0
            await self._load_builtin_tools()
            self._initialized = True

    @property
    def version(self) -> int:
        return self._version

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    @property
    def global_tool_count(self) -> int:
        return len(self._global_tools)


async def create_tool_identifier(
    name: str, scope: str = "global", server: str = "builtin"
) -> ToolIdentifier:
    return ToolIdentifier(scope=scope, server=server, name=name)


async def get_tool_registry() -> ToolRegistry:
    return await ToolRegistry.get_instance()
