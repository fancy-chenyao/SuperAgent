#!/usr/bin/env python
"""Communication agent for handling contact information and email sending."""

from typing import Any, Dict, List
from .base_agent import BaseRemoteAgent
import logging

logger = logging.getLogger(__name__)


class RemoteCommunicationAgent(BaseRemoteAgent):
    """Remote communication agent for managing contacts and sending emails."""

    def __init__(self):
        super().__init__(
            name="RemoteCommunicationAgent",
            prompt="You are a professional communication officer. Your task is to query contact information and send emails to relevant personnel."
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """
        Execute the communication agent.

        Args:
            tools: List of tool definitions
            messages: Conversation history
            context: Additional context
            parameter_extractor: LLM parameter extractor

        Returns:
            Execution result
        """
        logger.info(f"Executing RemoteCommunicationAgent with {len(tools)} tools")

        # 提取工具参数
        if len(tools) == 1:
            # 单个工具，直接提取参数
            tool = tools[0]
            parameters = await parameter_extractor.extract(
                self.name,
                self.prompt,
                tool,
                messages
            )
            logger.info(f"Extracted parameters: {parameters}")

            # 调用工具
            result = await self.call_tool(
                tool_name=tool["name"],
                arguments=parameters
            )

            return {
                "status": "success",
                "message": "操作成功",
                "result": result
            }
        else:
            # 多个工具，先选择工具
            selected_tool, parameters = await parameter_extractor.select_tool_and_extract(
                self.name,
                self.prompt,
                tools,
                messages
            )
            logger.info(f"Selected tool: {selected_tool['name']}, parameters: {parameters}")

            # 调用工具
            result = await self.call_tool(
                tool_name=selected_tool["name"],
                arguments=parameters
            )

            return {
                "status": "success",
                "message": "操作成功",
                "result": result
            }
