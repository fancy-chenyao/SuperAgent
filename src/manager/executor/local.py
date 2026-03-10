import logging
import time
from typing import Any, Dict, List, Optional

from .base import AgentExecutor, ExecuteResult, ExecutionContext, ExecutionStatus
from src.manager.registry import ToolRegistry

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


class LocalExecutor(AgentExecutor):
    """Executor for local agents using LangGraph react-agent runtime."""

    def __init__(self):
        super().__init__()
        self._tool_registry: Optional[ToolRegistry] = None
        self._agent_cache: Dict[str, Any] = {}

    async def _do_initialize(self):
        self._tool_registry = await ToolRegistry.get_instance()

    @staticmethod
    def _candidate_names(tool_name: str) -> List[str]:
        names = [tool_name]
        if tool_name.endswith("_tool"):
            names.append(tool_name[: -len("_tool")])
        else:
            names.append(f"{tool_name}_tool")
        return names

    async def load_tools(self, agent: Any) -> List[Any]:
        if self._tool_registry is None:
            await self.initialize()

        # Build a lookup table from registry metadata.
        metadata_list = await self._tool_registry.get_tools_metadata_for_agent(agent.agent_name)
        tool_by_name: Dict[str, Any] = {}
        for meta in metadata_list:
            tool_by_name[meta.identifier.name] = meta.tool
            runtime_name = getattr(meta.tool, "name", "")
            if runtime_name:
                tool_by_name[runtime_name] = meta.tool

        selected = getattr(agent, "selected_tools", []) or []
        if not selected:
            return []

        resolved: List[Any] = []
        seen = set()
        for item in selected:
            raw_name = getattr(item, "name", None)
            if raw_name is None and isinstance(item, dict):
                raw_name = item.get("name")
            if not raw_name:
                continue

            for candidate in self._candidate_names(raw_name):
                tool = tool_by_name.get(candidate)
                if tool is None:
                    continue
                tool_key = getattr(tool, "name", candidate)
                if tool_key not in seen:
                    resolved.append(tool)
                    seen.add(tool_key)
                break

        return resolved

    async def execute(
        self,
        agent: Any,
        messages: List[Any],
        context: ExecutionContext,
    ) -> ExecuteResult:
        start_time = time.time()

        try:
            await self.initialize()

            if not await self.validate(agent):
                return ExecuteResult(status=ExecutionStatus.FAILED, error="Agent validation failed")

            tools = await self.load_tools(agent)

            from langgraph.prebuilt import create_react_agent

            from src.llm.llm import get_llm_by_type
            from src.prompts.template import apply_prompt
            from src.service.env import MAX_STEPS

            llm = get_llm_by_type(agent.llm_type)
            prompt = apply_prompt(
                {"messages": messages, "deep_thinking_mode": context.deep_thinking_mode},
                agent.prompt,
            )

            react_agent = create_react_agent(llm, tools=tools, prompt=prompt)
            config = {
                "configurable": {"user_id": context.user_id},
                "recursion_limit": int(MAX_STEPS),
            }
            state = {"messages": messages}
            response = await react_agent.ainvoke(state, config=config)

            duration = time.time() - start_time
            result_messages = response.get("messages", [])
            final_message = result_messages[-1] if result_messages else None
            content = final_message.content if hasattr(final_message, "content") else str(final_message or "")

            return ExecuteResult(
                status=ExecutionStatus.SUCCESS,
                result=content,
                metadata={
                    "agent_name": agent.agent_name,
                    "duration": duration,
                    "message_count": len(result_messages),
                    "workflow_id": context.workflow_id,
                    "workflow_mode": context.workflow_mode,
                    "tool_count": len(tools),
                },
            )

        except Exception as e:
            duration = time.time() - start_time
            logger.error("Error executing local agent %s: %s", getattr(agent, "agent_name", "unknown"), e)
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error=str(e),
                metadata={
                    "agent_name": getattr(agent, "agent_name", "unknown"),
                    "duration": duration,
                },
            )

    async def execute_with_tools(
        self,
        agent: Any,
        messages: List[Any],
        tools: List[Any],
        context: ExecutionContext,
    ) -> ExecuteResult:
        start_time = time.time()

        try:
            await self.initialize()

            from langgraph.prebuilt import create_react_agent

            from src.llm.llm import get_llm_by_type
            from src.prompts.template import apply_prompt
            from src.service.env import MAX_STEPS

            llm = get_llm_by_type(agent.llm_type)
            prompt = apply_prompt(
                {"messages": messages, "deep_thinking_mode": context.deep_thinking_mode},
                agent.prompt,
            )

            react_agent = create_react_agent(llm, tools=tools, prompt=prompt)
            config = {
                "configurable": {"user_id": context.user_id},
                "recursion_limit": int(MAX_STEPS),
            }

            state = {"messages": messages}
            response = await react_agent.ainvoke(state, config=config)

            duration = time.time() - start_time
            result_messages = response.get("messages", [])
            final_message = result_messages[-1] if result_messages else None
            content = final_message.content if hasattr(final_message, "content") else str(final_message or "")

            return ExecuteResult(
                status=ExecutionStatus.SUCCESS,
                result=content,
                metadata={
                    "agent_name": agent.agent_name,
                    "duration": duration,
                    "tool_count": len(tools),
                },
            )

        except Exception as e:
            duration = time.time() - start_time
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error=str(e),
                metadata={
                    "agent_name": getattr(agent, "agent_name", "unknown"),
                    "duration": duration,
                },
            )

    async def cleanup(self):
        self._agent_cache.clear()
