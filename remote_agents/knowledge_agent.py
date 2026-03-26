#!/usr/bin/env python
"""Knowledge Agent - queries HR policies and labor laws."""

from typing import Any, Dict, List
import logging

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class RemoteKnowledgeAgent(BaseRemoteAgent):
    """Knowledge Agent for querying HR policies and labor laws."""

    def __init__(self):
        super().__init__(
            name="RemoteKnowledgeAgent",
            prompt="You are a knowledge assistant that helps query HR policies and labor laws."
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """Execute knowledge query - single tool agent."""
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
            arguments=arguments,
            timeout=60  # Knowledge queries may take longer
        )

        return result
