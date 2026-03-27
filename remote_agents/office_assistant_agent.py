#!/usr/bin/env python
"""Office Assistant Agent - handles leave and travel applications."""

from typing import Any, Dict, List
import logging
import json

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


class RemoteOfficeAssistantAgent(BaseRemoteAgent):
    """
    Office Assistant Agent that handles:
    - Save leave records
    - Query leave records
    - Save travel records
    - Query travel records
    """

    def __init__(self):
        super().__init__(
            name="RemoteOfficeAssistantAgent",
            prompt="""You are an office assistant that helps employees with leave and travel applications.

Your responsibilities:
1. Save leave application records
2. Query leave application records
3. Save travel application records
4. Query travel application records

Important notes:
- Extract key information from the conversation (dates, reasons, destinations, etc.)
- Convert relative dates (like "next Wednesday") to specific dates in YYYY-MM-DD format
- Today's date is <<CURRENT_DATE>>
- If information is incomplete, you should still try to extract what's available
- Extract employee_id and employee_name from previous agent results in the conversation history
"""
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """
        Execute office assistant logic:
        1. Select the appropriate tool based on user intent
        2. Extract parameters
        3. Call the tool
        4. Return result
        """
        logger.info(f"[{self.name}] Starting execution with {len(tools)} tools")

        if not tools:
            return {
                "status": "failed",
                "error": "No tools provided"
            }

        try:
            # If multiple tools, let the extractor select the best one
            if len(tools) > 1:
                logger.info(f"[{self.name}] Multiple tools available, selecting best match")
                selected_tool, params = await parameter_extractor.select_tool_and_extract(
                    agent_name=self.name,
                    agent_prompt=self.prompt,
                    tools=tools,
                    messages=messages
                )
                tool_name = selected_tool.get("name")
            else:
                # Single tool, just extract parameters
                tool = tools[0]
                tool_name = tool.get("name")
                logger.info(f"[{self.name}] Single tool: {tool_name}")
                params = await parameter_extractor.extract(
                    agent_name=self.name,
                    agent_prompt=self.prompt,
                    tool=tool,
                    messages=messages
                )

            logger.info(f"[{self.name}] Tool: {tool_name}, Params: {json.dumps(params, ensure_ascii=False)}")

            # Call the selected tool
            result = await self.call_tool(
                tool_name=tool_name,
                arguments=params,
                timeout=10
            )

            logger.info(f"[{self.name}] Tool execution completed successfully")
            return result

        except Exception as e:
            logger.error(f"[{self.name}] Execution failed: {e}")
            import traceback
            logger.error(f"[{self.name}] Traceback: {traceback.format_exc()}")
            return {
                "status": "failed",
                "error": str(e)
            }
