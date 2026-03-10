import logging
from typing import Any, Dict, Optional

from .base import AgentExecutor
from .local import LocalExecutor
from .remote import RemoteExecutor

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

LOCAL_SOURCE = "local"
REMOTE_SOURCE = "remote"


class ExecutorFactory:
    """Factory that returns local/remote executors based on agent source."""

    _executors: Dict[str, AgentExecutor] = {}
    _local_executor: Optional[LocalExecutor] = None
    _remote_executor: Optional[RemoteExecutor] = None

    @classmethod
    async def get_executor(cls, agent: Any) -> AgentExecutor:
        source = str(getattr(agent, "source", LOCAL_SOURCE))
        if source == REMOTE_SOURCE:
            return await cls.get_remote_executor()
        return await cls.get_local_executor()

    @classmethod
    async def get_local_executor(cls) -> LocalExecutor:
        if cls._local_executor is None:
            cls._local_executor = LocalExecutor()
            await cls._local_executor.initialize()
        return cls._local_executor

    @classmethod
    async def get_remote_executor(
        cls,
        timeout: int = 120,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> RemoteExecutor:
        cache_key = f"remote_{timeout}_{max_retries}_{retry_delay}"
        if cache_key not in cls._executors:
            remote_executor = RemoteExecutor(
                timeout=timeout,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )
            await remote_executor.initialize()
            cls._executors[cache_key] = remote_executor
            cls._remote_executor = remote_executor
        return cls._executors[cache_key]  # type: ignore[return-value]

    @classmethod
    async def get_executor_by_type(cls, source: str, **kwargs) -> AgentExecutor:
        if source == LOCAL_SOURCE:
            return await cls.get_local_executor()
        if source == REMOTE_SOURCE:
            return await cls.get_remote_executor(**kwargs)
        raise ValueError(f"Unknown executor type: {source}")

    @classmethod
    async def cleanup(cls):
        if cls._local_executor:
            await cls._local_executor.cleanup()
            cls._local_executor = None

        for executor in cls._executors.values():
            await executor.cleanup()

        cls._executors.clear()
        cls._remote_executor = None

    @classmethod
    async def get_executor_info(cls, agent: Any) -> Dict[str, Any]:
        source = str(getattr(agent, "source", LOCAL_SOURCE))
        info = {
            "source": source,
            "agent_name": getattr(agent, "agent_name", "unknown"),
        }
        if source == REMOTE_SOURCE:
            info["endpoint"] = getattr(agent, "endpoint", None)
            info["has_api_key"] = bool(getattr(agent, "api_key", None))
        return info


async def execute_agent(agent: Any, messages: list, context: Any) -> Any:
    executor = await ExecutorFactory.get_executor(agent)
    return await executor.execute(agent, messages, context)
