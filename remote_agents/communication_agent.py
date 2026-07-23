#!/usr/bin/env python
"""Communication Agent: resolve recipients before sending notifications."""

from __future__ import annotations

from typing import Any, Dict, List
import json
import logging

from .base_agent import BaseRemoteAgent

logger = logging.getLogger(__name__)


def _tool_by_name(tools: List[Dict[str, Any]], name: str) -> Dict[str, Any] | None:
    return next((tool for tool in tools if tool.get("name") == name), None)


def _execution_brief(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    for message in reversed(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or "EXECUTION_CONTEXT" not in content:
            continue
        payload = content.split("EXECUTION_CONTEXT", 1)[1].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _notification_recipients(messages: List[Dict[str, Any]]) -> List[str]:
    brief = _execution_brief(messages)
    profile = brief.get("task_profile") or {}
    entities = profile.get("entities") or {}
    recipient = str(entities.get("recipient") or "").strip()
    people = [str(item).strip() for item in entities.get("people") or [] if str(item).strip()]
    generic_recipients = {"参会人", "所有参会人", "全体参会人", "与会人员", "相关人员"}
    values = people if recipient in generic_recipients else [recipient] if recipient else people
    return list(dict.fromkeys(value for value in values if value))


def _is_notification_step(messages: List[Dict[str, Any]]) -> bool:
    brief = _execution_brief(messages)
    assigned_steps = brief.get("assigned_steps") or []
    return any(
        isinstance(step, dict)
        and (
            step.get("intent") == "message_or_email_send"
            or any(token in json.dumps(step, ensure_ascii=False) for token in ("通知", "发送", "邮件"))
        )
        for step in assigned_steps
    )


class RemoteCommunicationAgent(BaseRemoteAgent):
    """Resolve contacts and send a notification as one complete Agent task."""

    def __init__(self):
        super().__init__(
            name="RemoteCommunicationAgent",
            prompt=(
                "你是企业通信专员。发送通知前必须解析全部收件人的联系方式，"
                "再根据上游任务结果生成主题和正文并调用邮件工具。"
            ),
        )

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any,
    ) -> Dict[str, Any]:
        logger.info("Executing %s with %s tools", self.name, len(tools))
        if not tools:
            raise ValueError("No communication tools configured")

        contact_tool = _tool_by_name(tools, "remote_contact_query_tool")
        email_tool = _tool_by_name(tools, "remote_email_tool")
        if _is_notification_step(messages) and email_tool:
            recipients = _notification_recipients(messages)
            if not recipients:
                return {"status": "failed", "error": "通知任务没有可解析的收件人"}

            contacts_result: Dict[str, Any] = {"contacts": []}
            if contact_tool:
                contacts_result = await self.call_tool(
                    tool_name="remote_contact_query_tool",
                    arguments={"names": recipients},
                )
                unresolved = contacts_result.get("unresolved_names") or []
                contacts = contacts_result.get("contacts") or []
                if unresolved or not contacts:
                    return {
                        "status": "failed",
                        "error": "部分通知对象没有可用联系方式",
                        "unresolved_recipients": unresolved or recipients,
                        "contact_lookup": contacts_result,
                    }

            emails = list(
                dict.fromkeys(
                    str(contact.get("email") or "").strip()
                    for contact in contacts_result.get("contacts") or []
                    if str(contact.get("email") or "").strip()
                )
            )
            if not emails:
                return {
                    "status": "failed",
                    "error": "通知对象没有可用邮箱",
                    "contact_lookup": contacts_result,
                }

            email_arguments = await parameter_extractor.extract(
                agent_name=self.name,
                agent_prompt=self.prompt,
                tool=email_tool,
                messages=messages
                + [{"role": "assistant", "content": json.dumps(contacts_result, ensure_ascii=False)}],
            )
            email_arguments["to"] = ",".join(emails)
            if not str(email_arguments.get("body") or "").strip():
                return {"status": "failed", "error": "未能根据会议结果生成通知正文"}

            sent = await self.call_tool(
                tool_name="remote_email_tool",
                arguments=email_arguments,
            )
            return {
                "status": "success",
                "message": f"已向 {len(emails)} 个参会邮箱发送通知",
                "recipients": recipients,
                "resolved_contacts": contacts_result.get("contacts") or [],
                "sent": sent.get("sent") or sent,
            }

        if len(tools) == 1:
            selected_tool = tools[0]
            arguments = await parameter_extractor.extract(
                agent_name=self.name,
                agent_prompt=self.prompt,
                tool=selected_tool,
                messages=messages,
            )
        else:
            selected_tool, arguments = await parameter_extractor.select_tool_and_extract(
                agent_name=self.name,
                agent_prompt=self.prompt,
                tools=tools,
                messages=messages,
            )

        result = await self.call_tool(
            tool_name=str(selected_tool["name"]),
            arguments=arguments,
        )
        return {"status": "success", "message": "操作成功", "result": result}
