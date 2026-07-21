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

_WRITE_MODES = {"write", "send", "delete", "update", "create"}


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
) -> Artifact:
    """Convert an executor result into a typed :class:`Artifact`.

    ``step`` (a TaskStep) and ``context`` (an ExecutionContext) are optional; in
    the legacy publisher/while path there is no TaskStep, so callers pass
    ``logical_name``/``schema_ref`` explicitly or rely on context metadata.
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

    metadata: dict[str, Any] = {
        "operation_mode": operation_mode,
        "executor_success": is_success,
    }
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

    sensitivity = Sensitivity.INTERNAL
    if context is not None:
        meta = getattr(context, "metadata", None) or {}
        if str(meta.get("risk_profile", "")).upper() in {"HIGH", "CRITICAL"}:
            sensitivity = Sensitivity.CONFIDENTIAL

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
