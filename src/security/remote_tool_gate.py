"""Trusted intent-to-tool authorization for tools hidden behind remote agents.

Remote agents are execution adapters: dispatching an agent is not equivalent to
authorizing every tool that the remote implementation can call.  This module
maps the platform-owned TaskGraph intent to the concrete governed resource so
the scheduler can authorize it before the remote request leaves the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class RemoteToolAuthorization:
    tool_name: str
    arguments: dict[str, Any]


# The mapping is intentionally keyed by both agent and intent.  Authorizing all
# selected tools on an agent would make an ordinary employee-profile lookup ask
# for salary approval merely because the HR agent also owns the salary tool.
_INTENT_TOOL_MAP: dict[tuple[str, str], tuple[str, ...]] = {
    ("RemoteWeatherAgent", "weather_query"): ("remote_weather_tool",),
    ("RemoteHRAssistantAgent", "employee_information_query"): ("remote_person_info_tool",),
    ("RemoteHRAssistantAgent", "salary_query"): ("remote_salary_info_tool",),
    ("RemoteUnicornSelectorAgent", "information_research"): ("remote_unicorn_db_tool",),
    ("RemoteBusinessRiskAgent", "risk_analysis"): ("remote_credit_risk_db_tool",),
    ("RemoteReportAgent", "report_generation"): ("remote_report_builder_tool",),
    ("RemoteDocumentGeneratorAgent", "document_generation"): ("remote_docx_generator_tool",),
    ("RemoteEmailDispatchAgent", "message_or_email_send"): ("remote_email_tool",),
    # This agent first resolves a contact and then sends.  Both resources must
    # be in the manifest or the remote request fails closed at the second hop.
    ("RemoteCommunicationAgent", "message_or_email_send"): (
        "remote_contact_query_tool",
        "remote_email_tool",
    ),
    ("RemoteKnowledgeAgent", "knowledge_lookup"): ("knowledge_search_tool",),
    ("RemoteOfficeAssistantAgent", "leave_record_query"): ("query_leave_record",),
    ("RemoteOfficeAssistantAgent", "leave_request"): ("save_leave_record",),
    ("RemoteMeetingManagerAgent", "meeting_arrangement"): (
        "remote_meeting_scheduling_tool",
    ),
    ("RemoteScheduleAgent", "schedule_management"): ("remote_schedule_tool",),
    ("RemoteTodoAgent", "schedule_management"): ("remote_todo_query_tool",),
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _stable_arguments(task_profile: Mapping[str, Any], intent: str) -> dict[str, Any]:
    """Return server-recognized entities that are safe to bind to approval."""

    entities = _as_mapping(task_profile.get("entities"))
    arguments: dict[str, Any] = {}
    for key in (
        "employee_name",
        "employee_id",
        "recipient",
        "recipients",
        "document_type",
        "date",
        "start_date",
        "end_date",
        "location",
    ):
        value = entities.get(key)
        if value not in (None, "", [], {}):
            arguments[key] = value
    # Intent is part of the action context even when entity extraction produced
    # no arguments, so two different operations cannot share one approval.
    arguments["intent"] = intent
    return arguments


def required_remote_tool_authorizations(
    *,
    agent_name: str,
    intents: Iterable[str],
    task_profile: Any,
    operation_mode: str = "read",
) -> list[RemoteToolAuthorization]:
    """Resolve governed remote tools from trusted scheduler-owned fields."""

    profile = _as_mapping(task_profile)
    resolved: list[RemoteToolAuthorization] = []
    seen: set[str] = set()
    for raw_intent in intents:
        intent = str(raw_intent or "").strip()
        tool_names = _INTENT_TOOL_MAP.get((str(agent_name), intent), ())
        mode = str(operation_mode or "read").lower()
        if agent_name == "RemoteOfficeAssistantAgent" and intent == "travel_service":
            tool_names = (
                ("query_travel_record",)
                if mode in {"read", "query"}
                else ("save_travel_record",)
            )
        elif agent_name == "RemoteHRCalendarAgent" and intent in {
            "schedule_management",
            "meeting_arrangement",
        }:
            tool_names = (
                ("get_calendar_events_tool",)
                if mode in {"read", "query"}
                else ("create_calendar_event_tool",)
            )

        for tool_name in tool_names:
            if tool_name in seen:
                continue
            seen.add(tool_name)
            resolved.append(
                RemoteToolAuthorization(
                    tool_name=tool_name,
                    arguments=_stable_arguments(profile, intent),
                )
            )
    return resolved
