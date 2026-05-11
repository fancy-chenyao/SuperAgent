from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from config.s_abac_config import (
    AGENT_SECURITY_ATTRIBUTES,
    DEFAULT_OBJECT_ATTRIBUTES,
    DEFAULT_SUBJECT_ATTRIBUTES,
    RESOURCE_SECURITY_ATTRIBUTES,
    SYSTEM_SUBJECT_ATTRIBUTES,
)
from config.s_abac_demo_users import get_demo_user
from src.security.policy import Action, Object, Scenario, Subject


def _merge_dicts(*items: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _extract_amount(value: Any) -> float:
    if isinstance(value, dict):
        for key in ("amount", "reimbursement_amount", "total_amount", "request_amount", "estimated_amount"):
            if isinstance(value.get(key), (int, float)):
                return float(value[key])
        for nested in value.values():
            amount = _extract_amount(nested)
            if amount:
                return amount
    if isinstance(value, list):
        for item in value:
            amount = _extract_amount(item)
            if amount:
                return amount
    return 0.0


class SecurityContextBuilder:
    @staticmethod
    def subject_for_agent(agent: Any) -> Subject:
        name = getattr(agent, "agent_name", None) or getattr(agent, "name", None) or str(agent)
        attrs = _merge_dicts(
            DEFAULT_SUBJECT_ATTRIBUTES,
            AGENT_SECURITY_ATTRIBUTES.get(name),
            getattr(agent, "security_attributes", None),
        )
        return Subject(subject_type="agent", id=name, attributes=attrs)

    @staticmethod
    def system_subject() -> Subject:
        return Subject(
            subject_type="system",
            id="superagent_orchestrator",
            attributes=dict(SYSTEM_SUBJECT_ATTRIBUTES),
        )

    @staticmethod
    def object_for_agent(agent: Any) -> Object:
        name = getattr(agent, "agent_name", None) or getattr(agent, "name", None) or str(agent)
        attrs = _merge_dicts(
            DEFAULT_OBJECT_ATTRIBUTES,
            {"type": "agent", "protocol": getattr(agent, "source", "local")},
            RESOURCE_SECURITY_ATTRIBUTES.get(name),
            getattr(agent, "security_attributes", None),
        )
        return Object(object_type="agent", id=name, attributes=attrs)

    @staticmethod
    def object_for_tool(tool_name: str, tool: Any = None, metadata: Optional[Dict[str, Any]] = None) -> Object:
        runtime_attrs = {}
        if tool is not None:
            runtime_attrs = {
                "description": getattr(tool, "description", ""),
                "args_schema": str(getattr(tool, "args_schema", "") or ""),
            }
        category = SecurityContextBuilder._category_for_tool(tool_name)
        attrs = _merge_dicts(
            DEFAULT_OBJECT_ATTRIBUTES,
            {"type": "tool", "category": category},
            metadata,
            RESOURCE_SECURITY_ATTRIBUTES.get(tool_name),
            runtime_attrs,
        )
        return Object(object_type="tool", id=tool_name, attributes=attrs)

    @staticmethod
    def object_for_resource_spec(spec: Any) -> Object:
        metadata = dict(getattr(spec, "metadata", {}) or {})
        name = getattr(spec, "name", "")
        attrs = _merge_dicts(
            DEFAULT_OBJECT_ATTRIBUTES,
            {
                "type": getattr(spec, "type", "tool"),
                "protocol": getattr(spec, "protocol", "remote") or "remote",
                "server_id": getattr(spec, "server_id", ""),
                "category": SecurityContextBuilder._category_for_tool(name),
            },
            metadata,
            RESOURCE_SECURITY_ATTRIBUTES.get(name),
        )
        return Object(object_type=getattr(spec, "type", "tool"), id=name, attributes=attrs)

    @staticmethod
    def scenario_from_context(context: Any = None, *, state: Optional[Dict[str, Any]] = None) -> Scenario:
        metadata = dict(getattr(context, "metadata", {}) or {})
        if state:
            metadata.update(state)
        workflow_mode = getattr(context, "workflow_mode", None) or metadata.get("workflow_mode", "execution")
        return Scenario(
            task_scenario={
                "stage": str(workflow_mode or "execution").upper(),
                "goal": metadata.get("USER_QUERY", metadata.get("user_query", "workflow_execution")),
                "risk_profile": metadata.get("risk_profile", "LOW"),
            },
            environment={
                "time": "working_hours" if 9 <= datetime.now().hour < 18 else "off_hours",
                "network_zone": metadata.get("network_zone", "internal"),
                "authentication_strength": metadata.get("authentication_strength", "MFA"),
            },
            business_context={
                "workflow_id": getattr(context, "workflow_id", None) or metadata.get("workflow_id"),
                "task_id": metadata.get("task_id"),
                "current_step": metadata.get("current_step"),
            },
        )

    @staticmethod
    def action_for_agent_dispatch(target_agent_name: str) -> Action:
        return Action(
            verb="orchestrate",
            attributes={
                "action_type": "delegate",
                "target_agent": target_agent_name,
                "irreversible": False,
            },
        )

    @staticmethod
    def action_for_tool_call(tool_name: str, arguments: Optional[Dict[str, Any]]) -> Action:
        arguments = arguments or {}
        irreversible = bool(arguments.get("irreversible"))
        mapped = RESOURCE_SECURITY_ATTRIBUTES.get(tool_name, {})
        irreversible = irreversible or bool(mapped.get("irreversible", False))
        return Action(
            verb="execute",
            attributes={
                "action_type": "call",
                "tool_id": tool_name,
                "parameters": arguments,
                "amount": _extract_amount(arguments),
                "irreversible": irreversible,
            },
        )

    @staticmethod
    def subject_for_user(user_id: str) -> Subject:
        profile = get_demo_user(user_id)
        if profile:
            attrs = {
                "role": profile.get("role", DEFAULT_SUBJECT_ATTRIBUTES.get("role", "UniversalAssistant")),
                "department": profile.get("department", DEFAULT_SUBJECT_ATTRIBUTES.get("department", "General")),
                "clearance_level": profile.get("clearance_level", DEFAULT_SUBJECT_ATTRIBUTES.get("clearance_level", 2)),
                "trust_level": profile.get("trust_level", DEFAULT_SUBJECT_ATTRIBUTES.get("trust_level", "MEDIUM")),
                "display_name": profile.get("display_name", user_id),
            }
            return Subject(subject_type="user", id=user_id, attributes=attrs)
        return Subject(
            subject_type="user",
            id=user_id,
            attributes=dict(DEFAULT_SUBJECT_ATTRIBUTES),
        )

    @staticmethod
    def _category_for_tool(tool_name: str) -> str:
        lowered = tool_name.lower()
        if any(token in lowered for token in ("salary", "person", "hr", "leave", "travel")):
            return "HR"
        if any(token in lowered for token in ("email", "contact", "communication")):
            return "Communication"
        if any(token in lowered for token in ("risk", "credit", "compliance")):
            return "Risk"
        if any(token in lowered for token in ("doc", "file", "write")):
            return "Document"
        if any(token in lowered for token in ("search", "crawl", "weather", "knowledge")):
            return "Research"
        return "General"
