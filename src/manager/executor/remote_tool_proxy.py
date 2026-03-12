import asyncio
from typing import Any, Dict, Optional

try:
    from langchain_core.tools import BaseTool
except Exception:  # pragma: no cover - fallback for older langchain
    try:
        from langchain.tools import BaseTool  # type: ignore
    except Exception:  # pragma: no cover - test env without langchain
        class BaseTool:  # type: ignore
            name: str
            description: str

            def __init__(self, name: str = "", description: str = ""):
                self.name = name
                self.description = description

            def invoke(self, *args, **kwargs):
                raise RuntimeError("BaseTool.invoke is unavailable without langchain")

            async def ainvoke(self, *args, **kwargs):
                raise RuntimeError("BaseTool.ainvoke is unavailable without langchain")

from src.manager.executor.factory import execute_tool
from src.manager.registry.resource_registry import ResourceSpec


class RemoteToolProxy(BaseTool):
    """LangChain tool wrapper that dispatches to remote tool executors."""

    name: str
    description: str

    def __init__(
        self,
        spec: ResourceSpec,
        tool_registry: Any,
        description: Optional[str] = None,
    ):
        self._spec = spec
        self._tool_registry = tool_registry
        tool_desc = description or (spec.metadata or {}).get("description", "")
        super().__init__(name=spec.name, description=tool_desc or "")

    def _run(self, **kwargs: Dict[str, Any]) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun(**kwargs))
        raise RuntimeError(
            "RemoteToolProxy requires async execution when an event loop is running"
        )

    async def _arun(self, **kwargs: Dict[str, Any]) -> Any:
        return await execute_tool(self._spec, self._tool_registry, kwargs)


__all__ = ["RemoteToolProxy"]
