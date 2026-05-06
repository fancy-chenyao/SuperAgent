from __future__ import annotations

import asyncio
from typing import Any, Dict

try:
    from pydantic import PrivateAttr
except Exception:  # pragma: no cover
    PrivateAttr = None  # type: ignore

try:
    from langchain_core.tools import BaseTool
except Exception:  # pragma: no cover
    from langchain.tools import BaseTool  # type: ignore

from src.security.enforcement import enforce_tool_call


class SecureToolWrapper(BaseTool):
    name: str
    description: str = ""
    args_schema: Any = None

    if PrivateAttr is not None:
        _tool: Any = PrivateAttr()
        _agent: Any = PrivateAttr()
        _context: Any = PrivateAttr()

    def __init__(self, tool: Any, agent: Any, context: Any):
        super().__init__(
            name=getattr(tool, "name", ""),
            description=getattr(tool, "description", "") or "",
            args_schema=getattr(tool, "args_schema", None),
        )
        self._tool = tool
        self._agent = agent
        self._context = context

    def _run(self, **kwargs: Dict[str, Any]) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun(**kwargs))
        raise RuntimeError("SecureToolWrapper requires async execution when an event loop is running")

    async def _arun(self, **kwargs: Dict[str, Any]) -> Any:
        await enforce_tool_call(
            agent=self._agent,
            tool_name=self.name,
            arguments=kwargs,
            context=self._context,
            tool=self._tool,
        )
        if hasattr(self._tool, "ainvoke"):
            return await self._tool.ainvoke(kwargs)
        if hasattr(self._tool, "invoke"):
            return self._tool.invoke(kwargs)
        if hasattr(self._tool, "_arun"):
            return await self._tool._arun(**kwargs)
        if hasattr(self._tool, "_run"):
            return self._tool._run(**kwargs)
        raise RuntimeError(f"Tool not invokable: {self.name}")


def wrap_tools_for_agent(tools: list[Any], agent: Any, context: Any) -> list[Any]:
    return [SecureToolWrapper(tool, agent, context) for tool in tools]
