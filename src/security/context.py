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


class UnknownSecurityUserError(ValueError):
    """Raised when a requested S-ABAC user profile does not exist."""


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


def _extract_batch_size(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("employee_id_list", "recipient_list", "records", "items"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return len(candidate)
        for nested in value.values():
            size = _extract_batch_size(nested)
            if size:
                return size
    return 0


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, (tuple, set)):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        return [value]
    return [str(value)]


class SecurityContextBuilder:
    @staticmethod
    def subject_for_agent(agent: Any) -> Subject:
        name = getattr(agent, "agent_name", None) or getattr(agent, "name", None) or str(agent)
        attrs = _merge_dicts(
            DEFAULT_SUBJECT_ATTRIBUTES,
            AGENT_SECURITY_ATTRIBUTES.get(name),
            getattr(agent, "security_attributes", None),
        )
        attrs["job_role"] = attrs.get("job_role", DEFAULT_SUBJECT_ATTRIBUTES["job_role"])
        attrs["grants"] = _normalize_list(attrs.get("grants"))
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

        mapped = dict(RESOURCE_SECURITY_ATTRIBUTES.get(tool_name, {}))
        owner_agent = mapped.get("owner_agent")
        inherited = dict(RESOURCE_SECURITY_ATTRIBUTES.get(owner_agent, {})) if owner_agent else {}
        category = SecurityContextBuilder._category_for_tool(tool_name)
        attrs = _merge_dicts(
            DEFAULT_OBJECT_ATTRIBUTES,
            inherited,
            {
                "type": "tool",
                "category": category,
                "capability_domain": inherited.get("capability_domain", category),
            },
            metadata,
            mapped,
            runtime_attrs,
        )
        return Object(object_type="tool", id=tool_name, attributes=attrs)

    @staticmethod
    def object_for_resource_spec(spec: Any) -> Object:
        metadata = dict(getattr(spec, "metadata", {}) or {})
        name = getattr(spec, "name", "")
        mapped = dict(RESOURCE_SECURITY_ATTRIBUTES.get(name, {}))
        owner_agent = mapped.get("owner_agent")
        inherited = dict(RESOURCE_SECURITY_ATTRIBUTES.get(owner_agent, {})) if owner_agent else {}
        attrs = _merge_dicts(
            DEFAULT_OBJECT_ATTRIBUTES,
            inherited,
            {
                "type": getattr(spec, "type", "tool"),
                "protocol": getattr(spec, "protocol", "remote") or "remote",
                "server_id": getattr(spec, "server_id", ""),
                "category": SecurityContextBuilder._category_for_tool(name),
            },
            metadata,
            mapped,
        )
        return Object(object_type=getattr(spec, "type", "tool"), id=name, attributes=attrs)

    @staticmethod
    def scenario_from_context(context: Any = None, *, state: Optional[Dict[str, Any]] = None) -> Scenario:
        metadata = dict(getattr(context, "metadata", {}) or {})
        if state:
            metadata.update(state)

        task_profile = metadata.get("task_profile")
        if not isinstance(task_profile, dict):
            task_profile = {}

        workflow_mode = getattr(context, "workflow_mode", None) or metadata.get("workflow_mode", "execution")
        user_query = metadata.get("USER_QUERY", metadata.get("user_query", "workflow_execution"))
        task_type = str(
            task_profile.get("task_type")
            or metadata.get("task_type")
            or SecurityContextBuilder._infer_task_type(user_query, metadata)
        ).upper()
        scenario_tags = _normalize_list(
            task_profile.get("scenario_tags")
            or metadata.get("scenario_tags")
            or SecurityContextBuilder._infer_scenario_tags(user_query, metadata)
        )
        expected_capabilities = _normalize_list(
            task_profile.get("expected_capabilities")
            or metadata.get("expected_capabilities")
            or SecurityContextBuilder._infer_expected_capabilities(task_type, scenario_tags, metadata)
        )
        business_goal = str(
            task_profile.get("business_goal")
            or metadata.get("business_goal")
            or user_query
        )
        data_scope = str(
            task_profile.get("data_scope")
            or metadata.get("data_scope")
            or SecurityContextBuilder._infer_data_scope(user_query, metadata)
        )
        operation_mode = str(
            task_profile.get("operation_mode")
            or metadata.get("operation_mode")
            or SecurityContextBuilder._infer_operation_mode(user_query, metadata)
        )
        risk_profile = str(
            task_profile.get("risk_profile")
            or metadata.get("risk_profile")
            or "LOW"
        )

        return Scenario(
            task_scenario={
                "stage": str(workflow_mode or "execution").upper(),
                "goal": user_query,
                "business_goal": business_goal,
                "task_type": task_type,
                "data_scope": data_scope,
                "operation_mode": operation_mode,
                "scenario_tags": scenario_tags,
                "expected_capabilities": expected_capabilities,
                "risk_profile": risk_profile,
                "scenario_fit_result": metadata.get("scenario_fit_result", {}),
            },
            environment={
                "time": metadata.get("time") or ("working_hours" if 9 <= datetime.now().hour < 18 else "off_hours"),
                "network_zone": metadata.get("network_zone", "internal"),
                "authentication_strength": metadata.get("authentication_strength", "MFA"),
            },
            business_context={
                "workflow_id": getattr(context, "workflow_id", None) or metadata.get("workflow_id"),
                "task_id": metadata.get("task_id"),
                "current_step": metadata.get("current_step"),
                "delegation_chain": metadata.get("delegation_chain", []),
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
                "operation_mode": "delegate",
            },
        )

    @staticmethod
    def action_for_tool_call(tool_name: str, arguments: Optional[Dict[str, Any]]) -> Action:
        arguments = arguments or {}
        irreversible = bool(arguments.get("irreversible"))
        mapped = RESOURCE_SECURITY_ATTRIBUTES.get(tool_name, {})
        irreversible = irreversible or bool(mapped.get("irreversible", False))
        action_type = SecurityContextBuilder._infer_action_type(tool_name, arguments)
        return Action(
            verb="execute",
            attributes={
                "action_type": action_type,
                "tool_id": tool_name,
                "parameters": arguments,
                "amount": _extract_amount(arguments),
                "batch_size": _extract_batch_size(arguments),
                "irreversible": irreversible,
                "target_scope": arguments.get("target_scope", ""),
                "operation_mode": arguments.get("operation_mode", action_type),
            },
        )

    @staticmethod
    def subject_for_user(user_id: str) -> Subject:
        profile = get_demo_user(user_id)
        if profile:
            attrs = {
                "role": profile.get("role", DEFAULT_SUBJECT_ATTRIBUTES.get("role", "UniversalAssistant")),
                "department": profile.get("department", DEFAULT_SUBJECT_ATTRIBUTES.get("department", "General")),
                "job_role": profile.get("job_role", DEFAULT_SUBJECT_ATTRIBUTES.get("job_role", "general_staff")),
                "clearance_level": profile.get("clearance_level", DEFAULT_SUBJECT_ATTRIBUTES.get("clearance_level", 2)),
                "trust_level": profile.get("trust_level", DEFAULT_SUBJECT_ATTRIBUTES.get("trust_level", "MEDIUM")),
                "grants": _normalize_list(profile.get("grants")),
                "display_name": profile.get("display_name", user_id),
            }
            return Subject(subject_type="user", id=user_id, attributes=attrs)
        raise UnknownSecurityUserError(f"Unknown S-ABAC demo user: {user_id}")

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

    @staticmethod
    def _infer_task_type(user_query: str, metadata: Dict[str, Any]) -> str:
        explicit = metadata.get("task_type")
        if explicit:
            return str(explicit).upper()
        lowered = str(user_query).lower()
        if any(token in lowered for token in ("salary", "employee", "hr", "leave", "travel", "personnel", "income proof")):
            return "HR"
        if any(token in lowered for token in ("email", "notify", "notification", "message", "mail")):
            return "COMMUNICATION"
        if any(token in lowered for token in ("risk", "credit", "compliance")):
            return "RISK"
        if any(token in lowered for token in ("document", "report", "proof", "docx")):
            return "DOCUMENT"
        if any(token in lowered for token in ("research", "search", "crawl", "market")):
            return "RESEARCH"
        return "GENERAL"

    @staticmethod
    def _infer_scenario_tags(user_query: str, metadata: Dict[str, Any]) -> list[str]:
        explicit = _normalize_list(metadata.get("scenario_tags"))
        if explicit:
            return explicit
        lowered = str(user_query).lower()
        tags: list[str] = []
        if "salary" in lowered:
            tags.append("salary_query")
        if "employee" in lowered or "person" in lowered:
            tags.append("employee_info")
        if "proof" in lowered or "certificate" in lowered:
            tags.append("employee_proof")
        if "email" in lowered or "mail" in lowered:
            tags.append("notification_send")
        if "batch" in lowered or "mass" in lowered:
            tags.append("mass_notification")
        if "risk" in lowered or "credit" in lowered:
            tags.append("risk_analysis")
        if "research" in lowered or "search" in lowered or "market" in lowered:
            tags.append("market_research")
        return tags or ["general"]

    @staticmethod
    def _infer_expected_capabilities(task_type: str, scenario_tags: list[str], metadata: Dict[str, Any]) -> list[str]:
        explicit = _normalize_list(metadata.get("expected_capabilities"))
        if explicit:
            return explicit
        mapping = {
            "HR": ["HR"],
            "COMMUNICATION": ["Communication"],
            "RISK": ["Risk"],
            "DOCUMENT": ["Document"],
            "RESEARCH": ["Research"],
            "GENERAL": ["General"],
        }
        capabilities = mapping.get(task_type, ["General"])
        if "employee_proof" in scenario_tags and "Document" not in capabilities:
            capabilities.append("Document")
        return capabilities

    @staticmethod
    def _infer_data_scope(user_query: str, metadata: Dict[str, Any]) -> str:
        explicit = metadata.get("data_scope")
        if explicit:
            return str(explicit)
        lowered = str(user_query).lower()
        if any(token in lowered for token in ("all employees", "all staff", "company-wide", "全员", "全公司")):
            return "company"
        if any(token in lowered for token in ("department", "team", "本部门")):
            return "department"
        if any(token in lowered for token in ("my", "myself", "本人")):
            return "self"
        return "targeted"

    @staticmethod
    def _infer_operation_mode(user_query: str, metadata: Dict[str, Any]) -> str:
        explicit = metadata.get("operation_mode")
        if explicit:
            return str(explicit)
        lowered = str(user_query).lower()
        if any(token in lowered for token in ("send", "email", "mail", "通知")):
            return "send"
        if any(token in lowered for token in ("create", "generate", "report", "document", "proof")):
            return "generate"
        if any(token in lowered for token in ("save", "submit", "write", "update")):
            return "write"
        return "read"

    @staticmethod
    def _infer_action_type(tool_name: str, arguments: Dict[str, Any]) -> str:
        if arguments.get("action_type"):
            return str(arguments["action_type"])
        lowered = tool_name.lower()
        if "email" in lowered:
            return "send"
        if any(token in lowered for token in ("save_", "write", "update")):
            return "write"
        if any(token in lowered for token in ("doc", "report")):
            return "generate"
        return "call"
