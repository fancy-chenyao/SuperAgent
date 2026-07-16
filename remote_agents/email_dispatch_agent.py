#!/usr/bin/env python
"""Email Dispatch Agent - sends emails."""

from typing import Any, Dict, List
import logging

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class RemoteEmailDispatchAgent(BaseRemoteAgent):
    """Email Dispatch Agent for sending emails."""

    def __init__(self):
        super().__init__(
            name="RemoteEmailDispatchAgent",
            prompt=(
                "You are an email dispatcher. Your task is to send emails by extracting "
                "recipient address and content from previous agent results in the conversation history.\n\n"
                "IMPORTANT:\n"
                "- The 'to' field MUST be extracted from previous RemoteCommunicationAgent results. "
                "Look for 'email' field in the contact query result JSON.\n"
                "- The 'body' field MUST be extracted from previous RemoteReportAgent results "
                "(the markdown report content).\n"
                "- The 'subject' field should be derived from the user's original request or the report title."
            )
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """Execute email sending - single tool agent."""
        if not tools or len(tools) == 0:
            return {"error": "No tools provided"}

        tool = tools[0]
        tool_name = tool.get("name", "unknown")

        logger.info(f"[{self.name}] Extracting parameters for {tool_name}")
        arguments = await parameter_extractor.extract(
            agent_name=self.name,
            agent_prompt=self.prompt,
            tool=tool,
            messages=messages
        )

        logger.info(f"[{self.name}] Calling {tool_name}")
        result = await self.call_tool(
            tool_name=tool_name,
            arguments=arguments
        )

        return result
