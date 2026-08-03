"""Execution evidence used by workflow-skill distillation.

The executor status answers whether a call returned successfully.  Skill
distillation needs a stronger, step-level record that separates technical
success from a verified business effect.  This module deliberately stores
references and verification metadata only; raw payloads remain in the
Artifact/receipt stores.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from src.interface.artifact import Artifact, ArtifactRef, StepStatus
from src.interface.task_graph import TaskGraph, WorkflowStatus


SIDE_EFFECT_MODES = frozenset(
    {
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
)

_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "error", "rejected", "cancelled", "canceled", "timeout"}
)
_SUCCESS_STATUSES = frozenset(
    {
        "success",
        "succeeded",
        "completed",
        "accepted",
        "submitted",
        "created",
        "updated",
        "deleted",
        "delivered",
        "queued",
        "pending_approval",
    }
)


def _safe_scalar(value: Any) -> str | int | None:
    """Return an identifier-safe scalar, excluding booleans and containers."""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    return value


def _sanitize_verification_contract(value: Any) -> dict[str, Any]:
    """Keep contract semantics and references without arbitrary planner text."""

    raw = value if isinstance(value, Mapping) else {}
    sanitized: dict[str, Any] = {}
    for key in ("required", "trusted_verifier_required"):
        if isinstance(raw.get(key), bool):
            sanitized[key] = raw[key]
    for key in (
        "method",
        "verification_ref",
        "evidence_ref",
        "receipt_ref",
        "provider_reference",
        "schema_ref",
    ):
        scalar = _safe_scalar(raw.get(key))
        if scalar is not None:
            sanitized[key] = scalar
    return sanitized


def _sanitize_task_graph(task_graph: Any) -> dict[str, Any]:
    """Return topology/schema metadata without goals, descriptions, or inputs."""

    if isinstance(task_graph, TaskGraph):
        raw_graph: Mapping[str, Any] = task_graph.model_dump(mode="json")
    elif isinstance(task_graph, Mapping):
        raw_graph = task_graph
    else:
        return {}

    safe_steps: list[dict[str, Any]] = []
    raw_steps = raw_graph.get("steps", [])
    if isinstance(raw_steps, list):
        for raw_step in raw_steps:
            if not isinstance(raw_step, Mapping):
                continue
            step_id = _safe_scalar(raw_step.get("step_id"))
            if step_id is None:
                continue
            safe_step: dict[str, Any] = {
                "step_id": str(step_id),
                "depends_on": [
                    str(dep)
                    for dep in raw_step.get("depends_on", [])
                    if _safe_scalar(dep) is not None
                ],
                "operation_mode": str(raw_step.get("operation_mode") or "read"),
                "risk_level": str(raw_step.get("risk_level") or "LOW"),
            }
            agent_name = _safe_scalar(raw_step.get("agent_name"))
            if agent_name is not None:
                safe_step["agent_name"] = str(agent_name)
            capabilities = raw_step.get("required_capabilities")
            if isinstance(capabilities, list):
                safe_step["required_capabilities"] = [
                    str(item)
                    for item in capabilities
                    if _safe_scalar(item) is not None
                ]
            schema_ref = _safe_scalar(
                raw_step.get("expected_schema_ref")
                or raw_step.get("output_schema_ref")
            )
            if schema_ref is not None:
                safe_step["expected_schema_ref"] = str(schema_ref)
            contract = _sanitize_verification_contract(
                raw_step.get("verification_contract")
            )
            if contract:
                safe_step["verification_contract"] = contract
            input_bindings = raw_step.get("input_bindings") or raw_step.get(
                "required_inputs"
            )
            if isinstance(input_bindings, Mapping):
                safe_step["input_names"] = sorted(str(key) for key in input_bindings)
            safe_steps.append(safe_step)

    safe_graph: dict[str, Any] = {
        "schema_version": raw_graph.get("schema_version", 1),
        "steps": safe_steps,
    }
    canonical = json.dumps(
        safe_graph, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    safe_graph["graph_hash"] = hashlib.sha256(canonical).hexdigest()
    return safe_graph


class VerificationStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"


class StepExecutionEvidence(BaseModel):
    """Sanitized evidence for one workflow step."""

    model_config = ConfigDict(extra="allow")

    step_id: str
    agent_name: str = ""
    planned_agent: str = ""
    executed_agent: str = ""
    operation_mode: str = "read"
    risk_level: str = "LOW"
    technical_success: bool = False
    business_success: bool | None = None
    outcome_status: str = "unknown"
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_method: str | None = None
    verification_evidence_ref: str | None = None
    schema_valid: bool | None = None
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)
    external_operation_id: str | None = None
    idempotency_key: str | None = None
    receipt_status: str | None = None
    needs_reconciliation: bool = False
    error: str | None = None

    @property
    def is_side_effect(self) -> bool:
        return self.operation_mode.lower() in SIDE_EFFECT_MODES


class SkillExecutionEvidence(BaseModel):
    """Workflow-level evidence consumed by the skill distiller."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    task_id: str
    workflow_id: str = ""
    execution_mode: str = "legacy"
    workflow_status: str = "UNKNOWN"
    technical_success: bool = False
    business_success: bool | None = None
    step_coverage: float = 0.0
    business_outcome_coverage: float = 0.0
    steps: list[StepExecutionEvidence] = Field(default_factory=list)
    task_graph: dict[str, Any] = Field(default_factory=dict)
    planning_steps: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def outcome_summary(self) -> dict[str, Any]:
        """Return the stable, payload-free shape stored with skill evidence."""

        return {
            "evidence_schema_version": self.schema_version,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "technical_success": self.technical_success,
            "business_success": self.business_success,
            "step_coverage": self.step_coverage,
            "business_outcome_coverage": self.business_outcome_coverage,
            "workflow_status": self.workflow_status,
            "execution_mode": self.execution_mode,
            "steps": [item.model_dump(mode="json") for item in self.steps],
            "task_graph": _sanitize_task_graph(self.task_graph),
        }


class DistillationDecision(BaseModel):
    """Explain whether an execution trace may contribute to a skill."""

    eligible: bool
    promotion_ready: bool
    reasons: list[str] = Field(default_factory=list)


def evaluate_distillation_evidence(
    evidence: SkillExecutionEvidence,
) -> DistillationDecision:
    """Apply the shared automatic/manual distillation gate.

    A technically successful, structurally recorded trace may contribute a
    candidate.  A side-effect trace can only promote a candidate when every
    business outcome is verified.  Explicit business failure is never learned
    as a successful procedure.
    """

    reasons: list[str] = []
    if not evidence.steps:
        reasons.append("no_step_execution_evidence")
    elif evidence.step_coverage < 1.0:
        reasons.append("incomplete_step_execution_evidence")
    if not evidence.technical_success:
        reasons.append("workflow_not_technically_successful")
    if evidence.business_success is False:
        reasons.append("business_outcome_failed")
    eligible = not reasons
    side_effects = [step for step in evidence.steps if step.is_side_effect]
    promotion_ready = eligible and (
        not side_effects
        or (
            evidence.business_success is True
            and evidence.business_outcome_coverage >= 1.0
            and all(
                step.verification_status == VerificationStatus.VERIFIED
                for step in side_effects
            )
        )
    )
    if eligible and side_effects and not promotion_ready:
        reasons.append("business_outcome_not_fully_verified")
    return DistillationDecision(
        eligible=eligible,
        promotion_ready=promotion_ready,
        reasons=reasons,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload_from_result(execute_result: Any) -> Any:
    return getattr(execute_result, "result", None)


def _metadata_from_result(execute_result: Any) -> Mapping[str, Any]:
    return _mapping(getattr(execute_result, "metadata", None))


def _status_from_payload(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return "unknown"
    nested = payload.get("business_outcome")
    nested_mapping = _mapping(nested)
    status = (
        nested_mapping.get("operation_status")
        or nested_mapping.get("status")
        or payload.get("operation_status")
        or payload.get("status")
    )
    return str(status).strip().lower() if status is not None else "unknown"


def _business_outcome(payload: Any, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    explicit = metadata.get("business_outcome")
    if isinstance(explicit, Mapping):
        return explicit
    if isinstance(payload, Mapping) and isinstance(payload.get("business_outcome"), Mapping):
        return payload["business_outcome"]
    return {}


def _resource_ids(payload: Any, outcome: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []

    def append_id(value: Any) -> None:
        scalar = _safe_scalar(value)
        if scalar is not None:
            ids.append(str(scalar))

    resource = _mapping(outcome.get("resource"))
    for value in (resource.get("id"), outcome.get("resource_id")):
        append_id(value)
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            normalized = str(key).lower()
            if normalized == "id" or normalized.endswith("_id"):
                if normalized not in {"employee_id", "user_id", "workflow_id", "task_id"}:
                    append_id(value)
    return list(dict.fromkeys(ids))


def _artifact_parts(
    artifact: Any,
) -> tuple[dict[str, Any] | None, Any, bool | None, Mapping[str, Any]]:
    if artifact is None:
        return None, None, None, {}
    if isinstance(artifact, Artifact):
        return (
            artifact.ref().model_dump(mode="json"),
            artifact.payload,
            artifact.schema_valid,
            _mapping(artifact.metadata),
        )
    if isinstance(artifact, Mapping):
        ref = ArtifactRef(
            artifact_id=str(artifact.get("artifact_id", "")),
            version=artifact.get("version"),
            expected_schema_ref=artifact.get("schema_ref"),
        ) if artifact.get("artifact_id") else None
        return (
            ref.model_dump(mode="json") if ref else None,
            artifact.get("payload"),
            artifact.get("schema_valid"),
            _mapping(artifact.get("metadata")),
        )
    return None, None, None, {}


def build_step_evidence(
    *,
    step_id: str,
    agent_name: str = "",
    operation_mode: str = "read",
    risk_level: str = "LOW",
    verification_contract: Mapping[str, Any] | None = None,
    execute_result: Any = None,
    artifact: Any = None,
    step_result: Any = None,
    receipt_status: str | None = None,
    idempotency_key: str | None = None,
) -> StepExecutionEvidence:
    """Normalize an executor result without trusting free-form text."""

    metadata = dict(_metadata_from_result(execute_result))
    payload = _payload_from_result(execute_result)
    artifact_ref, artifact_payload, schema_valid, artifact_metadata = _artifact_parts(
        artifact
    )
    for key, value in artifact_metadata.items():
        metadata.setdefault(key, value)
    if payload is None and artifact_payload is not None:
        payload = artifact_payload

    technical_success = bool(getattr(execute_result, "is_success", False))
    if step_result is not None:
        status = getattr(step_result, "status", None)
        status_value = getattr(status, "value", status)
        if status_value is not None:
            technical_success = str(status_value).upper() == StepStatus.SUCCEEDED.value

    outcome = _business_outcome(payload, metadata)
    outcome_status = str(
        outcome.get("operation_status")
        or outcome.get("status")
        or _status_from_payload(payload)
    ).strip().lower()
    if not outcome_status:
        outcome_status = "unknown"

    nested_failure = outcome_status in _FAILURE_STATUSES
    if isinstance(payload, Mapping) and not outcome:
        nested_failure = nested_failure or _status_from_payload(payload) in _FAILURE_STATUSES

    metrics = _mapping(getattr(step_result, "metrics", None))
    needs_reconciliation = bool(metrics.get("needs_reconciliation"))
    if not needs_reconciliation:
        needs_reconciliation = bool(metadata.get("needs_reconciliation"))
    receipt_status = receipt_status or metrics.get("receipt_status") or metadata.get("receipt_status")
    external_operation_id = (
        outcome.get("external_operation_id")
        or outcome.get("external_op_id")
        or metadata.get("external_operation_id")
        or metadata.get("external_op_id")
        or metrics.get("external_operation_id")
        or metrics.get("external_op_id")
    )
    idempotency_key = (
        idempotency_key
        or outcome.get("idempotency_key")
        or metadata.get("idempotency_key")
        or metrics.get("idempotency_key")
    )

    verification = _mapping(outcome.get("verification"))
    verification_evidence_ref = (
        verification.get("evidence_ref")
        or verification.get("receipt_ref")
        or verification.get("provider_reference")
    )
    explicitly_verified = (
        verification.get("verified") is True
        and metadata.get("verification_trusted") is True
        and bool(verification_evidence_ref)
    )
    verification_method = verification.get("method")

    normalized_mode = str(operation_mode or "read").lower()
    normalized_risk = str(risk_level or "LOW").upper()
    contract = _mapping(verification_contract)
    strong_verification_required = (
        normalized_risk in {"HIGH", "CRITICAL"}
        or normalized_mode in {"approve", "delete"}
        or contract.get("trusted_verifier_required") is True
    )
    if not technical_success or nested_failure:
        business_success: bool | None = False
        verification_status = VerificationStatus.FAILED
    elif normalized_mode not in SIDE_EFFECT_MODES:
        business_success = True
        verification_status = VerificationStatus.NOT_REQUIRED
        verification_method = verification_method or "technical_read_success"
    elif needs_reconciliation:
        business_success = None
        verification_status = VerificationStatus.UNVERIFIED
        verification_method = verification_method or "reconciliation_required"
    elif explicitly_verified:
        business_success = True
        verification_status = VerificationStatus.VERIFIED
        verification_method = verification_method or "declared_verification"
    elif (
        not strong_verification_required
        and str(receipt_status).upper() == "SUCCEEDED"
        and external_operation_id
    ):
        # Payload schema governs downstream data use. Business verification is
        # independently backed by the platform receipt and durable provider id.
        business_success = True
        verification_status = VerificationStatus.VERIFIED
        verification_method = verification_method or "provider_receipt"
    elif (
        not strong_verification_required
        and str(receipt_status).upper() == "SUCCEEDED"
        and _resource_ids(payload, outcome)
        and schema_valid is True
    ):
        business_success = True
        verification_status = VerificationStatus.VERIFIED
        verification_method = verification_method or "typed_resource_receipt"
    else:
        business_success = None
        verification_status = VerificationStatus.UNVERIFIED
        verification_method = verification_method or (
            "trusted_verifier_required"
            if strong_verification_required
            else "missing_business_verifier"
        )

    error = getattr(execute_result, "error", None)
    return StepExecutionEvidence(
        step_id=str(step_id),
        agent_name=str(agent_name or ""),
        operation_mode=normalized_mode,
        risk_level=normalized_risk,
        technical_success=technical_success,
        business_success=business_success,
        outcome_status=outcome_status,
        verification_status=verification_status,
        verification_method=str(verification_method) if verification_method else None,
        verification_evidence_ref=(
            str(verification_evidence_ref) if verification_evidence_ref else None
        ),
        schema_valid=schema_valid,
        artifact_refs=[artifact_ref] if artifact_ref else [],
        resource_ids=_resource_ids(payload, outcome),
        external_operation_id=str(external_operation_id) if external_operation_id else None,
        idempotency_key=str(idempotency_key) if idempotency_key else None,
        receipt_status=str(receipt_status) if receipt_status else None,
        needs_reconciliation=needs_reconciliation,
        error=str(error) if error else None,
    )


def aggregate_evidence(
    *,
    task_id: str,
    workflow_id: str = "",
    execution_mode: str = "legacy",
    workflow_status: str = "UNKNOWN",
    steps: Iterable[StepExecutionEvidence],
    task_graph: TaskGraph | Mapping[str, Any] | None = None,
    planning_steps: Iterable[Mapping[str, Any]] | None = None,
) -> SkillExecutionEvidence:
    normalized_steps = list(steps)
    normalized_plan = [
        dict(item) for item in (planning_steps or ()) if isinstance(item, Mapping)
    ]

    expected_graph_ids: list[str] = []
    if isinstance(task_graph, TaskGraph):
        expected_graph_ids = [str(step.step_id) for step in task_graph.steps]
    elif isinstance(task_graph, Mapping):
        raw_steps = task_graph.get("steps")
        if isinstance(raw_steps, list):
            expected_graph_ids = [
                str(item.get("step_id"))
                for item in raw_steps
                if isinstance(item, Mapping) and item.get("step_id")
            ]

    if expected_graph_ids:
        actual_ids = {item.step_id for item in normalized_steps}
        covered_steps = sum(step_id in actual_ids for step_id in expected_graph_ids)
        step_coverage = covered_steps / len(expected_graph_ids)
    elif normalized_plan:
        expected_ids = [
            str(item.get("step_id"))
            for item in normalized_plan
            if item.get("step_id")
        ]
        if len(expected_ids) == len(normalized_plan):
            actual_ids = {item.step_id for item in normalized_steps}
            covered_steps = sum(step_id in actual_ids for step_id in expected_ids)
            # Records created by the legacy publisher/agent_proxy loop before
            # Planner-id binding used runtime keys such as ``2:SomeAgent``.
            # That loop executes one Agent for all plan steps assigned to it, so
            # agent identity is the only durable join key available in those
            # historical records.  Restrict this compatibility path to legacy
            # mode; Scheduler evidence must continue matching exact step ids.
            if (
                covered_steps < len(normalized_plan)
                and str(execution_mode).lower() == "legacy"
            ):
                actual_agents = {
                    item.agent_name
                    for item in normalized_steps
                    if item.agent_name
                }
                legacy_covered_steps = sum(
                    str(item.get("agent_name") or "") in actual_agents
                    for item in normalized_plan
                )
                covered_steps = max(covered_steps, legacy_covered_steps)
        else:
            expected_agents = Counter(
                str(item.get("agent_name"))
                for item in normalized_plan
                if item.get("agent_name")
            )
            actual_agents = Counter(
                item.agent_name for item in normalized_steps if item.agent_name
            )
            if sum(expected_agents.values()) == len(normalized_plan):
                covered_steps = sum(
                    min(count, actual_agents.get(agent_name, 0))
                    for agent_name, count in expected_agents.items()
                )
            else:
                covered_steps = min(len(normalized_steps), len(normalized_plan))
        step_coverage = covered_steps / len(normalized_plan)
    else:
        step_coverage = 1.0 if normalized_steps else 0.0

    terminal = str(workflow_status or "UNKNOWN").upper()
    technical_success = step_coverage == 1.0 and terminal in {
        WorkflowStatus.SUCCEEDED.value,
        "COMPLETED",
    } and all(item.technical_success for item in normalized_steps)

    side_effects = [item for item in normalized_steps if item.is_side_effect]
    resolved_results = [item.business_success for item in side_effects]
    if not technical_success:
        business_success: bool | None = False
    elif not side_effects:
        business_success = True
    elif any(item is False for item in resolved_results):
        business_success = False
    elif all(item is True for item in resolved_results):
        business_success = True
    else:
        business_success = None
    coverage = (
        sum(item is not None for item in resolved_results) / len(side_effects)
        if side_effects
        else 1.0
    )
    graph_payload: dict[str, Any] = {}
    if task_graph is not None:
        graph_payload = _sanitize_task_graph(task_graph)
    return SkillExecutionEvidence(
        task_id=str(task_id),
        workflow_id=str(workflow_id or ""),
        execution_mode=str(execution_mode),
        workflow_status=terminal,
        technical_success=technical_success,
        business_success=business_success,
        step_coverage=round(step_coverage, 8),
        business_outcome_coverage=round(coverage, 8),
        steps=normalized_steps,
        task_graph=graph_payload,
        planning_steps=normalized_plan,
    )


def load_execution_evidence(
    raw: Mapping[str, Any],
    *,
    planning_steps: Iterable[Mapping[str, Any]] | None = None,
    task_graph: TaskGraph | Mapping[str, Any] | None = None,
) -> SkillExecutionEvidence:
    """Load evidence and safely derive coverage for records from older schemas."""

    evidence = SkillExecutionEvidence.model_validate(raw)
    if "step_coverage" in raw:
        return evidence
    return aggregate_evidence(
        task_id=evidence.task_id,
        workflow_id=evidence.workflow_id,
        execution_mode=evidence.execution_mode,
        workflow_status=evidence.workflow_status,
        steps=evidence.steps,
        task_graph=task_graph or evidence.task_graph or None,
        planning_steps=planning_steps or evidence.planning_steps,
    )


def build_legacy_evidence(
    *,
    task_id: str,
    workflow_id: str,
    execution_failed: bool,
    step_evidence: Iterable[Mapping[str, Any]] | None,
    planning_steps: Iterable[Mapping[str, Any]] | None = None,
) -> SkillExecutionEvidence:
    steps = [
        StepExecutionEvidence(**dict(item))
        for item in (step_evidence or ())
        if isinstance(item, Mapping)
    ]
    return aggregate_evidence(
        task_id=task_id,
        workflow_id=workflow_id,
        execution_mode="legacy",
        workflow_status="FAILED" if execution_failed else "COMPLETED",
        steps=steps,
        planning_steps=planning_steps,
    )


def build_scheduler_evidence(
    *,
    task_id: str,
    workflow_id: str,
    graph: TaskGraph,
    results: Mapping[str, Any],
    artifact_store: Any,
    receipt_store: Any = None,
    planning_steps: Iterable[Mapping[str, Any]] | None = None,
    workflow_status: str | None = None,
) -> SkillExecutionEvidence:
    """Build evidence from the scheduler's authoritative step results."""

    step_evidence: list[StepExecutionEvidence] = []
    for step in graph.steps:
        result = results.get(step.step_id) if isinstance(results, Mapping) else None
        planned_agent = str(
            getattr(step, "agent_name", "") or step.preferred_resource_id or ""
        )
        metrics = _mapping(getattr(result, "metrics", None)) if result is not None else {}
        executed_agent = str(
            metrics.get("selected_agent") or planned_agent
        )
        if result is None:
            step_evidence.append(
                StepExecutionEvidence(
                    step_id=step.step_id,
                    agent_name=planned_agent,
                    planned_agent=planned_agent,
                    executed_agent="",
                    operation_mode=str(step.operation_mode),
                    risk_level=str(step.risk_level),
                    technical_success=False,
                    business_success=False,
                    verification_status=VerificationStatus.FAILED,
                    error="scheduler did not produce a step result",
                )
            )
            continue

        artifacts: list[Artifact] = []
        refs: list[dict[str, Any]] = []
        for raw_ref in _mapping(getattr(result, "outputs", None)).values():
            try:
                ref = raw_ref if isinstance(raw_ref, ArtifactRef) else ArtifactRef(**dict(raw_ref))
                refs.append(ref.model_dump(mode="json"))
                if artifact_store is not None:
                    artifacts.append(artifact_store.get(ref))
            except Exception:
                continue
        receipt_status = metrics.get("receipt_status")
        idem_key = metrics.get("idempotency_key")
        if receipt_store is not None and idem_key:
            try:
                receipt = receipt_store.get(str(idem_key)) or {}
                receipt_status = receipt.get("status") or receipt_status
            except Exception:
                pass
        item = build_step_evidence(
            step_id=step.step_id,
            agent_name=executed_agent,
            operation_mode=str(step.operation_mode),
            risk_level=str(step.risk_level),
            verification_contract=_mapping(
                getattr(step, "verification_contract", None)
            ),
            execute_result=None,
            artifact=artifacts[0] if artifacts else None,
            step_result=result,
            receipt_status=str(receipt_status) if receipt_status else None,
            idempotency_key=str(idem_key) if idem_key else None,
        )
        item.planned_agent = planned_agent
        item.executed_agent = executed_agent
        if refs:
            item.artifact_refs = refs
        if artifacts:
            schema_values = [artifact.schema_valid for artifact in artifacts]
            if any(value is False for value in schema_values):
                item.schema_valid = False
            elif all(value is True for value in schema_values):
                item.schema_valid = True
        step_evidence.append(item)

    terminal = workflow_status or getattr(results, "terminal_status", None)
    terminal_value = getattr(terminal, "value", terminal) or WorkflowStatus.FAILED.value
    return aggregate_evidence(
        task_id=task_id,
        workflow_id=workflow_id,
        execution_mode="scheduler",
        workflow_status=str(terminal_value),
        steps=step_evidence,
        task_graph=graph,
        planning_steps=planning_steps,
    )


__all__ = [
    "DistillationDecision",
    "SIDE_EFFECT_MODES",
    "SkillExecutionEvidence",
    "StepExecutionEvidence",
    "VerificationStatus",
    "aggregate_evidence",
    "build_legacy_evidence",
    "build_scheduler_evidence",
    "build_step_evidence",
    "evaluate_distillation_evidence",
    "load_execution_evidence",
]
