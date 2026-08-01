"""DAG-aware recovery planning and retry classification."""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from src.interface.task_graph import TaskGraph


class FailureCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    VALIDATION = "VALIDATION"
    PERMISSION = "PERMISSION"
    PERSISTENCE = "PERSISTENCE"
    RECONCILIATION = "RECONCILIATION"
    ROUTING = "ROUTING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    reason_code: str
    retryable: bool


@dataclass
class DAGRecoveryPlan:
    failed_steps: list[str]
    retry_steps: list[str]
    keep_steps: list[str]
    automatic: bool
    reason_code: str
    classifications: dict[str, dict[str, Any]] = field(default_factory=dict)
    compensation_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "超时",
)
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "too many requests",
    "限流",
)
_TRANSIENT_MARKERS = (
    "temporar",
    "connection reset",
    "connection aborted",
    "connection refused",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "network",
    "503",
    "502",
    "连接中断",
    "网络异常",
    "服务暂不可用",
)
_VALIDATION_MARKERS = (
    "validation",
    "invalid input",
    "schema",
    "completion condition",
    "could not convert",
    "missing required",
    "格式",
    "校验",
    "缺少必填",
)


def classify_failure(
    error: Optional[str],
    metrics: Optional[Mapping[str, Any]] = None,
    *,
    read_only: bool,
) -> FailureClassification:
    """Classify one failed step without trusting free-form Agent success text."""
    data = dict(metrics or {})
    text = str(error or "").strip().lower()

    if data.get("needs_reconciliation"):
        return FailureClassification(
            FailureCategory.RECONCILIATION,
            "SIDE_EFFECT_OUTCOME_UNCONFIRMED",
            False,
        )
    if data.get("approval_required") or data.get("permission_denied"):
        return FailureClassification(
            FailureCategory.PERMISSION,
            "POLICY_DECISION_REQUIRED",
            False,
        )
    if data.get("persistence_failed") or data.get("receipt_store_corrupt"):
        return FailureClassification(
            FailureCategory.PERSISTENCE,
            "DURABLE_STATE_UNAVAILABLE",
            False,
        )
    routing = str(data.get("routing_decision") or "").upper()
    if routing and routing != "DISPATCH":
        return FailureClassification(
            FailureCategory.ROUTING,
            f"ROUTING_{routing}",
            False,
        )
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return FailureClassification(
            FailureCategory.RATE_LIMITED,
            "RATE_LIMITED",
            read_only,
        )
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return FailureClassification(
            FailureCategory.TIMEOUT,
            "TEMPORARY_TIMEOUT",
            read_only,
        )
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return FailureClassification(
            FailureCategory.TRANSIENT,
            "TRANSIENT_EXTERNAL_FAILURE",
            read_only,
        )
    if any(marker in text for marker in _VALIDATION_MARKERS):
        return FailureClassification(
            FailureCategory.VALIDATION,
            "OUTPUT_OR_INPUT_VALIDATION_FAILED",
            bool(read_only or data.get("safe_to_retry")),
        )
    return FailureClassification(
        FailureCategory.UNKNOWN,
        "READ_FAILURE" if read_only else "UNCLASSIFIED_SIDE_EFFECT_FAILURE",
        read_only or bool(data.get("safe_to_retry")),
    )


def retry_delay_seconds(
    retry_index: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter_ratio: float,
    random_value: Optional[float] = None,
) -> float:
    """Exponential backoff with bounded symmetric jitter."""
    base = max(0.0, float(base_seconds))
    maximum = max(base, float(max_seconds))
    raw = min(maximum, base * (2 ** max(0, int(retry_index) - 1)))
    ratio = max(0.0, min(1.0, float(jitter_ratio)))
    sample = random.random() if random_value is None else float(random_value)
    jitter = raw * ratio * ((sample * 2.0) - 1.0)
    return max(0.0, min(maximum, raw + jitter))


def descendants(graph: TaskGraph, step_ids: set[str]) -> set[str]:
    affected = set(step_ids)
    changed = True
    while changed:
        changed = False
        for step in graph.steps:
            if step.step_id in affected:
                continue
            if any(dep in affected for dep in step.depends_on):
                affected.add(step.step_id)
                changed = True
    return affected


def build_dag_recovery_plan(
    graph: TaskGraph,
    results: Mapping[str, Any],
    completed_steps: set[str],
) -> DAGRecoveryPlan:
    """Preserve independent successes and isolate the failed DAG branch."""
    smap = graph.step_map()
    failed = [
        sid
        for sid, result in results.items()
        if str(getattr(getattr(result, "status", None), "value", getattr(result, "status", "")))
        != "SUCCEEDED"
    ]
    retry_set = descendants(graph, set(failed))
    order = graph.topological_order()
    retry_steps = [sid for sid in order if sid in retry_set]
    keep_steps = [
        sid for sid in order if sid in completed_steps and sid not in retry_set
    ]

    classifications: dict[str, dict[str, Any]] = {}
    compensation_actions: list[dict[str, Any]] = []
    automatic = bool(failed)
    reason_code = "DAG_BRANCH_SAFE_TO_RETRY"
    for sid in failed:
        result = results[sid]
        step = smap.get(sid)
        classification = classify_failure(
            getattr(result, "error", None),
            getattr(result, "metrics", None),
            read_only=bool(step and step.is_read_only),
        )
        classifications[sid] = {
            "category": classification.category.value,
            "reason_code": classification.reason_code,
            "retryable": classification.retryable,
        }
        if not classification.retryable:
            automatic = False
            reason_code = classification.reason_code

    # A previously completed side-effect node that is invalidated by the
    # rollback needs an explicit business compensation contract. Merely
    # deleting its local result would not undo the external operation.
    for sid in retry_steps:
        step = smap.get(sid)
        if (
            sid not in completed_steps
            or step is None
            or step.is_read_only
        ):
            continue
        contract = getattr(step, "compensation_action", None)
        compensation_actions.append(
            {
                "step_id": sid,
                "operation_mode": step.operation_mode,
                "risk_level": step.risk_level,
                "contract": dict(contract or {}),
                "status": "required",
            }
        )
        automatic = False
        reason_code = (
            "COMPENSATION_CONTRACT_REQUIRED"
            if not contract
            else "COMPENSATION_CONFIRMATION_REQUIRED"
        )

    return DAGRecoveryPlan(
        failed_steps=failed,
        retry_steps=retry_steps,
        keep_steps=keep_steps,
        automatic=automatic,
        reason_code=reason_code,
        classifications=classifications,
        compensation_actions=compensation_actions,
    )


def apply_dag_recovery_state(
    state: dict[str, Any],
    plan: DAGRecoveryPlan,
    *,
    attempt: int,
) -> None:
    """Invalidate only the failed branch; keep unrelated successful results."""
    retry = set(plan.retry_steps)
    state["completed_steps"] = [
        sid for sid in (state.get("completed_steps") or []) if sid not in retry
    ]
    step_results = dict(state.get("step_results") or {})
    for sid in retry:
        step_results.pop(sid, None)
    state["step_results"] = step_results
    evidence = dict(state.get("skill_step_evidence") or {})
    for sid in retry:
        evidence.pop(sid, None)
    state["skill_step_evidence"] = evidence
    state["__dag_recovery__"] = {
        **plan.to_dict(),
        "attempt": attempt,
    }
    state["__auto_recovery_attempted"] = True
