#!/usr/bin/env python
"""Communication agent for handling contact information queries."""

from typing import Any, Dict, List
from .base_agent import BaseRemoteAgent
import logging

logger = logging.getLogger(__name__)


class RemoteCommunicationAgent(BaseRemoteAgent):
    """Remote communication agent for querying contact information."""

    def __init__(self):
        super().__init__(
            name="RemoteCommunicationAgent",
            prompt="You are a professional communication officer. Your task is to query contact information for personnel."
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """
        Execute the communication agent - query contact information.

        Args:
            tools: List of tool definitions
            messages: Conversation history
            context: Additional context
            parameter_extractor: LLM parameter extractor

        Returns:
            Execution result
        """
        logger.info(f"Executing RemoteCommunicationAgent with {len(tools)} tools")

        if len(tools) == 1:
            tool = tools[0]
            parameters = await parameter_extractor.extract(
                self.name,
                self.prompt,
                tool,
                messages
            )
            logger.info(f"Extracted parameters: {parameters}")

            result = await self.call_tool(
                tool_name=tool["name"],
                arguments=parameters
            )

            return {
                "status": "success",
                "message": "查询成功",
                "result": result
            }
        else:
            selected_tool, parameters = await parameter_extractor.select_tool_and_extract(
                self.name,
                self.prompt,
                tools,
                messages
            )
            logger.info(f"Selected tool: {selected_tool['name']}, parameters: {parameters}")

            result = await self.call_tool(
                tool_name=selected_tool["name"],
                arguments=parameters
            )

            return {
                "status": "success",
                "message": "查询成功",
                "result": result
            }
