"""Stable, browser-safe workflow failure protocol.

The descriptor intentionally carries only a short user-facing message and a
small allow-listed diagnostic context.  Tracebacks, remote responses, payloads,
and validator error trees belong in server logs, not in this contract.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FailureCategory(str, Enum):
    """Coarse failure classes used by API and UI consumers."""

    ROUTING = "routing"
    EXECUTION = "execution"
    CONTRACT = "contract"
    SCHEMA = "schema"
    ARTIFACT = "artifact"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    PERSISTENCE = "persistence"
    RECONCILIATION = "reconciliation"
    PLANNING = "planning"
    INTERNAL = "internal"


class FailureCode(str, Enum):
    """Platform-owned failure codes.

    ``FailureDescriptor.code`` remains a string for straightforward JSON
    compatibility.  Builders accept this enum and safely downgrade unknown
    values to ``INTERNAL_STEP_ERROR``.
    """

    ROUTING_FAILED = "ROUTING_FAILED"
    NO_CAPABLE_AGENT = "NO_CAPABLE_AGENT"
    ROUTING_REJECTED = "ROUTING_REJECTED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    CLARIFICATION_BLOCKED = "CLARIFICATION_BLOCKED"
    DISPATCH_AGENT_MISSING = "DISPATCH_AGENT_MISSING"
    AGENT_DISPATCH_DENIED = "AGENT_DISPATCH_DENIED"

    AGENT_EXECUTION_FAILED = "AGENT_EXECUTION_FAILED"
    AGENT_BUSINESS_ERROR = "AGENT_BUSINESS_ERROR"
    AGENT_RESULT_INVALID = "AGENT_RESULT_INVALID"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    COMPLETION_CONDITION_FAILED = "COMPLETION_CONDITION_FAILED"

    CONTRACT_VERSION_MISMATCH = "CONTRACT_VERSION_MISMATCH"
    PRODUCER_AGENT_MISMATCH = "PRODUCER_AGENT_MISMATCH"
    REROUTED_AGENT_CONTRACT_MISSING = "REROUTED_AGENT_CONTRACT_MISSING"
    MISSING_REQUIRED_OUTPUT = "MISSING_REQUIRED_OUTPUT"
    UNDECLARED_OUTPUT = "UNDECLARED_OUTPUT"
    BUSINESS_RESULT_INCOMPLETE = "BUSINESS_RESULT_INCOMPLETE"
    CONTRACT_NO_OUTPUTS = "CONTRACT_NO_OUTPUTS"
    AMBIGUOUS_LEGACY_OUTPUT = "AMBIGUOUS_LEGACY_OUTPUT"

    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    UNREGISTERED_SCHEMA = "UNREGISTERED_SCHEMA"

    UPSTREAM_OUTPUT_MISSING = "UPSTREAM_OUTPUT_MISSING"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_SCHEMA_INCOMPATIBLE = "ARTIFACT_SCHEMA_INCOMPATIBLE"
    ARTIFACT_SCHEMA_INVALID = "ARTIFACT_SCHEMA_INVALID"
    ARTIFACT_SELECTOR_INVALID = "ARTIFACT_SELECTOR_INVALID"
    ARTIFACT_ACCESS_DENIED = "ARTIFACT_ACCESS_DENIED"
    UPSTREAM_STEP_FAILED = "UPSTREAM_STEP_FAILED"

    FAN_IN_BINDING_INVALID = "FAN_IN_BINDING_INVALID"
    DUPLICATE_INPUT_PARAMETER = "DUPLICATE_INPUT_PARAMETER"

    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    ARTIFACT_STORE_CORRUPTION = "ARTIFACT_STORE_CORRUPTION"
    SIDE_EFFECT_UNCONFIRMED = "SIDE_EFFECT_UNCONFIRMED"
    INTERNAL_STEP_ERROR = "INTERNAL_STEP_ERROR"
    INTERNAL_SCHEDULER_ERROR = "INTERNAL_SCHEDULER_ERROR"
    TASK_GRAPH_INVALID = "TASK_GRAPH_INVALID"
    TASK_GRAPH_MISSING = "TASK_GRAPH_MISSING"
    OPERATION_MODE_UNCLASSIFIED = "OPERATION_MODE_UNCLASSIFIED"


# Only these keys may cross the API boundary inside ``details_safe``.
SAFE_DETAIL_KEYS = frozenset(
    {
        "actual_schema_ref",
        "attempts",
        "blocked_by",
        "completion_condition",
        "expected_schema_ref",
        "logical_name",
        "missing_outputs",
        "reason_codes",
        "routing_decision",
        "schema_ref",
        "timeout_seconds",
        "undeclared_outputs",
    }
)

_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")


def _safe_value(value: Any) -> Any:
    """Reduce detail values to bounded JSON-compatible scalar/list data."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item) for item in list(value)[:50]]
    # Nested mappings and arbitrary objects can hide payloads, exception text,
    # or provider-specific representations. Never stringify them into SSE.
    return "[redacted]"


class FailureDescriptor(BaseModel):
    """Structured failure shared by checkpoints, SSE, logs, and the UI."""

    code: str
    category: FailureCategory
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False
    action: str | None = Field(default=None, max_length=1000)

    step_id: str | None = Field(default=None, max_length=256)
    agent_id: str | None = Field(default=None, max_length=256)
    parameter_name: str | None = Field(default=None, max_length=256)
    source_step: str | None = Field(default=None, max_length=256)
    source_output: str | None = Field(default=None, max_length=256)
    blocked_by: list[str] = Field(default_factory=list)
    details_safe: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        str_strip_whitespace=True,
    )

    @field_validator("code")
    @classmethod
    def _machine_readable_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not _CODE_PATTERN.fullmatch(value):
            raise ValueError(
                "failure code must contain only uppercase letters, digits, and underscores"
            )
        return value

    @field_validator("blocked_by")
    @classmethod
    def _stable_blocked_by(cls, value: list[str]) -> list[str]:
        # Deterministic ordering makes checkpoint/SSE comparisons reliable.
        return sorted(
            {
                str(item).strip()[:256]
                for item in value[:100]
                if item is not None and str(item).strip()
            }
        )

    @field_validator("details_safe", mode="before")
    @classmethod
    def _filter_safe_details(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        filtered: dict[str, Any] = {}
        for key, item in value.items():
            if key not in SAFE_DETAIL_KEYS or item is None:
                continue
            if key == "reason_codes":
                raw_codes = item if isinstance(item, (list, tuple, set)) else [item]
                filtered[key] = [
                    normalized
                    for raw in list(raw_codes)[:50]
                    for normalized in [str(raw).strip().upper()]
                    if _CODE_PATTERN.fullmatch(normalized)
                ]
                continue
            filtered[key] = _safe_value(item)
        return filtered


__all__ = [
    "FailureCategory",
    "FailureCode",
    "FailureDescriptor",
    "SAFE_DETAIL_KEYS",
]
