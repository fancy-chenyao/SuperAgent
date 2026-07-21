#!/usr/bin/env python
"""Base class for remote agents with multi-tool support."""

from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


class BaseRemoteAgent(ABC):
    """Base class for all remote agents."""

    def __init__(self, name: str, prompt: str):
        self.name = name
        self.prompt = prompt

    @abstractmethod
    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """
        Execute the agent with given tools and messages.

        Args:
            tools: List of tool definitions to call
            messages: Conversation history
            context: Additional context
            parameter_extractor: LLM parameter extractor instance

        Returns:
            Execution result dictionary
        """
        pass

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_service_url: str = "http://127.0.0.1:8011/tool",
        timeout: int = 10
    ) -> Any:
        """
        Call a single tool.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments
            tool_service_url: URL of the tool service
            timeout: Request timeout in seconds

        Returns:
            Tool execution result
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=timeout)) as client:
                resp = await client.post(
                    tool_service_url,
                    json={"tool": tool_name, "arguments": arguments},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                payload = resp.json()
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(
                        f"Tool {tool_name} returned an invalid result payload"
                    )
                if str(result.get("status") or "").lower() in {"error", "failed"}:
                    detail = result.get("error") or result.get("message") or "unknown tool error"
                    raise RuntimeError(f"Tool {tool_name} failed: {detail}")
                logger.info(f"Tool {tool_name} executed successfully")
                return result
        except httpx.TimeoutException as exc:
            message = f"Tool {tool_name} timed out after {timeout}s"
            logger.error(message)
            raise TimeoutError(message) from exc
        except Exception as e:
            detail = str(e) or type(e).__name__
            logger.error(f"Tool {tool_name} execution failed: {detail}")
            raise
