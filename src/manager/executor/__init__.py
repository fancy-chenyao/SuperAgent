from typing import Any

from .base import AgentExecutor, ExecuteResult, ExecutionContext, ExecutionStatus, ToolCall

__all__ = [
    "AgentExecutor",
    "ExecuteResult",
    "ExecutionStatus",
    "ExecutionContext",
    "ToolCall",
    "LocalExecutor",
    "RemoteExecutor",
    "ExecutorFactory",
    "execute_agent",
]


def __getattr__(name: str) -> Any:
    if name in {"LocalExecutor", "RemoteExecutor", "ExecutorFactory", "execute_agent"}:
        from .factory import ExecutorFactory, execute_agent
        from .local import LocalExecutor
        from .remote import RemoteExecutor

        mapping = {
            "LocalExecutor": LocalExecutor,
            "RemoteExecutor": RemoteExecutor,
            "ExecutorFactory": ExecutorFactory,
            "execute_agent": execute_agent,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
