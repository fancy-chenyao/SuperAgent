#!/usr/bin/env python
"""Report Agent - generates reports."""

from typing import Any, Dict, List
import json
import logging
import os

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.contracts.agent_result import AgentResultError

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


def _resolved_report_sources(messages: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Read the Scheduler-assembled report input without asking an LLM to copy it."""

    for message in reversed(messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.startswith("EXECUTION_CONTEXT"):
            continue
        _, _, raw = content.partition("\n")
        try:
            brief = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        resolved = brief.get("resolved_inputs") if isinstance(brief, dict) else None
        report_sources = (
            resolved.get("report.sources") if isinstance(resolved, dict) else None
        )
        if isinstance(report_sources, dict):
            return report_sources
    return None


def _resolved_legacy_report_data(messages: List[Dict[str, Any]]) -> List[Any]:
    """Collect legacy resolved inputs when the report.sources contract is absent."""

    for message in reversed(messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.startswith("EXECUTION_CONTEXT"):
            continue
        _, _, raw = content.partition("\n")
        try:
            brief = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        resolved = brief.get("resolved_inputs") if isinstance(brief, dict) else None
        if not isinstance(resolved, dict):
            continue
        data: List[Any] = []
        for value in resolved.values():
            if isinstance(value, dict) and isinstance(value.get("records"), list):
                data.extend(value["records"])
            else:
                data.append(value)
        return data
    return []


class RemoteReportAgent(BaseRemoteAgent):
    """Report Agent for generating reports."""

    def __init__(self):
        super().__init__(
            name="RemoteReportAgent",
            prompt="You are a report generator that creates comprehensive reports.",
            contract=AgentContract(
                contract_version="1.0",
                requires=[
                    DataContractRef(
                        name="report.sources",
                        schema_ref="report.sources@v1",
                    )
                ],
                produces=[
                    DataContractRef(
                        name="report.markdown",
                        schema_ref="report.markdown@v1",
                    )
                ],
            ),
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any,
    ) -> Dict[str, Any]:
        """Execute report generation - single tool agent."""
        if not tools or len(tools) == 0:
            return self.result_envelope(
                error=AgentResultError(
                    code="NO_TOOL_PROVIDED",
                    message="No report tool was provided",
                    retryable=False,
                )
            )

        tool = tools[0]
        tool_name = tool.get("name", "unknown")

        try:
            # Artifact fan-in is a governed data-plane input. The extractor may
            # not remove, replace or rewrite the actual upstream payloads
            # assembled by the Scheduler.
            report_sources = _resolved_report_sources(messages)
            if report_sources is not None:
                sources = report_sources.get("sources")
                if not isinstance(sources, list) or not sources:
                    raise ValueError(
                        "report.sources must contain at least one upstream source"
                    )
                arguments = {
                    "data": sources,
                    "title": str(report_sources.get("title") or "分析报告"),
                    "instruction": str(
                        report_sources.get("instruction") or "使用全部上游来源生成报告"
                    ),
                }
            else:
                # Legacy/direct invocation has no Scheduler data-plane input.
                logger.info(f"[{self.name}] Extracting parameters for {tool_name}")
                arguments = await parameter_extractor.extract(
                    agent_name=self.name,
                    agent_prompt=self.prompt,
                    tool=tool,
                    messages=messages,
                )
                if not isinstance(arguments, dict):
                    arguments = {}
                if not arguments.get("data") and not arguments.get("sections"):
                    legacy_data = _resolved_legacy_report_data(messages)
                    if legacy_data:
                        arguments["data"] = legacy_data

            llm_timeout = int(os.getenv("REMOTE_REPORT_LLM_TIMEOUT", "120"))
            tool_timeout = int(os.getenv("REMOTE_REPORT_TOOL_TIMEOUT", "150"))
            if tool_timeout <= llm_timeout:
                raise ValueError(
                    "REMOTE_REPORT_TOOL_TIMEOUT must be greater than REMOTE_REPORT_LLM_TIMEOUT"
                )
            arguments["llm_timeout_sec"] = llm_timeout

            logger.info(f"[{self.name}] Calling {tool_name}")
            result = await self.call_tool(
                tool_name=tool_name,
                arguments=arguments,
                timeout=tool_timeout,
            )
            sources = arguments.get("sources")
            if not isinstance(sources, list):
                sources = arguments.get("data")
            source_count = len(sources) if isinstance(sources, list) else 0
            payload = {
                "title": str(arguments.get("title") or "分析报告"),
                "markdown": str(result.get("markdown") or ""),
                "source_count": source_count,
            }
            return self.result_envelope(outputs={"report.markdown": payload})
        except Exception as exc:
            return self.result_envelope(
                error=self.execution_error(exc, tool_name=tool_name)
            )
