#!/usr/bin/env python
"""Base class for remote agents with multi-tool support."""

from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod
from contextvars import ContextVar, Token
import logging
import unicodedata

from src.contracts.agent_contract import AgentContract
from src.contracts.agent_result import (
    AgentResultEnvelope,
    AgentResultError,
    AgentResultMetadata,
    AgentResultStatus,
)

logger = logging.getLogger(__name__)

_authorized_remote_tools: ContextVar[tuple[tuple[str, Dict[str, Any]], ...]] = ContextVar(
    "authorized_remote_tools", default=()
)

_SECURITY_ARGUMENT_ALIASES: Dict[str, tuple[str, ...]] = {
    "employee_name": ("employee_name", "keyword"),
    "employee_id": ("employee_id", "employee_id_list"),
    "recipient": ("recipient", "recipients", "names", "to"),
    "recipients": ("recipients", "recipient", "names", "to"),
    "resolved_recipient_addresses": ("resolved_recipient_addresses", "to"),
    "document_type": ("document_type", "template_name"),
    "date": ("date",),
    "start_date": ("start_date",),
    "end_date": ("end_date",),
    "location": ("location", "destination"),
}

_TOOL_SECURITY_ARGUMENTS: Dict[str, frozenset[str]] = {
    "remote_person_info_tool": frozenset({"employee_name", "employee_id"}),
    "remote_salary_info_tool": frozenset({"employee_name", "employee_id"}),
    "remote_contact_query_tool": frozenset({"recipient", "recipients"}),
    "remote_email_tool": frozenset({"resolved_recipient_addresses"}),
    "remote_docx_generator_tool": frozenset({"document_type"}),
    "query_leave_record": frozenset({"employee_name", "employee_id", "start_date", "end_date"}),
    "save_leave_record": frozenset({"employee_name", "employee_id", "start_date", "end_date"}),
    "query_travel_record": frozenset({"employee_name", "employee_id", "start_date", "end_date", "location"}),
    "save_travel_record": frozenset({"employee_name", "employee_id", "start_date", "end_date", "location"}),
    "get_calendar_events_tool": frozenset({"date", "start_date", "end_date"}),
    "create_calendar_event_tool": frozenset({"date", "start_date", "end_date", "location"}),
    "remote_meeting_scheduling_tool": frozenset({"date", "start_date", "end_date", "location", "recipient", "recipients"}),
}


def _normalize_security_value(value: Any, *, plural: bool = False) -> Any:
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).strip().casefold()
        if plural:
            parts = [part.strip() for part in normalized.replace(";", ",").split(",")]
            return tuple(sorted(part for part in parts if part))
        return normalized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_normalize_security_value(item) for item in value]
        return tuple(sorted(items, key=repr))
    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(key), _normalize_security_value(item))
                for key, item in value.items()
            )
        )
    return value


def _find_argument(arguments: Dict[str, Any], aliases: tuple[str, ...]) -> tuple[bool, Any]:
    for key in aliases:
        if key in arguments:
            return True, arguments[key]
    for value in arguments.values():
        if isinstance(value, dict):
            found, nested = _find_argument(value, aliases)
            if found:
                return True, nested
    return False, None


def _arguments_match_authorization(
    tool_name: str,
    expected: Dict[str, Any],
    actual: Dict[str, Any],
) -> bool:
    keys = _TOOL_SECURITY_ARGUMENTS.get(tool_name, frozenset())
    for key in keys:
        aliases = _SECURITY_ARGUMENT_ALIASES[key]
        expected_found, expected_value = _find_argument(expected, aliases)
        actual_found, actual_value = _find_argument(actual, aliases)
        if expected_found != actual_found:
            return False
        if not expected_found:
            continue
        plural = key in {"recipient", "recipients", "resolved_recipient_addresses"}
        if _normalize_security_value(expected_value, plural=plural) != _normalize_security_value(
            actual_value, plural=plural
        ):
            return False
    return True


def bind_authorized_remote_tools(context: Dict[str, Any]) -> Token:
    """Bind a request-scoped platform authorization manifest."""

    raw = context.get("authorized_remote_tools")
    manifest: list[tuple[str, Dict[str, Any]]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool_name") or "").strip()
            arguments = item.get("arguments")
            if tool_name and isinstance(arguments, dict):
                manifest.append((tool_name, dict(arguments)))
    # Missing, malformed and explicitly empty manifests all bind to an empty
    # tuple. call_tool therefore fails closed instead of treating None as an
    # instruction to disable the gate.
    return _authorized_remote_tools.set(tuple(manifest))


def reset_authorized_remote_tools(token: Token) -> None:
    _authorized_remote_tools.reset(token)


class RemoteToolExecutionError(RuntimeError):
    """Tool failure carrying machine-readable side-effect phase metadata."""

    def __init__(self, tool_name: str, result: Dict[str, Any]):
        detail = result.get("error") or result.get("message") or "unknown tool error"
        super().__init__(f"Tool {tool_name} failed: {detail}")
        self.tool_name = tool_name
        self.tool_result = dict(result)


class BaseRemoteAgent(ABC):
    """Base class for all remote agents."""

    def __init__(
        self,
        name: str,
        prompt: str,
        contract: AgentContract | None = None,
    ):
        self.name = name
        self.prompt = prompt
        self.contract = contract

    def result_envelope(
        self,
        *,
        outputs: Dict[str, Any] | None = None,
        error: AgentResultError | None = None,
    ) -> Dict[str, Any]:
        outputs = outputs or {}
        if outputs and error:
            status = AgentResultStatus.PARTIAL
        elif error:
            status = AgentResultStatus.ERROR
        else:
            status = AgentResultStatus.SUCCESS
        contract_version = self.contract.contract_version if self.contract else "1.0"
        envelope = AgentResultEnvelope(
            contract_version=contract_version,
            status=status,
            outputs=outputs,
            error=error,
            metadata=AgentResultMetadata(
                producer_agent=self.name,
                schema_version=contract_version,
            ),
        )
        return envelope.model_dump(mode="json")

    @staticmethod
    def execution_error(
        exc: Exception,
        *,
        tool_name: str,
    ) -> AgentResultError:
        if isinstance(exc, TimeoutError):
            return AgentResultError(
                code="REMOTE_TOOL_TIMEOUT",
                message=str(exc) or f"{tool_name} timed out",
                retryable=True,
                details={"tool": tool_name},
            )
        return AgentResultError(
            code="REMOTE_TOOL_ERROR",
            message=str(exc) or f"{tool_name} failed",
            retryable=False,
            details={"tool": tool_name},
        )

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

        manifest = _authorized_remote_tools.get()
        matching_entries = [
            expected for authorized_tool, expected in manifest
            if authorized_tool == tool_name
        ]
        if not matching_entries:
            raise PermissionError(
                f"Remote tool '{tool_name}' is outside the platform-authorized manifest"
            )
        if not isinstance(arguments, dict) or not any(
            _arguments_match_authorization(tool_name, expected, arguments)
            for expected in matching_entries
        ):
            raise PermissionError(
                f"Remote tool '{tool_name}' arguments do not match the platform-authorized manifest"
            )

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
                    raise RemoteToolExecutionError(tool_name, result)
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
