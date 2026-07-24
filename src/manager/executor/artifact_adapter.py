"""Adapter: executor result -> typed Artifact (Plan §7, Phase 2).

Converts a Tool/Agent :class:`~src.manager.executor.base.ExecuteResult` into an
:class:`~src.interface.artifact.Artifact`: computes a checksum, runs schema
validation when a schema is known, and applies the plan's degradation rules:

- Read-only results with no output schema are captured but flagged low-confidence
  / untyped (``schema_valid`` left ``None``).
- Write/send results with no output schema must NOT be passed downstream as typed
  data: they are flagged ``schema_valid=False`` with an explicit warning so the
  scheduler/resolver can refuse to consume them.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from src.interface.artifact import Artifact, ArtifactRef, Sensitivity, compute_checksum
from src.orchestration.schema_registry import SchemaRegistry, get_schema_registry

_WRITE_MODES = {
    "write",
    "send",
    "delete",
    "update",
    "create",
    "submit",
    "approve",
    "execute",
    "export",
}

# Sensitivity ordering: an artifact is at least as sensitive as the most
# sensitive datum it derives from (upstream) and at least as sensitive as the
# task risk implies. Never blindly default to INTERNAL for sensitive lineage.
_SENSITIVITY_ORDER = {
    Sensitivity.PUBLIC.value: 0,
    Sensitivity.INTERNAL.value: 1,
    Sensitivity.CONFIDENTIAL.value: 2,
    Sensitivity.RESTRICTED.value: 3,
}
_ORDER_TO_SENSITIVITY = {v: k for k, v in _SENSITIVITY_ORDER.items()}


def _coerce_sensitivity_value(value: Any) -> Optional[str]:
    raw = getattr(value, "value", value)
    if raw is None:
        return None
    text = str(raw).lower()
    return text if text in _SENSITIVITY_ORDER else None


def _coerce_payload(result: Any) -> Any:
    """Best-effort turn a raw executor result into a structured payload.

    A JSON-object string is parsed to a dict; everything else is passed through.
    """
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                return json.loads(stripped)
            except Exception:
                return result
    return result


def _resolve_operation_mode(step: Any, context: Any) -> str:
    if step is not None and getattr(step, "operation_mode", None):
        return str(step.operation_mode).lower()
    if context is not None:
        meta = getattr(context, "metadata", None) or {}
        mode = meta.get("operation_mode")
        if mode:
            return str(mode).lower()
    return "read"


def _resolve_schema_ref(step: Any, explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    if step is not None:
        # A step may declare an expected schema for its primary output.
        ref = getattr(step, "expected_schema_ref", None)
        if ref:
            return str(ref)
    return None


def _resolve_logical_name(step: Any, context: Any, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if step is not None and getattr(step, "expected_outputs", None):
        return str(step.expected_outputs[0])
    if context is not None:
        meta = getattr(context, "metadata", None) or {}
        node = meta.get("node_name")
        if node:
            return f"{node}_result"
    return "result"


def _lineage(step: Any) -> List[ArtifactRef]:
    if step is None:
        return []
    required = getattr(step, "required_inputs", None) or {}
    refs: List[ArtifactRef] = []
    for value in required.values():
        if isinstance(value, ArtifactRef):
            refs.append(value)
    return refs


def to_artifact(
    execute_result: Any,
    step: Any = None,
    context: Any = None,
    *,
    logical_name: Optional[str] = None,
    schema_ref: Optional[str] = None,
    schema_registry: Optional[SchemaRegistry] = None,
    upstream_sensitivities: Optional[List[Any]] = None,
) -> Artifact:
    """Convert an executor result into a typed :class:`Artifact`.

    ``step`` (a TaskStep) and ``context`` (an ExecutionContext) are optional; in
    the legacy publisher/while path there is no TaskStep, so callers pass
    ``logical_name``/``schema_ref`` explicitly or rely on context metadata.

    ``upstream_sensitivities`` are the sensitivities of the artifacts this step
    consumed; the produced artifact's sensitivity is raised to the maximum of
    the task-risk-derived level and the upstream levels.
    """
    registry = schema_registry or get_schema_registry()

    is_success = bool(getattr(execute_result, "is_success", False))
    raw_result = getattr(execute_result, "result", None)
    error = getattr(execute_result, "error", None)

    payload: Any = _coerce_payload(raw_result)
    if payload is None:
        # Preserve a non-empty payload so the Artifact model stays valid.
        payload = {"error": error} if error else {"status": "empty"}

    operation_mode = _resolve_operation_mode(step, context)
    resolved_schema = _resolve_schema_ref(step, schema_ref)
    name = _resolve_logical_name(step, context, logical_name)

    ctx_meta = getattr(context, "metadata", None) or {
    } if context is not None else {}
    risk_level = str(ctx_meta.get("risk_profile", "LOW")).upper()

    metadata: dict[str, Any] = {
        "operation_mode": operation_mode,
        "executor_success": is_success,
        "risk_level": risk_level,
    }
    # Preserve the machine-readable outcome envelope and receipt identifiers,
    # but never copy the entire executor metadata (it may contain transport
    # details or request context that does not belong in an Artifact).
    exec_metadata = getattr(execute_result, "metadata", None) or {}
    if isinstance(exec_metadata, dict):
        business_outcome = exec_metadata.get("business_outcome")
        if isinstance(business_outcome, dict):
            metadata["business_outcome"] = dict(business_outcome)
        for key in (
            "external_op_id",
            "external_operation_id",
            "idempotency_key",
            "receipt_status",
            "verification_trusted",
        ):
            if exec_metadata.get(key) is not None:
                metadata[key] = exec_metadata[key]
    # Carry the acting scenario/capability domain + provenance so a downstream
    # artifact-read guard can evaluate S-ABAC scenario fit, ownership and
    # clearance against real data.
    if context is not None:
        scenario_tags = ctx_meta.get("scenario_tags")
        if scenario_tags:
            metadata["scenario_tags"] = list(scenario_tags)
        expected_capabilities = ctx_meta.get("expected_capabilities")
        if expected_capabilities:
            metadata["expected_capabilities"] = list(expected_capabilities)
        acting_user = getattr(context, "user_id", None)
        if acting_user:
            metadata["owner_user_id"] = acting_user
            metadata["producer_subject"] = acting_user
        # Governed collaboration: a producing step may declare the users allowed
        # to read its output across ownership. The artifact guard honors this
        # ``allowed_reader_ids`` roster so cross-user sharing is *governed* rather
        # than impossible. Sourced from the step first, then context metadata.
        # NOTE (production hardening): in production this roster must come from a
        # TRUSTED authority, not raw (untrusted) planner output.
        allowed_readers = None
        if step is not None:
            allowed_readers = getattr(step, "allowed_reader_ids", None)
        if not allowed_readers:
            allowed_readers = ctx_meta.get("allowed_reader_ids")
        if allowed_readers:
            metadata["allowed_reader_ids"] = [str(r) for r in allowed_readers]
        producer_agent_id = ctx_meta.get(
            "producer_agent_id") or ctx_meta.get("selected_agent")
        if producer_agent_id:
            metadata["producer_agent_id"] = producer_agent_id
        metadata["data_source"] = (
            producer_agent_id or ctx_meta.get("node_name") or "executor"
        )
    schema_valid: Optional[bool] = None

    if resolved_schema and registry.has(resolved_schema):
        valid, errors = registry.validate(payload, resolved_schema)
        schema_valid = valid
        if not valid:
            metadata["schema_errors"] = errors
    else:
        # No usable output schema for this result.
        metadata["typed"] = False
        metadata["confidence"] = "low"
        if operation_mode in _WRITE_MODES:
            # Untyped write/send output must not be consumed downstream as typed.
            schema_valid = False
            metadata["warning"] = (
                "untyped output from a write/send operation; downstream steps "
                "must not consume it as typed data"
            )

    # Sensitivity = max(task-risk-derived, most sensitive upstream datum).
    if risk_level in {"HIGH", "CRITICAL"}:
        sensitivity_order = _SENSITIVITY_ORDER[Sensitivity.CONFIDENTIAL.value]
    else:
        sensitivity_order = _SENSITIVITY_ORDER[Sensitivity.INTERNAL.value]
    for upstream in upstream_sensitivities or []:
        value = _coerce_sensitivity_value(upstream)
        if value is not None:
            sensitivity_order = max(
                sensitivity_order, _SENSITIVITY_ORDER[value])
    sensitivity = _ORDER_TO_SENSITIVITY[sensitivity_order]

    return Artifact(
        logical_name=name,
        schema_ref=resolved_schema,
        payload=payload,
        checksum=compute_checksum(payload),
        derived_from=_lineage(step),
        sensitivity=sensitivity,
        schema_valid=schema_valid,
        metadata=metadata,
    )
