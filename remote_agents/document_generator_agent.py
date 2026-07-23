#!/usr/bin/env python
"""Document Generator Agent - generates Word documents."""

from typing import Any, Dict, List
import json
import logging

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)

_TEMPLATE_BY_DOCUMENT_TYPE = {
    "income_proof": "income_proof",
    "employment_certificate": "employment_certificate",
    "leave_application": "leave_application",
    "explanation_document": "explanation_document",
}


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


def _upstream_report(messages: List[Dict[str, Any]]) -> str:
    """读取报告 Agent 的真实输出，供说明文档模板消费。"""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("tool") not in {"RemoteReportAgent", "reporter"}:
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if content not in (None, ""):
            return json.dumps(content, ensure_ascii=False, default=str)
    return ""


def _document_title(brief: Dict[str, Any]) -> str:
    for step in brief.get("assigned_steps") or []:
        if isinstance(step, dict):
            title = str(step.get("title") or step.get("goal") or "").strip()
            if title:
                return title
    return "说明文档"


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
        if not isinstance(arguments, dict):
            raise ValueError("Document parameter extractor returned a non-object result")

        # TaskProfile 是识别和规划阶段形成的执行契约。模板选择以其中已经校验过的
        # document_type 为准，防止参数模型生成注册表之外或仓库中不存在的模板名。
        brief = _execution_brief(messages)
        profile = brief.get("task_profile") or {}
        entities = profile.get("entities") or {}
        document_type = str(entities.get("document_type") or "").strip()
        expected_template = _TEMPLATE_BY_DOCUMENT_TYPE.get(document_type)
        if expected_template:
            arguments["template_name"] = expected_template
            data = arguments.get("data")
            if not isinstance(data, dict):
                data = {}
                arguments["data"] = data
            data["document_type"] = document_type
            if document_type == "explanation_document":
                data.setdefault("title", _document_title(brief))
                content = (
                    data.get("content")
                    or data.get("summary")
                    or data.get("report")
                    or _upstream_report(messages)
                )
                if not content:
                    raise ValueError("说明文档缺少上游摘要内容")
                data["content"] = content

        logger.info(f"[{self.name}] Calling {tool_name}")
        result = await self.call_tool(
            tool_name=tool_name,
            arguments=arguments,
            timeout=30  # Document generation may take some time
        )

        return result
