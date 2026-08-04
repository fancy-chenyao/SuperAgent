#!/usr/bin/env python
"""Email Dispatch Agent - sends emails."""

from typing import Any, Dict, List
import json
import logging

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


def _execution_brief(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    for message in reversed(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or "EXECUTION_CONTEXT" not in content:
            continue
        try:
            payload = json.loads(content.split("EXECUTION_CONTEXT", 1)[1].strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _upstream_body(brief: Dict[str, Any]) -> str:
    resolved = brief.get("resolved_inputs") or {}
    if not isinstance(resolved, dict):
        return ""
    for value in resolved.values():
        if isinstance(value, dict):
            for key in ("markdown", "content", "message", "file_path"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return str(candidate)
        elif value not in (None, ""):
            return str(value)
    return ""


class RemoteEmailDispatchAgent(BaseRemoteAgent):
    """Email Dispatch Agent for sending emails."""

    def __init__(self):
        super().__init__(
            name="RemoteEmailDispatchAgent",
            prompt="You are an email dispatcher that sends emails to recipients."
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
        if not isinstance(arguments, dict):
            arguments = {}

        # The model still performs parameter extraction and any model error is
        # propagated.  This is contract normalization only: reuse recipient and
        # upstream content already validated by TaskProfile/ArtifactResolver
        # when the extracted optional fields are empty.
        brief = _execution_brief(messages)
        profile = brief.get("task_profile") or {}
        entities = profile.get("entities") or {}
        recipient = str(entities.get("recipient") or "").strip()
        if recipient and not str(arguments.get("to") or "").strip():
            arguments["to"] = recipient
        if not str(arguments.get("body") or "").strip():
            arguments["body"] = _upstream_body(brief)
        if not str(arguments.get("subject") or "").strip():
            arguments["subject"] = str(
                entities.get("document_type") or "任务结果通知"
            )

        logger.info(f"[{self.name}] Calling {tool_name}")
        result = await self.call_tool(
            tool_name=tool_name,
            arguments=arguments
        )

        return result
