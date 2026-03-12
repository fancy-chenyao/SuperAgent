import logging
from typing import Any, Dict, Optional

from .base import AgentExecutor
from .local import LocalExecutor
from .remote import RemoteExecutor
from .tool import ToolExecutor, RemoteToolExecutor
from .skill import SkillExecutor, RemoteSkillExecutor

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

LOCAL_SOURCE = "local"
REMOTE_SOURCE = "remote"


class ExecutorFactory:
    """Factory that returns local/remote executors based on agent source."""

    _executors: Dict[str, AgentExecutor] = {}
    _local_executor: Optional[LocalExecutor] = None
    _remote_executor: Optional[RemoteExecutor] = None
    _tool_executor: Optional[ToolExecutor] = None
    _remote_tool_executor: Optional[RemoteToolExecutor] = None
    _skill_executor: Optional[SkillExecutor] = None
    _remote_skill_executor: Optional[RemoteSkillExecutor] = None

    @staticmethod
    def _normalize_source(source: Any) -> str:
        if hasattr(source, "value"):
            try:
                return str(source.value)
            except Exception:
                return str(source)
        return str(source)

    @classmethod
    async def get_executor(cls, agent: Any) -> AgentExecutor:
        source = cls._normalize_source(getattr(agent, "source", LOCAL_SOURCE))
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
    async def get_tool_executor(cls, tool_registry) -> ToolExecutor:
        if cls._tool_executor is None:
            cls._tool_executor = ToolExecutor(tool_registry)
        return cls._tool_executor

    @classmethod
    async def get_remote_tool_executor(cls, timeout: float = 10.0) -> RemoteToolExecutor:
        if cls._remote_tool_executor is None:
            cls._remote_tool_executor = RemoteToolExecutor(timeout=timeout)
        return cls._remote_tool_executor

    @classmethod
    async def get_skill_executor(cls, skills_manager) -> SkillExecutor:
        if cls._skill_executor is None:
            cls._skill_executor = SkillExecutor(skills_manager)
        return cls._skill_executor

    @classmethod
    async def get_remote_skill_executor(cls, timeout: float = 10.0) -> RemoteSkillExecutor:
        if cls._remote_skill_executor is None:
            cls._remote_skill_executor = RemoteSkillExecutor(timeout=timeout)
        return cls._remote_skill_executor

    @classmethod
    async def get_executor_by_type(cls, source: str, **kwargs) -> AgentExecutor:
        source = cls._normalize_source(source)
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
        cls._tool_executor = None
        if cls._remote_tool_executor:
            await cls._remote_tool_executor.cleanup()
        cls._remote_tool_executor = None
        cls._skill_executor = None
        if cls._remote_skill_executor:
            await cls._remote_skill_executor.cleanup()
        cls._remote_skill_executor = None

    @classmethod
    async def get_executor_info(cls, agent: Any) -> Dict[str, Any]:
        source = cls._normalize_source(getattr(agent, "source", LOCAL_SOURCE))
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


async def execute_tool(
    resource_spec,
    tool_registry,
    arguments: Dict[str, Any],
) -> Any:
    protocol = getattr(resource_spec, "protocol", "local")
    if protocol == "local":
        executor = await ExecutorFactory.get_tool_executor(tool_registry)
        return await executor.execute(resource_spec.name, arguments)
    executor = await ExecutorFactory.get_remote_tool_executor()
    return await executor.execute(resource_spec.endpoint, resource_spec.name, arguments, resource_spec.auth, protocol=protocol)


async def execute_skill(
    resource_spec,
    skills_manager,
    arguments: Dict[str, Any],
) -> Any:
    protocol = getattr(resource_spec, "protocol", "local")
    if protocol == "local":
        executor = await ExecutorFactory.get_skill_executor(skills_manager)
        return await executor.execute(resource_spec.name, arguments)
    executor = await ExecutorFactory.get_remote_skill_executor()
    return await executor.execute(resource_spec.endpoint, resource_spec.name, arguments, resource_spec.auth, protocol=protocol)
