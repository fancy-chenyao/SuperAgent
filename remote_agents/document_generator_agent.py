#!/usr/bin/env python
"""Document Generator Agent - generates Word documents."""

from typing import Any, Dict, List
import logging

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class RemoteDocumentGeneratorAgent(BaseRemoteAgent):
    """Document Generator Agent for creating Word documents."""

    def __init__(self):
        super().__init__(
            name="RemoteDocumentGeneratorAgent",
            prompt="You are a document generator that creates Word documents from templates."
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """Execute document generation - single tool agent."""
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
            timeout=30  # Document generation may take some time
        )

        return result
