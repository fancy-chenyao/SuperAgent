"""Trusted output contracts for built-in and mock remote Agents.

Planner output is untrusted, so side-effect result schemas are selected from
this server-owned registry rather than invented by the planner. The same
contracts are exposed in Agent Cards, attached to TaskGraph steps and
registered with the runtime SchemaRegistry.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional


DOCUMENT_GENERATION_RESULT_V1 = "document_generation_result@v1"
EMAIL_DISPATCH_RESULT_V1 = "email_dispatch_result@v1"
REPORT_GENERATION_RESULT_V1 = "report_generation_result@v1"
COMMUNICATION_RESULT_V1 = "communication_result@v1"
STRUCTURED_AGENT_RESULT_V1 = "structured_agent_result@v1"
EMPLOYEE_QUERY_RESULT_V1 = "employee_query_result@v1"
MARKDOWN_TEXT_RESULT_V1 = "markdown_text_result@v1"


OUTPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    DOCUMENT_GENERATION_RESULT_V1: {
        "required": ["status"],
        "properties": {
            "status": {"type": "string"},
            "file_path": {"type": "string"},
            "file_name": {"type": "string"},
            "template_used": {"type": "string"},
            "message": {"type": "string"},
        },
        "additional_properties": True,
    },
    EMAIL_DISPATCH_RESULT_V1: {
        "required": ["status"],
        "properties": {
            "status": {"type": "string"},
            "sent": {"type": "object"},
        },
        "additional_properties": True,
    },
    REPORT_GENERATION_RESULT_V1: {
        "required": ["status", "markdown"],
        "properties": {
            "status": {"type": "string"},
            "markdown": {"type": "string"},
        },
        "additional_properties": True,
    },
    COMMUNICATION_RESULT_V1: {
        "required": ["status"],
        "properties": {
            "status": {"type": "string"},
            "message": {"type": "string"},
        },
        "additional_properties": True,
    },
    # The mock read/business Agents all return a structured status envelope,
    # with domain-specific fields (records, events, detail, answer, etc.).  The
    # shared envelope is intentionally small but trusted: it prevents an
    # untyped write-classified result from bypassing Artifact validation while
    # preserving each Agent's demonstrative payload.
    STRUCTURED_AGENT_RESULT_V1: {
        "required": ["status"],
        "properties": {
            "status": {"type": "string"},
        },
        "additional_properties": True,
    },
    # RemoteHRAssistantAgent intentionally exposes the matched employee rows
    # directly as an array so document/report Agents can consume the same
    # structure shown by the demo UI.
    EMPLOYEE_QUERY_RESULT_V1: {
        "type": "array",
        "items": {"type": "object"},
    },
    # The built-in researcher/reporter Agents return Markdown directly rather
    # than a status envelope.  Treat that real payload shape as a first-class
    # trusted contract so report chains remain typed without rewriting or
    # fabricating model output.
    MARKDOWN_TEXT_RESULT_V1: {
        "type": "string",
    },
}


AGENT_OUTPUT_SCHEMA_REFS: Dict[str, str] = {
    "RemoteDocumentGeneratorAgent": DOCUMENT_GENERATION_RESULT_V1,
    "RemoteEmailDispatchAgent": EMAIL_DISPATCH_RESULT_V1,
    "RemoteReportAgent": REPORT_GENERATION_RESULT_V1,
    "RemoteCommunicationAgent": COMMUNICATION_RESULT_V1,
    "RemoteWeatherAgent": STRUCTURED_AGENT_RESULT_V1,
    "RemoteHRAssistantAgent": EMPLOYEE_QUERY_RESULT_V1,
    "RemoteUnicornSelectorAgent": STRUCTURED_AGENT_RESULT_V1,
    "RemoteBusinessRiskAgent": STRUCTURED_AGENT_RESULT_V1,
    "RemoteScheduleAgent": STRUCTURED_AGENT_RESULT_V1,
    "RemoteTodoAgent": STRUCTURED_AGENT_RESULT_V1,
    "RemoteHRCalendarAgent": STRUCTURED_AGENT_RESULT_V1,
    "RemoteKnowledgeAgent": STRUCTURED_AGENT_RESULT_V1,
    "RemoteOfficeAssistantAgent": STRUCTURED_AGENT_RESULT_V1,
    "RemoteMeetingManagerAgent": STRUCTURED_AGENT_RESULT_V1,
    "researcher": MARKDOWN_TEXT_RESULT_V1,
    "reporter": MARKDOWN_TEXT_RESULT_V1,
}


AGENT_OUTPUT_LOGICAL_NAMES: Dict[str, list[str]] = {
    "RemoteWeatherAgent": ["weather.forecast"],
    "RemoteHRAssistantAgent": [
        "employee.info",
        "employee.id",
        "employee.name",
        "employee.salary",
    ],
    "RemoteUnicornSelectorAgent": ["company.records"],
    "RemoteBusinessRiskAgent": ["risk.records"],
    "RemoteReportAgent": ["report.markdown"],
    "RemoteEmailDispatchAgent": ["email.dispatch"],
    "RemoteScheduleAgent": ["schedule.result"],
    "RemoteTodoAgent": ["todo.records"],
    "RemoteHRCalendarAgent": ["calendar.result"],
    "RemoteKnowledgeAgent": ["knowledge.answer"],
    "RemoteDocumentGeneratorAgent": ["document.file"],
    "RemoteOfficeAssistantAgent": [
        "office.result",
        "employee.leave_records",
        "employee.travel_records",
    ],
    "RemoteMeetingManagerAgent": ["meeting.result"],
    "RemoteCommunicationAgent": ["communication.result"],
    "researcher": ["research.markdown"],
    "reporter": ["report.markdown"],
}


def get_agent_output_schema_ref(agent_name: Any) -> Optional[str]:
    """Return the trusted schema reference for a known Agent."""

    return AGENT_OUTPUT_SCHEMA_REFS.get(str(agent_name or ""))


def get_agent_output_schema(agent_name: Any) -> Dict[str, Any]:
    """Return an Agent-Card-safe copy of the trusted output contract."""

    schema_ref = get_agent_output_schema_ref(agent_name)
    if not schema_ref:
        return {}
    return {
        "schema_ref": schema_ref,
        **deepcopy(OUTPUT_SCHEMAS[schema_ref]),
    }


def get_agent_output_logical_names(agent_name: Any) -> list[str]:
    """Return trusted logical output names for a built-in/mock Agent."""

    return list(AGENT_OUTPUT_LOGICAL_NAMES.get(str(agent_name or ""), []))
