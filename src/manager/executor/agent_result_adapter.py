"""Normalize remote Agent results before they enter the Artifact data plane.

Contracted Agents must publish a versioned :class:`AgentResultEnvelope`.  This
module also provides a deliberately narrow compatibility path for legacy
Agents: a legacy payload is accepted only when it can be mapped to the declared
outputs without guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from src.contracts.agent_contract import AgentContract
from src.contracts.agent_result import (
    AgentResultEnvelope,
    AgentResultStatus,
    validate_agent_result,
)
from src.orchestration.schema_registry import SchemaRegistry, get_schema_registry


class AgentResultNormalizationError(ValueError):
    """An executor result cannot safely be exposed as downstream data."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Any = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.details = details
        # Preserved from the envelope's error object so the scheduler can keep
        # the Agent's own retryability verdict in the failed step's metrics.
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class NormalizedAgentResult:
    """Internal representation consumed by the Artifact adapter."""

    outputs: dict[str, Any]
    schema_refs: dict[str, str | None] = field(default_factory=dict)
    contract_version: str | None = None
    producer_agent: str | None = None
    legacy: bool = False


def _coerce_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return value
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return value


def _looks_like_envelope(value: Any) -> bool:
    return isinstance(value, dict) and {
        "contract_version",
        "status",
        "outputs",
        "metadata",
    }.issubset(value)


def _register_missing_agent_schemas(registry: SchemaRegistry) -> SchemaRegistry:
    """Install built-in Agent schemas without replacing existing definitions."""

    from src.contracts.agent_schema_catalog import AGENT_SCHEMA_CATALOG

    for schema_ref, schema in AGENT_SCHEMA_CATALOG.items():
        if not registry.has(schema_ref):
            registry.register(schema_ref, schema)
    return registry


def _legacy_error(payload: Any) -> tuple[str, str, Any] | None:
    if not isinstance(payload, dict):
        return None
    status = (
        str(payload.get("operation_status") or payload.get("status") or "")
        .strip()
        .lower()
    )
    if status in {
        "failed",
        "failure",
        "error",
        "rejected",
        "cancelled",
        "canceled",
        "timeout",
    }:
        return (
            "BUSINESS_RESULT_ERROR",
            str(payload.get("error") or payload.get("message") or status),
            None,
        )
    # A legacy partial result is as unsafe to publish as an explicit error:
    # downstream consumers cannot tell which declared data is missing.
    if status == "partial":
        return (
            "BUSINESS_RESULT_INCOMPLETE",
            str(payload.get("error") or payload.get("message") or "legacy result is partial"),
            None,
        )
    # Any explicit error field fails closed, even when the payload also carries
    # outputs: a result that reports an error must never enter the data plane.
    error = payload.get("error")
    if error:
        if isinstance(error, dict):
            # Mirror the envelope path: the remote business code is kept in
            # details for server-side diagnostics, never as the platform code.
            details = {"remote_code": error.get("code")} if error.get("code") else None
            return (
                "BUSINESS_RESULT_ERROR",
                str(error.get("message") or error),
                details,
            )
        return "BUSINESS_RESULT_ERROR", str(error), None
    return None


def _legacy_outputs(payload: Any, contract: AgentContract) -> dict[str, Any]:
    names = [ref.name for ref in contract.produces]
    if not names:
        raise AgentResultNormalizationError(
            "CONTRACT_NO_OUTPUTS",
            "Agent contract does not declare any outputs",
        )
    if isinstance(payload, dict):
        matched = {name: payload[name] for name in names if name in payload}
        if matched:
            return matched
    if len(names) == 1:
        return {names[0]: payload}
    raise AgentResultNormalizationError(
        "AMBIGUOUS_LEGACY_OUTPUT",
        "Legacy result cannot be mapped to multiple declared outputs",
    )


def normalize_agent_result(
    execute_result: Any,
    *,
    agent_contract: AgentContract | dict[str, Any] | None = None,
    expected_outputs: list[str] | None = None,
    producer_agent: str | None = None,
    schema_registry: SchemaRegistry | None = None,
) -> NormalizedAgentResult:
    """Normalize and validate one executor result.

    Contracted results fail closed on malformed envelopes, business errors,
    partial results, missing required outputs, undeclared outputs, and Schema
    errors.  Uncontracted legacy results preserve the historical single-output
    behavior while still rejecting explicit business-error payloads.
    """

    if not bool(getattr(execute_result, "is_success", False)):
        raise AgentResultNormalizationError(
            "EXECUTION_FAILED",
            str(getattr(execute_result, "error", None) or "Agent execution failed"),
        )

    payload = _coerce_json(getattr(execute_result, "result", None))
    legacy_error = _legacy_error(payload)
    if legacy_error is not None and not _looks_like_envelope(payload):
        code, message, details = legacy_error
        raise AgentResultNormalizationError(code, message, details=details)

    if agent_contract is None:
        names = list(expected_outputs or [])
        if not names:
            names = ["result"]
        # Preserve the historical uncontracted behavior where one legacy
        # payload was exposed under every planner-declared output alias.
        return NormalizedAgentResult(
            outputs={name: payload for name in names},
            legacy=True,
        )

    contract = (
        agent_contract
        if isinstance(agent_contract, AgentContract)
        else AgentContract.model_validate(agent_contract)
    )
    if schema_registry is None:
        # The contract catalog is part of the platform protocol and must be
        # available on the production path, not only in contract unit tests.
        # Only fill missing entries: callers may have registered a stricter
        # schema under the same versioned reference.
        registry = _register_missing_agent_schemas(get_schema_registry())
    else:
        registry = schema_registry

    if _looks_like_envelope(payload):
        try:
            envelope = AgentResultEnvelope.model_validate(payload)
        except ValidationError as exc:
            raise AgentResultNormalizationError(
                "INVALID_ENVELOPE",
                "Agent result envelope is invalid",
                details=exc.errors(include_url=False),
            ) from exc
    else:
        outputs = _legacy_outputs(payload, contract)
        envelope_payload = {
            "contract_version": contract.contract_version,
            "status": "success",
            "outputs": outputs,
            "metadata": {
                "producer_agent": producer_agent or "legacy-agent",
                "schema_version": contract.contract_version,
            },
        }
        envelope = AgentResultEnvelope.model_validate(envelope_payload)

    if producer_agent and envelope.metadata.producer_agent != producer_agent:
        raise AgentResultNormalizationError(
            "PRODUCER_AGENT_MISMATCH",
            (
                f"result producer_agent {envelope.metadata.producer_agent!r} does "
                f"not match selected Agent {producer_agent!r}"
            ),
        )
    if envelope.metadata.schema_version != contract.contract_version:
        raise AgentResultNormalizationError(
            "RESULT_SCHEMA_VERSION_MISMATCH",
            (
                f"result schema_version {envelope.metadata.schema_version!r} does "
                f"not match Agent contract {contract.contract_version!r}"
            ),
        )

    if envelope.status == AgentResultStatus.ERROR:
        assert envelope.error is not None
        raise AgentResultNormalizationError(
            "BUSINESS_RESULT_ERROR",
            envelope.error.message,
            details={
                "remote_code": envelope.error.code,
                "remote_details": envelope.error.details,
            },
            retryable=envelope.error.retryable,
        )
    if envelope.status == AgentResultStatus.PARTIAL:
        raise AgentResultNormalizationError(
            "BUSINESS_RESULT_INCOMPLETE",
            envelope.error.message if envelope.error else "Agent result is partial",
            details=envelope.error.details if envelope.error else None,
            retryable=bool(envelope.error and envelope.error.retryable),
        )

    validation = validate_agent_result(envelope, contract, registry)
    if not validation.valid:
        first = validation.errors[0]
        raise AgentResultNormalizationError(
            first.code,
            first.message,
            details=[error.model_dump(mode="json") for error in validation.errors],
        )

    missing = [
        ref.name
        for ref in contract.produces
        if ref.required and ref.name not in envelope.outputs
    ]
    if missing:
        raise AgentResultNormalizationError(
            "MISSING_REQUIRED_OUTPUT",
            f"Agent result is missing required outputs: {missing}",
            details={"missing": missing},
        )

    return NormalizedAgentResult(
        outputs=dict(envelope.outputs),
        schema_refs={
            name: contract.output_schema_refs.get(name) for name in envelope.outputs
        },
        contract_version=envelope.contract_version,
        producer_agent=envelope.metadata.producer_agent,
        legacy=not _looks_like_envelope(payload),
    )
