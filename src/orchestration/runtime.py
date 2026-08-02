"""Scheduler <-> workflow runtime bridge (Plan §8, Phase 3d — R1).

Adapts the :class:`TaskScheduler` to the existing ``_process_workflow`` runtime:
it produces the same SSE event shapes (``start_of_workflow`` / ``start_of_agent``
/ ``end_of_agent`` / ``end_of_workflow``), saves checkpoints, drives task logging
and hooks, and carries ``memory_session_id`` / ``memory_context`` (added on main).

Design notes
------------
- ``execute_step`` and ``routing_provider`` are **injectable** so this module is
  unit-testable with fakes; the real agent execution + routing are used only when
  they are not supplied.
- Heavy imports (agent_manager, executor factory, security, S-ABAC) are performed
  lazily inside the real ``execute_step`` so importing this module stays light.
- Concurrency: step lifecycle events are funneled through a single
  ``asyncio.Queue`` and drained in order by the async generator, mirroring
  ``process._execute_node_with_runtime_events``.
- DAG checkpoints record the set of completed ``step_id`` plus captured artifacts
  in ``state`` (not a linear step index).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

from src.interface.artifact import ArtifactRef, StepResult, StepStatus
from src.interface.task_graph import TaskGraph, WorkflowStatus
from src.orchestration.artifact_guard import PolicyEngineArtifactGuard
from src.orchestration.artifact_payload_store import (
    ArtifactPayloadCorruption,
    ArtifactPayloadStore,
)
from src.orchestration.completion import PersistentReceiptStore
from src.orchestration.failure_mapper import make_failure
from src.orchestration.governance import record_governance_event
from src.orchestration.reconciliation import get_reconciliation_store
from src.orchestration.recovery import (
    apply_dag_recovery_state,
    build_dag_recovery_plan,
)
from src.orchestration.plan_to_task_graph import plan_to_task_graph
from src.orchestration.providers import MainAgentRoutingProvider, RoutingProvider
from src.orchestration.resolver import ArtifactResolver
from src.orchestration.scheduler import TaskScheduler
from src.orchestration.store import ArtifactStore, ArtifactStoreCorruption
from src.skills.execution_evidence import (
    SkillExecutionEvidence,
    aggregate_evidence,
    build_scheduler_evidence,
)

logger = logging.getLogger(__name__)

ExecuteStep = Callable[..., Awaitable[Any]]


class TrustedSubtaskBindingError(ValueError):
    """The scheduler graph cannot be tied to the trusted TaskProfile."""


def _trusted_subtask_map(task_profile: Any) -> dict[str, dict[str, Any]]:
    """Return the trusted TaskProfile subtasks keyed by their stable run-local IDs."""

    if not isinstance(task_profile, dict):
        return {}
    raw_subtasks = task_profile.get("subtasks") or []
    if not raw_subtasks:
        return {}
    if not isinstance(raw_subtasks, list):
        raise TrustedSubtaskBindingError(
            "trusted TaskProfile subtasks must be a list"
        )

    subtasks: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_subtasks):
        if not isinstance(item, dict):
            raise TrustedSubtaskBindingError(
                f"trusted TaskProfile subtask at index {index} is invalid"
            )
        subtask_id = str(item.get("id") or "").strip()
        if not subtask_id:
            raise TrustedSubtaskBindingError(
                f"trusted TaskProfile subtask at index {index} has no id"
            )
        if subtask_id in subtasks:
            raise TrustedSubtaskBindingError(
                f"trusted TaskProfile contains duplicate subtask id {subtask_id!r}"
            )
        subtasks[subtask_id] = item
    return subtasks


def _step_subtask_ids(step: Any) -> list[str]:
    raw_ids = (
        getattr(step, "subtask_ids", None)
        or getattr(step, "subtask_id", None)
        or []
    )
    values = raw_ids if isinstance(raw_ids, (list, tuple, set)) else [raw_ids]
    return [str(item).strip() for item in values if str(item).strip()]


def _trusted_subtasks_for_step(
    task_profile: Any,
    step: Any,
) -> list[dict[str, Any]]:
    """Resolve one scheduler step to trusted subtasks, failing closed if needed."""

    subtasks = _trusted_subtask_map(task_profile)
    if not subtasks:
        return []

    step_id = str(getattr(step, "step_id", "") or "<unknown>")
    bound_ids = _step_subtask_ids(step)
    if not bound_ids:
        raise TrustedSubtaskBindingError(
            f"step {step_id!r} is missing trusted subtask_ids"
        )
    if len(bound_ids) != len(set(bound_ids)):
        raise TrustedSubtaskBindingError(
            f"step {step_id!r} contains duplicate subtask_ids"
        )

    unknown = [
        subtask_id
        for subtask_id in bound_ids
        if subtask_id not in subtasks
    ]
    if unknown:
        raise TrustedSubtaskBindingError(
            f"step {step_id!r} references unknown trusted subtasks {unknown}"
        )
    return [subtasks[subtask_id] for subtask_id in bound_ids]


def validate_trusted_subtask_bindings(
    graph: TaskGraph,
    task_profile: Any,
) -> TaskGraph:
    """Require exact TaskGraph coverage when the trusted profile has subtasks."""

    trusted_subtasks = _trusted_subtask_map(task_profile)
    if not trusted_subtasks:
        return graph

    coverage = {subtask_id: 0 for subtask_id in trusted_subtasks}
    for step in graph.steps:
        for subtask in _trusted_subtasks_for_step(task_profile, step):
            coverage[str(subtask["id"])] += 1

    missing = [
        subtask_id for subtask_id, count in coverage.items() if count == 0
    ]
    duplicated = [
        subtask_id for subtask_id, count in coverage.items() if count > 1
    ]
    if missing or duplicated:
        details: list[str] = []
        if missing:
            details.append(f"missing trusted subtasks {missing}")
        if duplicated:
            details.append(f"duplicate trusted subtasks {duplicated}")
        raise TrustedSubtaskBindingError("; ".join(details))
    return graph


def build_task_graph_from_state(state: dict) -> TaskGraph:
    """Resolve a :class:`TaskGraph` from state.

    Accepts an explicit ``state["task_graph"]`` (a ``TaskGraph`` or a dict) or
    falls back to converting ``state["planning_steps"]``.
    """
    tg = state.get("task_graph")
    if isinstance(tg, TaskGraph):
        graph = tg.validate_dag()
    elif isinstance(tg, dict):
        graph = TaskGraph(**tg).validate_dag()
    else:
        steps = state.get("planning_steps") or []
        task_id = state.get("task_id") or state.get("workflow_id") or "task"
        graph = plan_to_task_graph(
            steps,
            task_id=task_id,
            subject=state.get("user_id"),
        )
    return validate_trusted_subtask_bindings(
        graph,
        state.get("task_profile") or {},
    )


def has_task_graph(state: dict) -> bool:
    """True if state carries an explicit task graph (gates the scheduler path)."""
    return bool(state.get("task_graph"))


def _required_step_outputs(step: Any) -> list[str]:
    """Return outputs whose absence invalidates a resumed successful step."""

    contract = getattr(step, "agent_contract", None)
    if contract is not None:
        return [ref.name for ref in contract.produces if ref.required]
    return list(getattr(step, "expected_outputs", []) or [])


def _restore_outputs(state: dict, completed: set[str]) -> dict:
    """Rebuild ``{step_id: {param: ArtifactRef}}`` for completed steps on resume.

    Reads the serialized ``step_results`` persisted in a checkpoint and revives
    the ``ArtifactRef`` outputs so the scheduler can re-seed upstream data for
    resumed downstream steps.
    """
    step_results = state.get("step_results")
    if not isinstance(step_results, dict):
        return {}
    outputs: dict = {}
    for sid, result in step_results.items():
        if sid not in completed or not isinstance(result, dict):
            continue
        raw_outputs = result.get("outputs") or {}
        revived: dict = {}
        for param, ref in raw_outputs.items():
            if isinstance(ref, ArtifactRef):
                revived[param] = ref
            elif isinstance(ref, dict):
                try:
                    revived[param] = ArtifactRef(**ref)
                except Exception:  # noqa: BLE001 - skip malformed ref
                    continue
        if revived:
            outputs[sid] = revived
    return outputs


def _ref_unavailable(store: ArtifactStore, ref: ArtifactRef) -> bool:
    """Return whether a restored ref has no readable protected payload."""

    try:
        store.get(ref)
    except Exception:  # noqa: BLE001 - any missing/corrupt payload invalidates success
        return True
    return False


def _restore_completed_step_results(
    state: dict, completed: set[str]
) -> dict[str, StepResult]:
    """Restore only validated successful results for checkpointed steps."""

    raw_results = state.get("step_results")
    if not isinstance(raw_results, dict):
        return {}
    restored: dict[str, StepResult] = {}
    for step_id in completed:
        raw_result = raw_results.get(step_id)
        try:
            result = (
                raw_result
                if isinstance(raw_result, StepResult)
                else StepResult.model_validate(raw_result)
            )
        except Exception:
            continue
        if result.status == StepStatus.SUCCEEDED:
            restored[step_id] = result
    return restored


def _status_value(status: Any) -> str:
    raw = str(getattr(status, "value", status) or "")
    return raw.rsplit(".", 1)[-1].upper()


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


_PUBLIC_STEP_METRIC_KEYS = frozenset(
    {
        "attempts",
        "duration_ms",
        "elapsed_ms",
        "idempotent_reuse",
        "needs_reconciliation",
        "receipt_status",
        "redispatch_count",
        "redispatch_outcome",
        "recovery_path",
        "retry_count",
        "routing_decision",
    }
)
_CHECKPOINT_STEP_METRIC_KEYS = _PUBLIC_STEP_METRIC_KEYS | frozenset(
    {
        # Required to resume side-effect evidence and receipt verification.
        "external_op_id",
        "idempotency_key",
        # Legacy machine-readable compatibility fields. Raw diagnostics such as
        # result_error_details are deliberately excluded.
        "failure_code",
        "input_error",
        "persistence_failed",
        "receipt_store_corrupt",
        "result_error",
        "attempt_failures",
    }
)


def _safe_recovery_path(value: Any) -> list[str]:
    allowed = {"primary", "same_agent_retry", "redispatch"}
    if not isinstance(value, list):
        return []
    return [
        str(item)
        for item in value[:3]
        if isinstance(item, str) and item in allowed
    ]


def _safe_machine_token(value: Any, *, max_length: int = 64) -> Optional[str]:
    if not isinstance(value, str):
        return None
    token = value.strip().upper()
    if not token or not token.replace("_", "").isalnum():
        return None
    return token[:max_length]


def _safe_attempt_failures(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    safe: list[dict[str, Any]] = []
    for raw in value[:3]:
        if not isinstance(raw, dict):
            continue
        attempt = raw.get("attempt")
        phase = raw.get("phase")
        code = raw.get("code")
        retryable = raw.get("retryable")
        if (
            not isinstance(attempt, int)
            or not isinstance(phase, str)
            or phase not in {"primary", "redispatch"}
            or not isinstance(code, str)
            or not code.replace("_", "").isalnum()
            or not isinstance(retryable, bool)
        ):
            continue
        safe.append(
            {
                "attempt": attempt,
                "phase": phase,
                "code": code[:64],
                "retryable": retryable,
            }
        )
    return safe


def _public_step_metrics(metrics: Any) -> dict[str, Any]:
    """Return the small operational metric allow-list safe for SSE clients."""

    if not isinstance(metrics, dict):
        return {}
    public: dict[str, Any] = {}
    for key in _PUBLIC_STEP_METRIC_KEYS:
        value = metrics.get(key)
        if key == "recovery_path":
            path = _safe_recovery_path(value)
            if path:
                public[key] = path
            continue
        if key == "redispatch_outcome":
            outcome = _safe_machine_token(value)
            if outcome is not None:
                public[key] = outcome
            continue
        if value is None or not isinstance(value, (str, int, float, bool)):
            continue
        public[key] = value[:128] if isinstance(value, str) else value
    return public


def _checkpoint_step_result(result: StepResult) -> dict[str, Any]:
    """Serialize a step result without persisting raw provider diagnostics."""

    payload = result.model_dump(mode="json")
    failure = getattr(result, "failure", None)
    if result.status != StepStatus.SUCCEEDED:
        payload["error"] = (
            failure.message if failure is not None else "The workflow step failed."
        )
    metrics = result.metrics if isinstance(result.metrics, dict) else {}
    safe_metrics = {
        key: value[:256] if isinstance(value, str) else value
        for key in _CHECKPOINT_STEP_METRIC_KEYS
        for value in [metrics.get(key)]
        if key not in {"recovery_path", "redispatch_outcome", "attempt_failures"}
        if value is not None and isinstance(value, (str, int, float, bool))
    }
    recovery_path = _safe_recovery_path(metrics.get("recovery_path"))
    if recovery_path:
        safe_metrics["recovery_path"] = recovery_path
    redispatch_outcome = _safe_machine_token(metrics.get("redispatch_outcome"))
    if redispatch_outcome is not None:
        safe_metrics["redispatch_outcome"] = redispatch_outcome
    attempt_failures = _safe_attempt_failures(metrics.get("attempt_failures"))
    if attempt_failures:
        safe_metrics["attempt_failures"] = attempt_failures
    payload["metrics"] = safe_metrics
    return payload


def _leaf_step_ids(graph: TaskGraph) -> list[str]:
    dependencies = {
        dependency
        for step in graph.steps
        for dependency in (step.depends_on or [])
    }
    return [step.step_id for step in graph.steps if step.step_id not in dependencies]


def unknown_operation_modes(graph: TaskGraph) -> list[str]:
    """Return step ids whose ``operation_mode`` could not be classified.

    A step is scheduler-ready only when every step is a known read/write/send.
    An ``"unknown"`` mode means a potential side effect was not classifiable, so
    the runtime must refuse to schedule it (fail closed) rather than default to
    read and risk running a write as a parallel read-only step.
    """
    return [
        s.step_id
        for s in graph.steps
        if str(getattr(s, "operation_mode", "read")).lower() == "unknown"
    ]


def scheduler_ready(state: dict) -> tuple[bool, str, str]:
    """Classify whether ``state`` may enter the TaskGraph scheduler.

    Returns ``(ready, category, detail)`` where ``category`` is one of:

    - ``"ok"``       -> a valid, fully-classified graph; enter the scheduler.
    - ``"no_graph"`` -> no explicit task graph yet (planning phase may proceed
      to the Planner on the legacy path; the production execution phase must
      fail closed).
    - ``"invalid"``  -> the graph exists but fails structural validation.
    - ``"unknown"``  -> a step has an unclassified (``"unknown"``) operation
      mode, i.e. a potential side effect that must never run as read-only.

    ``invalid`` / ``unknown`` must always fail closed regardless of phase.
    """
    if not has_task_graph(state):
        return False, "no_graph", "no explicit task graph"
    try:
        graph = build_task_graph_from_state(state)
    except Exception as exc:  # noqa: BLE001 - invalid graph -> fail closed
        return False, "invalid", f"invalid task graph: {exc}"
    unknown = unknown_operation_modes(graph)
    if unknown:
        return False, "unknown", f"unclassified operation mode: {unknown}"
    return True, "ok", "ok"


async def _list_agents_and_authorized(state: dict) -> tuple[list, set]:
    """Best-effort gather agents + authorized ids for real routing (lazy imports)."""
    try:
        from src.manager import agent_manager
        from config.s_abac_demo_users import get_user_available_agents

        await agent_manager.ensure_initialized()
        agents = await agent_manager.agent_registry.list()
        available = get_user_available_agents(state.get("user_id")) or []
        if available == ["*"]:
            authorized = {getattr(a, "agent_name", "") for a in agents}
        else:
            authorized = set(available)
        return list(agents), authorized
    except Exception as exc:  # noqa: BLE001 - routing can still fall back to preferred
        logger.warning("scheduler: could not list agents for routing: %s", exc)
        return [], set()


def _build_step_task_profile(state: dict, step: Any, selected_agent: str) -> dict:
    """Scope the workflow profile to the step currently being dispatched.

    S-ABAC evaluates one target at a time. The global task profile is produced
    before planning, and the selected Agent's resource classification comes
    from the trusted platform registry. Planner-authored descriptions,
    capability/tag labels, task type and data scope are deliberately excluded
    from the authorization profile: they may make a plan stricter during
    conversion, but can never turn an authorization mismatch into a match.
    """
    from config.s_abac_config import RESOURCE_SECURITY_ATTRIBUTES

    global_profile = dict(state.get("task_profile") or {})
    trusted_attrs = dict(
        RESOURCE_SECURITY_ATTRIBUTES.get(selected_agent, {}) or {}
    )

    def _list_value(value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return [str(item) for item in values if str(item).strip()]

    def _ordered_values(items: Any, field: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            for value in _list_value(item.get(field)):
                key = value.strip().lower()
                if key and key not in seen:
                    result.append(value)
                    seen.add(key)
        return result

    trusted_subtasks = _trusted_subtasks_for_step(global_profile, step)

    if trusted_subtasks:
        required_capabilities = _ordered_values(
            trusted_subtasks, "expected_capabilities"
        )
        scenario_tags = _ordered_values(trusted_subtasks, "scenario_tags")
    else:
        required_capabilities = _list_value(
            trusted_attrs.get("expected_capabilities")
        ) or _list_value(global_profile.get("expected_capabilities"))
        scenario_tags = _list_value(
            trusted_attrs.get("scenario_tags")
        ) or _list_value(global_profile.get("scenario_tags"))

    trusted_fit_result: dict[str, Any] = {}
    if trusted_subtasks:
        resource_capabilities = {
            value.lower()
            for value in _list_value(trusted_attrs.get("expected_capabilities"))
        }
        resource_tags = {
            value.lower()
            for value in _list_value(trusted_attrs.get("scenario_tags"))
        }
        resource_task_type = str(
            trusted_attrs.get("capability_domain") or ""
        ).strip().lower()
        mismatch_reasons: list[str] = []
        if not trusted_attrs:
            mismatch_reasons.append(
                "selected agent has no trusted resource security attributes"
            )
        for subtask in trusted_subtasks:
            subtask_id = str(subtask.get("id") or "")
            expected = {
                value.lower()
                for value in _list_value(
                    subtask.get("expected_capabilities")
                )
            }
            tags = {
                value.lower()
                for value in _list_value(subtask.get("scenario_tags"))
            }
            subtask_task_type = str(
                subtask.get("task_type") or ""
            ).strip().lower()
            if expected and (
                not resource_capabilities
                or expected.isdisjoint(resource_capabilities)
            ):
                mismatch_reasons.append(
                    f"{subtask_id} capabilities do not match trusted resource"
                )
            if tags and (
                not resource_tags
                or tags.isdisjoint(resource_tags)
            ):
                mismatch_reasons.append(
                    f"{subtask_id} scenario tags do not match trusted resource"
                )
            if subtask_task_type and (
                not resource_task_type
                or subtask_task_type != resource_task_type
            ):
                mismatch_reasons.append(
                    f"{subtask_id} task type does not match trusted resource"
                )
        if mismatch_reasons:
            trusted_fit_result = {
                "fit": "mismatch",
                "confidence": 1.0,
                "reason": "; ".join(mismatch_reasons),
                "source": "trusted_task_profile_and_resource_registry",
            }
        else:
            trusted_fit_result = {
                "fit": "match",
                "confidence": 1.0,
                "reason": "Trusted subtask classification matches trusted resource",
                "source": "trusted_task_profile_and_resource_registry",
            }

    global_risk = str(
        global_profile.get("risk_profile")
        or global_profile.get("risk_level")
        or state.get("risk_profile")
        or "LOW"
    ).upper()
    step_risk = str(getattr(step, "risk_level", "") or global_risk).upper()
    risk_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    risk_profile = max(
        (global_risk, step_risk),
        key=lambda value: risk_order.get(value, risk_order["CRITICAL"]),
    )

    trusted_subtask_goals = _ordered_values(trusted_subtasks, "goal")
    business_goal = str(
        "; ".join(trusted_subtask_goals)
        or global_profile.get("business_goal")
        or state.get("business_goal")
        or state.get("original_user_query")
        or state.get("USER_QUERY")
        or ""
    )
    trusted_task_types = _ordered_values(trusted_subtasks, "task_type")
    trusted_data_scope = _ordered_values(trusted_subtasks, "data_scope")
    return {
        **global_profile,
        "business_goal": business_goal,
        "task_type": str(
            (trusted_task_types[0] if len(trusted_task_types) == 1 else "")
            or trusted_attrs.get("capability_domain")
            or global_profile.get("task_type")
            or "GENERAL"
        ).upper(),
        "expected_capabilities": required_capabilities,
        "scenario_tags": scenario_tags,
        "operation_mode": str(
            getattr(step, "operation_mode", "") or "read"
        ).lower(),
        "data_scope": str(
            ",".join(trusted_data_scope)
            or global_profile.get("data_scope")
            or state.get("data_scope")
            or "task"
        ),
        "risk_profile": risk_profile,
        "profile_scope": "step",
        "step_id": str(getattr(step, "step_id", "")),
        "authorization_profile_sources": [
            "global_task_profile",
            "trusted_resource_registry",
        ],
        "trusted_resource_fit": trusted_fit_result,
    }


def _build_execution_context(state: dict, step, selected_agent):
    """Build a per-step ExecutionContext carrying acting user + producer agent.

    Isolated per step (never shared across concurrent steps) so captured
    artifacts get correct owner/producer/provenance metadata.
    """
    from src.manager.executor.base import ExecutionContext

    task_profile = _build_step_task_profile(state, step, selected_agent)
    scenario_fit_cache = dict(state.get("scenario_fit_cache") or {})
    trusted_resource_fit = dict(
        task_profile.get("trusted_resource_fit") or {}
    )
    if trusted_resource_fit:
        # Enforcement looks up ``<object_type>:<object_id>`` before invoking
        # the optional fit analyzer. This trusted entry prevents Planner text
        # from replacing the deterministic subtask/resource compatibility
        # decision during Agent dispatch.
        scenario_fit_cache[f"agent:{selected_agent}"] = trusted_resource_fit
    return ExecutionContext(
        user_id=state.get("user_id"),
        workflow_id=state.get("workflow_id"),
        workflow_mode=state.get("workflow_mode"),
        deep_thinking_mode=state.get("deep_thinking_mode", False),
        metadata={
            "task_id": state.get("task_id"),
            "node_name": "scheduler",
            "step_id": step.step_id,
            "operation_mode": step.operation_mode,
            "producer_agent_id": selected_agent,
            "selected_agent": selected_agent,
            "risk_profile": task_profile["risk_profile"],
            "task_profile": task_profile,
            "scenario_tags": task_profile["scenario_tags"],
            "expected_capabilities": task_profile["expected_capabilities"],
            "scenario_fit_cache": scenario_fit_cache,
            "original_user_query": state.get("original_user_query")
            or state.get("USER_QUERY")
            or "",
        },
    )


def _make_context_factory(state: dict):
    """Return a ``factory(step, selected_agent) -> ExecutionContext`` bound to state."""

    def _factory(step, selected_agent):
        return _build_execution_context(state, step, selected_agent)

    return _factory


def _make_real_execute_step(state: dict) -> ExecuteStep:
    """Build the production ``execute_step`` mirroring ``agent_proxy_node``."""

    async def _execute_step(*, step, selected_agent, inputs, context) -> Any:
        from src.manager import agent_manager
        from src.manager.executor.base import ExecuteResult, ExecutionStatus
        from src.manager.executor.factory import execute_agent
        from src.security.enforcement import enforce_agent_dispatch

        if not selected_agent:
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error=f"no agent selected for step {step.step_id}",
            )

        await agent_manager.ensure_initialized()
        agent = await agent_manager.agent_registry.get(selected_agent)
        if agent is None:
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error=f"agent not found in registry: {selected_agent}",
            )

        # Reuse the per-step ExecutionContext built by the injected factory so
        # the same context drives dispatch enforcement and artifact capture.
        exec_ctx = context.get("execution_context") if isinstance(
            context, dict) else None
        if exec_ctx is None:
            exec_ctx = _build_execution_context(state, step, selected_agent)
        if not (
            isinstance(context, dict)
            and context.get("agent_dispatch_authorized")
        ):
            await enforce_agent_dispatch(agent, exec_ctx)

        brief = {
            "original_user_query": state.get("original_user_query")
            or state.get("USER_QUERY")
            or "",
            "assigned_agent": selected_agent,
            "assigned_steps": [
                {
                    "step_id": step.step_id,
                    "title": getattr(step, "title", ""),
                    "description": getattr(step, "description", ""),
                    "intents": list(getattr(step, "intents", []) or []),
                }
            ],
            "task_profile": state.get("task_profile") or {},
            # Surfaced so an idempotency-aware tool/provider can dedupe an
            # external side effect (e.g. a message id / request key).
            "idempotency_key": (context.get("idempotency_key") if isinstance(context, dict) else None),
            "step": {
                "step_id": step.step_id,
                "title": getattr(step, "title", ""),
                "description": getattr(step, "description", ""),
            },
            "resolved_inputs": inputs,
            "instruction": (
                "Complete only this step using the resolved inputs and the "
                "original user query. Do not inspect unrelated local files."
            ),
        }
        messages = list(state.get("messages", [])) + [
            {
                "role": "user",
                "content": "EXECUTION_CONTEXT\n"
                + json.dumps(brief, ensure_ascii=False, default=str),
            }
        ]
        return await execute_agent(agent, messages, exec_ctx)

    return _execute_step


def _make_real_authorize_step(state: dict):
    """Build the dispatch authorization hook used before receipt claiming."""

    async def _authorize_step(*, step, selected_agent, context) -> Any:
        from src.manager import agent_manager
        from src.security.enforcement import enforce_agent_dispatch, enforce_tool_call
        from src.security.remote_tool_gate import required_remote_tool_authorizations

        if not selected_agent:
            raise ValueError(f"no agent selected for step {step.step_id}")
        await agent_manager.ensure_initialized()
        agent = await agent_manager.agent_registry.get(selected_agent)
        if agent is None:
            raise ValueError(f"agent not found in registry: {selected_agent}")
        exec_ctx = context.get("execution_context") if isinstance(
            context, dict
        ) else None
        if exec_ctx is None:
            exec_ctx = _build_execution_context(state, step, selected_agent)
            if isinstance(context, dict):
                context["execution_context"] = exec_ctx
        dispatch_result = await enforce_agent_dispatch(agent, exec_ctx)

        # A remote Agent may own several internal tools with different security
        # levels.  Dispatch permission alone must not authorize all of them.
        # Resolve concrete resources from the server-owned TaskGraph step and
        # enforce each one before the scheduler claims an execution receipt.
        tool_authorizations = required_remote_tool_authorizations(
            agent_name=selected_agent,
            intents=list(getattr(step, "intents", []) or []),
            task_profile=state.get("task_profile") or {},
            operation_mode=str(getattr(step, "operation_mode", "read")),
        )
        authorized_manifest = []
        for authorization in tool_authorizations:
            result = await enforce_tool_call(
                agent=agent,
                tool_name=authorization.tool_name,
                arguments=authorization.arguments,
                context=exec_ctx,
            )
            authorized_manifest.append(
                {
                    "tool_name": authorization.tool_name,
                    "arguments": authorization.arguments,
                    "decision": result.get("decision", "ALLOW"),
                }
            )

        # Empty is a meaningful deny-all manifest. Always propagate it so an
        # unmapped or missing intent cannot silently disable the remote gate.
        exec_ctx.metadata["authorized_remote_tools"] = authorized_manifest
        if isinstance(context, dict):
            context["authorized_remote_tools"] = authorized_manifest
        return dispatch_result

    return _authorize_step


async def run_scheduler_workflow(
    state: dict,
    *,
    task_id: str,
    checkpoint_manager: Any = None,
    task_logger: Any = None,
    hook_engine: Any = None,
    execute_step: Optional[ExecuteStep] = None,
    authorize_step: Optional[Callable[..., Awaitable[Any]]] = None,
    routing_provider: Optional[RoutingProvider] = None,
    redispatch_enabled: Optional[bool] = None,
    retry_delay_seconds: Optional[float] = None,
    trusted_agents: Optional[list[Any]] = None,
    authorized_agent_ids: Optional[set[str]] = None,
) -> AsyncGenerator[dict, None]:
    """Drive the scheduler over the state's TaskGraph, yielding workflow events.

    Mirrors the legacy event stream so the frontend/consumers are unaffected.
    """
    # ``task_id`` is a scheduler-owned runtime fact.  Persist it into state
    # before building per-step contexts so approval lookup on resume can find
    # and atomically consume the decision created for this exact task.
    state["task_id"] = task_id
    workflow_id = state.get("workflow_id")
    graph = build_task_graph_from_state(state)
    def persist_skill_evidence(evidence: SkillExecutionEvidence) -> None:
        payload = evidence.model_dump(mode="json")
        state["skill_execution_evidence"] = payload
        state["business_success"] = evidence.business_success
        if task_logger is not None and hasattr(
            task_logger, "set_skill_execution_evidence"
        ):
            task_logger.set_skill_execution_evidence(payload)

    task_log_finalized = False

    def finalize_task_log(status: Any, error: Optional[str] = None) -> None:
        """Close the TaskLogger exactly once for every scheduler terminal path."""
        nonlocal task_log_finalized
        if task_logger is None or task_log_finalized:
            return

        status_value = _status_value(status)
        try:
            terminal_logger = getattr(task_logger, "log_workflow_terminal", None)
            if callable(terminal_logger):
                terminal_logger(status_value, error=error)
            elif status_value == WorkflowStatus.SUCCEEDED.value:
                task_logger.log_workflow_end()
            else:
                task_logger.log_error(
                    error=error or f"scheduler workflow ended with status {status_value}",
                    node_name="scheduler",
                )
        except Exception as exc:  # noqa: BLE001 - logging must not change execution
            logger.warning("scheduler: could not finalize task log: %s", exc)
        finally:
            task_log_finalized = True

    yield {
        "event": "start_of_workflow",
        "data": {"workflow_id": workflow_id, "task_id": task_id, "mode": "scheduler"},
    }
    record_governance_event(
        "WORKFLOW_STARTED",
        task_id=task_id,
        workflow_id=str(workflow_id or ""),
        subject=state.get("user_id"),
        decision="STARTED",
        details={"mode": "scheduler"},
    )

    # Scenario used by the artifact guard to evaluate S-ABAC scenario fit.
    scenario_ctx = {
        "scenario_tags": state.get("scenario_tags", []),
        "expected_capabilities": state.get("expected_capabilities", []),
        "task_type": (state.get("task_profile", {}) or {}).get("task_type", "GENERAL"),
        "risk_profile": state.get("risk_profile", "LOW"),
        "scenario_fit_result": state.get("scenario_fit_result", {}),
    }

    store = ArtifactStore()
    # Dedicated protected payload store: full (possibly sensitive) artifact
    # payloads live here, NOT in the generic checkpoint. The checkpoint keeps
    # only a de-sensitized index (refs + checksum + logical name/sensitivity).
    payload_store = ArtifactPayloadStore(task_id)
    # Retention: prune sibling payload stores older than the configured TTL so
    # sensitive payloads do not linger indefinitely on disk. Best-effort -- a
    # cleanup failure must never block or fail a run.
    try:
        ttl = float(os.getenv("ARTIFACT_PAYLOAD_TTL_SECONDS",
                    str(7 * 24 * 3600)))
        payload_store.cleanup_expired(ttl_seconds=ttl)
    except Exception as exc:  # noqa: BLE001 - retention is best-effort
        logger.debug("scheduler: payload retention cleanup skipped: %s", exc)
    # Resume: rebuild the artifact payloads produced by already-completed steps
    # from the protected payload store using the checkpoint's index. Any
    # integrity failure (missing/tampered payload) fails closed with a terminal
    # event -- never silently continues with partial data.
    restored_index = state.get("artifacts")
    if isinstance(restored_index, dict) and restored_index:
        try:
            payloads = payload_store.load_index(restored_index)
            store.load_state(payloads)
        except (ArtifactStoreCorruption, ArtifactPayloadCorruption) as exc:
            logger.error(
                "scheduler: corrupt/missing restored artifacts: %s", exc)
            evidence = aggregate_evidence(
                task_id=task_id,
                workflow_id=str(workflow_id or ""),
                execution_mode="scheduler",
                workflow_status=WorkflowStatus.FAILED.value,
                steps=[],
                task_graph=graph,
                planning_steps=state.get("planning_steps") or [],
            )
            persist_skill_evidence(evidence)
            failure = make_failure(
                "ARTIFACT_STORE_CORRUPTION",
                message="Saved workflow artifacts are missing or corrupted.",
                action="Restart from a safe checkpoint or run the workflow again.",
            )
            if task_logger is not None:
                if hasattr(task_logger, "log_failure"):
                    task_logger.log_failure(failure.model_dump(mode="json"))
            finalize_task_log(WorkflowStatus.FAILED, error=failure.message)
            yield {
                "event": "end_of_workflow",
                "data": {
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "mode": "scheduler",
                    "status": WorkflowStatus.FAILED.value,
                    "error": failure.message,
                    "reason": "artifact_store_corruption",
                    "failures": [failure.model_dump(mode="json")],
                    "failed_steps": [],
                    "blocked_steps": [],
                    "skill_execution_evidence": evidence.model_dump(mode="json"),
                },
            }
            return
    resolver = ArtifactResolver(
        store, guard=PolicyEngineArtifactGuard(scenario=scenario_ctx))

    if redispatch_enabled is None or retry_delay_seconds is None:
        from src.service.env import (
            SCHEDULER_REDISPATCH_ENABLED,
            SCHEDULER_RETRY_DELAY_SECONDS,
        )

        if redispatch_enabled is None:
            redispatch_enabled = SCHEDULER_REDISPATCH_ENABLED
        if retry_delay_seconds is None:
            retry_delay_seconds = SCHEDULER_RETRY_DELAY_SECONDS

    routing = routing_provider or MainAgentRoutingProvider()
    agents: list[Any]
    authorized: set[str]
    if trusted_agents is not None:
        agents = list(trusted_agents)
        trusted_names = {
            str(getattr(agent, "agent_name", "") or "")
            for agent in agents
            if str(getattr(agent, "agent_name", "") or "")
        }
        if authorized_agent_ids is None:
            from config.s_abac_demo_users import get_user_available_agents

            available = set(
                get_user_available_agents(state.get("user_id")) or []
            )
            authorized = (
                trusted_names if "*" in available else trusted_names & available
            )
        else:
            authorized = trusted_names & {
                str(agent_id) for agent_id in authorized_agent_ids
            }
    elif routing_provider is None or bool(redispatch_enabled):
        # A custom router may choose the candidate, but it never supplies the
        # trust root. Redispatch still loads contracts and authorization from
        # the real registry unless the caller injected an explicit trusted set.
        agents, authorized = await _list_agents_and_authorized(state)
        if authorized_agent_ids is not None:
            authorized &= {
                str(agent_id) for agent_id in authorized_agent_ids
            }
    else:
        agents, authorized = [], set()

    execute = execute_step or _make_real_execute_step(state)
    authorize = authorize_step
    if authorize is None and execute_step is None:
        authorize = _make_real_authorize_step(state)

    event_queue: asyncio.Queue[dict] = asyncio.Queue()
    counter = {"step": int(state.get("current_step") or 0)}
    step_numbers: dict[str, int] = {}
    step_agents: dict[str, str] = {}

    def step_number(step_id: str) -> int:
        if step_id not in step_numbers:
            step_numbers[step_id] = counter["step"]
            counter["step"] += 1
        return step_numbers[step_id]

    async def trigger_scheduler_hook(
        hook_point_value: str,
        *,
        step: Any = None,
        error: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> Any:
        if hook_engine is None:
            return None
        try:
            from src.robust.hooks.base import HookContext, HookPoint

            hook_point = HookPoint(hook_point_value)
            hook_state = dict(state)
            hook_state["__scheduler_hook_details__"] = dict(details or {})
            context = HookContext(
                task_id=task_id,
                workflow_id=str(workflow_id or ""),
                current_node=(
                    getattr(step, "step_id", None) if step is not None else "scheduler"
                ),
                current_step=(
                    step_number(step.step_id) if step is not None else counter["step"]
                ),
                state=hook_state,
                history=list(getattr(task_logger, "history", []) or []),
                error_message=error,
                hook_point=hook_point,
                workflow_status="failed" if error else "running",
                user_query=str(
                    state.get("USER_QUERY")
                    or state.get("original_user_query")
                    or ""
                ),
            )
            return await hook_engine.process(context)
        except Exception as exc:  # noqa: BLE001 - observability never fails a step
            logger.warning(
                "scheduler hook %s failed: %s", hook_point_value, exc
            )
            return None

    async def on_step_start(*, step, selected_agent, inputs):
        selected_name = selected_agent or getattr(step, "agent_name", None) or step.step_id
        step_agents[step.step_id] = selected_name
        # Read-only attempts have their own lifecycle below so retries and
        # redispatches cannot be misattributed to the planned Agent.
        if step.is_read_only:
            return
        current_step = step_number(step.step_id)
        if task_logger is not None:
            try:
                task_logger.log_agent_start(
                    node_name="scheduler", step=current_step, sub_agent_name=selected_name
                )
            except Exception:  # noqa: BLE001
                pass
        await event_queue.put(
            {
                "event": "start_of_agent",
                "data": {
                    "step_id": step.step_id,
                    "agent_name": f"scheduler【{selected_name}】",
                    "agent_id": f"{workflow_id}_{step.step_id}",
                    "sub_agent_name": selected_name,
                },
            }
        )
        record_governance_event(
            "STEP_STARTED",
            task_id=task_id,
            workflow_id=str(workflow_id or ""),
            step_id=step.step_id,
            subject=state.get("user_id"),
            agent=selected_name,
            operation_mode=str(getattr(step, "operation_mode", "") or ""),
            risk_level=str(getattr(step, "risk_level", "") or ""),
            decision="RUNNING",
            details={"step_number": current_step},
        )
        await trigger_scheduler_hook(
            "step_start",
            step=step,
            details={
                "agent": selected_name,
                "inputs_present": sorted(str(key) for key in inputs),
            },
        )

    async def on_retry(
        *,
        step,
        selected_agent,
        attempt,
        next_attempt,
        max_attempts,
        delay_seconds,
        error,
        reason_code,
    ):
        retry_data = {
            "step_id": step.step_id,
            "agent_name": selected_agent,
            "attempt": attempt,
            "next_attempt": next_attempt,
            "max_attempts": max_attempts,
            "next_delay_seconds": round(float(delay_seconds), 3),
            "error": error,
            "reason_code": reason_code,
        }
        record_governance_event(
            "RETRY_SCHEDULED",
            task_id=task_id,
            workflow_id=str(workflow_id or ""),
            step_id=step.step_id,
            subject=state.get("user_id"),
            agent=selected_agent,
            operation_mode=str(getattr(step, "operation_mode", "") or ""),
            risk_level=str(getattr(step, "risk_level", "") or ""),
            decision="RETRY",
            reason_code=reason_code,
            details=retry_data,
        )
        await event_queue.put({"event": "retry_scheduled", "data": retry_data})

    async def on_attempt_start(
        *,
        step,
        selected_agent,
        inputs,
        attempt,
        phase,
    ):
        planned_name = (
            getattr(step, "agent_name", None)
            or getattr(step, "preferred_resource_id", None)
            or step.step_id
        )
        executed_name = selected_agent or planned_name
        current_step = step_number(step.step_id)
        if task_logger is not None:
            try:
                task_logger.log_agent_start(
                    node_name="scheduler",
                    step=current_step,
                    sub_agent_name=executed_name,
                    attempt=attempt,
                    phase=phase,
                    planned_agent=planned_name,
                    executed_agent=executed_name,
                )
            except Exception:  # noqa: BLE001
                pass
        await event_queue.put(
            {
                "event": "start_of_agent",
                "data": {
                    "step_id": step.step_id,
                    "agent_name": f"scheduler【{executed_name}】",
                    "agent_id": (
                        f"{workflow_id}_{step.step_id}_{phase}_{attempt}"
                    ),
                    "sub_agent_name": executed_name,
                    "selected_agent": executed_name,
                    "planned_agent": planned_name,
                    "executed_agent": executed_name,
                    "attempt": attempt,
                    "phase": phase,
                },
            }
        )
        await trigger_scheduler_hook(
            "step_start",
            step=step,
            details={
                "agent": executed_name,
                "inputs_present": sorted(str(key) for key in inputs),
                "attempt": attempt,
                "phase": phase,
            },
        )

    async def on_attempt_end(
        *,
        step,
        selected_agent,
        result,
        attempt,
        phase,
    ):
        planned_name = (
            getattr(step, "agent_name", None)
            or getattr(step, "preferred_resource_id", None)
            or step.step_id
        )
        executed_name = selected_agent or planned_name
        status_value = _status_value(result.status)
        failure = getattr(result, "failure", None)
        if task_logger is not None:
            try:
                task_logger.log_agent_end(
                    node_name="scheduler",
                    next_node="scheduler",
                    step=step_number(step.step_id),
                    sub_agent_name=executed_name,
                    attempt=attempt,
                    phase=phase,
                    planned_agent=planned_name,
                    executed_agent=executed_name,
                )
            except Exception:  # noqa: BLE001
                pass
        await event_queue.put(
            {
                "event": "end_of_agent",
                "data": {
                    "step_id": step.step_id,
                    "agent_name": f"scheduler【{executed_name}】",
                    "agent_id": (
                        f"{workflow_id}_{step.step_id}_{phase}_{attempt}"
                    ),
                    "sub_agent_name": executed_name,
                    "selected_agent": executed_name,
                    "planned_agent": planned_name,
                    "executed_agent": executed_name,
                    "attempt": attempt,
                    "phase": phase,
                    "status": status_value,
                    "failure": (
                        failure.model_dump(mode="json")
                        if failure is not None
                        else None
                    ),
                },
            }
        )

    async def commit_step_result(*, step, result):
        """CRITICAL durable persistence for a completed step (crash-safe order).

        The checkpoint must record the completion ATOMICALLY with it becoming
        durable, otherwise a crash right after a step succeeds would restore a
        checkpoint that omits the step and re-schedule it on resume (a re-run;
        side effects are receipt-protected, but read-only queries would repeat).

        Order:
          1. write the Artifact payload;
          2. build a CANDIDATE state that already includes this step in
             ``completed_steps`` (+ updated ``step_results``/``artifacts``);
          3. save the checkpoint from the CANDIDATE state;
          4. only after the checkpoint succeeds, promote the candidate values
             into the live in-memory ``state``.
        On any failure the live ``state`` is left unchanged and the exception
        propagates so the scheduler marks the step FAILED (never SUCCEEDED).
        """
        succeeded = result.status == StepStatus.SUCCEEDED

        # Allocate the step number BEFORE building the candidate. Synthetic
        # results (clarify/blocked steps) never pass through ``on_step_start``,
        # so allocating lazily below would persist a candidate whose
        # ``current_step`` still equals this checkpoint's own step number --
        # after a resume the next step would reuse that number and overwrite
        # the very checkpoint used for recovery.
        current = step_number(step.step_id)

        # (1) Candidate step_results (do not mutate live state yet).
        step_results = dict(state.get("step_results") or {})
        step_results[step.step_id] = _checkpoint_step_result(result)

        # (1) Persist artifact payloads to the PROTECTED payload store. Only a
        # de-sensitized index (refs + checksum) is carried in the checkpoint.
        artifacts_index = state.get("artifacts")
        artifacts_updated = False
        if succeeded and result.outputs:
            artifacts_index = payload_store.save_store_state(
                store.dump_state())
            artifacts_updated = True

        # (2) Candidate completion set INCLUDING this step, so a checkpoint
        # restored after a crash skips it (never re-schedules a done step).
        completed = list(state.get("completed_steps") or [])
        if succeeded and step.step_id not in completed:
            completed.append(step.step_id)

        candidate = dict(state)
        candidate["step_results"] = step_results
        if artifacts_updated:
            candidate["artifacts"] = artifacts_index
        candidate["completed_steps"] = completed
        candidate["current_step"] = counter["step"]

        # (3) Save the checkpoint FROM THE CANDIDATE (completion already applied).
        # A failure here propagates -> the step is not reported SUCCEEDED and the
        # live state is left untouched.
        if checkpoint_manager is not None:
            checkpoint_manager.save_checkpoint(
                workflow_id=workflow_id,
                task_id=task_id,
                step=current,
                node_name="scheduler",
                next_node="scheduler",
                state=candidate,
            )
            record_governance_event(
                "CHECKPOINT_SAVED",
                task_id=task_id,
                workflow_id=str(workflow_id or ""),
                step_id=step.step_id,
                subject=state.get("user_id"),
                agent=getattr(step, "agent_name", None),
                operation_mode=str(getattr(step, "operation_mode", "") or ""),
                risk_level=str(getattr(step, "risk_level", "") or ""),
                decision="SAVED",
                details={
                    "checkpoint_step": step_number(step.step_id),
                    "step_succeeded": succeeded,
                },
            )
            result.metrics["checkpoint_step"] = step_number(step.step_id)

        # (4) Durable write succeeded -> promote candidate values into live state.
        state["step_results"] = step_results
        if artifacts_updated:
            state["artifacts"] = artifacts_index
        state["completed_steps"] = completed
        state["current_step"] = counter["step"]

    async def on_step_end(*, step, result):
        # Non-critical hooks: logging + SSE event. Best effort (the scheduler
        # swallows exceptions here so a monitoring failure never fails a step).
        if task_logger is not None:
            try:
                if not step.is_read_only:
                    task_logger.log_agent_end(
                        node_name="scheduler",
                        next_node="scheduler",
                        step=step_number(step.step_id),
                        sub_agent_name=(
                            (result.metrics or {}).get("selected_agent")
                            or step_agents.get(step.step_id)
                            or getattr(step, "agent_name", None)
                        ),
                    )
                failure = getattr(result, "failure", None)
                if failure is not None and hasattr(task_logger, "log_failure"):
                    task_logger.log_failure(
                        failure.model_dump(mode="json"),
                        node_name="scheduler",
                        step=step_number(step.step_id),
                    )
            except Exception:  # noqa: BLE001
                pass

        status_value = _status_value(result.status)
        selected_name = (
            (result.metrics or {}).get("selected_agent")
            or step_agents.get(step.step_id)
            or getattr(step, "agent_name", None)
            or step.step_id
        )
        planned_name = (
            getattr(step, "agent_name", None)
            or getattr(step, "preferred_resource_id", None)
            or step.step_id
        )
        metrics = dict(result.metrics or {})

        if metrics.get("approval_required"):
            payload = metrics.get("approval_payload")
            payload = payload if isinstance(payload, dict) else {}
            try:
                from src.security.approval import get_approval_store

                approval = get_approval_store().create(
                    user_id=str(state.get("user_id") or ""),
                    workflow_id=str(workflow_id or ""),
                    task_id=task_id,
                    # The failed step is checkpointed but not completed. Loading
                    # this checkpoint re-runs the same DAG node and keeps all
                    # independent successful branches.
                    resume_step=step_number(step.step_id) + 1,
                    node_name=selected_name,
                    step_id=step.step_id,
                    subject=dict(payload.get("subject") or {}),
                    object=dict(payload.get("object") or {}),
                    scenario=dict(payload.get("scenario") or {}),
                    action=dict(payload.get("action") or {}),
                    policy_result=dict(payload.get("policy_result") or {}),
                )
                approval_data = {
                    "approval_id": approval.approval_id,
                    "status": approval.status,
                    "task_id": approval.task_id,
                    "workflow_id": approval.workflow_id,
                    "step_id": approval.step_id,
                    "resume_step": approval.resume_step,
                    "reason": approval.policy_result.get("reason"),
                    "needs_reconciliation": bool(
                        metrics.get("approval_after_side_effect_start")
                    ),
                }
                metrics["approval_request"] = approval_data
                await event_queue.put(
                    {"event": "approval_required", "data": approval_data}
                )
                record_governance_event(
                    "APPROVAL_REQUIRED",
                    task_id=task_id,
                    workflow_id=str(workflow_id or ""),
                    step_id=step.step_id,
                    subject=state.get("user_id"),
                    agent=selected_name,
                    operation_mode=str(
                        getattr(step, "operation_mode", "") or ""
                    ),
                    risk_level=str(getattr(step, "risk_level", "") or ""),
                    decision="REVIEW_REQUIRED",
                    reason_code="POLICY_REVIEW_REQUIRED",
                    details=approval_data,
                )
            except Exception as exc:  # noqa: BLE001 - keep original step failure
                metrics["approval_store_error"] = str(exc)

        if metrics.get("permission_denied"):
            payload = metrics.get("permission_payload")
            payload = payload if isinstance(payload, dict) else {}
            permission_data = {
                **payload,
                "workflow_id": workflow_id,
                "task_id": task_id,
                "step_id": step.step_id,
                "error": result.error,
            }
            await event_queue.put(
                {"event": "permission_denied", "data": permission_data}
            )
            record_governance_event(
                "PERMISSION_DENIED",
                task_id=task_id,
                workflow_id=str(workflow_id or ""),
                step_id=step.step_id,
                subject=state.get("user_id"),
                agent=selected_name,
                operation_mode=str(getattr(step, "operation_mode", "") or ""),
                risk_level=str(getattr(step, "risk_level", "") or ""),
                decision="DENY",
                reason_code="S_ABAC_DENIED",
                details={
                    "reason": result.error,
                    "policy_result": payload.get("policy_result") or {},
                },
            )

        step_succeeded = status_value == StepStatus.SUCCEEDED.value
        record_governance_event(
            "STEP_SUCCEEDED" if step_succeeded else "STEP_FAILED",
            task_id=task_id,
            workflow_id=str(workflow_id or ""),
            step_id=step.step_id,
            subject=state.get("user_id"),
            agent=selected_name,
            operation_mode=str(getattr(step, "operation_mode", "") or ""),
            risk_level=str(getattr(step, "risk_level", "") or ""),
            decision=status_value,
            reason_code=(
                "RECONCILIATION_REQUIRED"
                if metrics.get("needs_reconciliation")
                else "STEP_EXECUTION_FAILED"
                if not step_succeeded
                else None
            ),
            details={"error": result.error, "metrics": _json_safe(metrics)},
        )
        if metrics.get("needs_reconciliation"):
            reconciliation_data: dict[str, Any] = {}
            idempotency_key = str(metrics.get("idempotency_key") or "")
            receipt = dict(metrics.get("receipt") or {})
            if idempotency_key and not receipt:
                receipt = dict(receipt_store.get(idempotency_key) or {})
            try:
                reconciliation = get_reconciliation_store().create(
                    user_id=str(state.get("user_id") or ""),
                    workflow_id=str(workflow_id or ""),
                    task_id=task_id,
                    step_id=step.step_id,
                    resume_step=step_number(step.step_id) + 1,
                    agent_name=selected_name,
                    error=str(result.error or "side-effect outcome unconfirmed"),
                    idempotency_key=idempotency_key,
                    claim_id=str(receipt.get("claim_id") or ""),
                    external_operation_id=str(
                        metrics.get("external_op_id")
                        or receipt.get("external_op_id")
                        or ""
                    ),
                    receipt=receipt,
                )
                reconciliation_data = {
                    "reconciliation_id": reconciliation.reconciliation_id,
                    "status": reconciliation.status,
                    "task_id": reconciliation.task_id,
                    "workflow_id": reconciliation.workflow_id,
                    "step_id": reconciliation.step_id,
                    "resume_step": reconciliation.resume_step,
                    "agent_name": reconciliation.agent_name,
                    "error": reconciliation.error,
                    "idempotency_key": reconciliation.idempotency_key,
                    "external_operation_id": reconciliation.external_operation_id,
                }
                metrics["reconciliation_request"] = reconciliation_data
                result.metrics = metrics
                await event_queue.put(
                    {"event": "reconciliation_required", "data": reconciliation_data}
                )
            except Exception as exc:  # noqa: BLE001 - preserve task verdict
                metrics["reconciliation_store_error"] = str(exc)
            record_governance_event(
                "RECONCILIATION_REQUIRED",
                task_id=task_id,
                workflow_id=str(workflow_id or ""),
                step_id=step.step_id,
                subject=state.get("user_id"),
                agent=selected_name,
                operation_mode=str(getattr(step, "operation_mode", "") or ""),
                risk_level=str(getattr(step, "risk_level", "") or ""),
                decision="MANUAL_REVIEW",
                reason_code="SIDE_EFFECT_OUTCOME_UNCONFIRMED",
                details={
                    "error": result.error,
                    **reconciliation_data,
                },
            )

        hook_point = "step_end"
        if metrics.get("permission_denied"):
            hook_point = "permission_denied"
        elif metrics.get("persistence_failed"):
            hook_point = "persistence_failed"
        elif metrics.get("needs_reconciliation"):
            hook_point = "reconciliation_required"
        elif not step_succeeded:
            hook_point = "step_failed"
        await trigger_scheduler_hook(
            hook_point,
            step=step,
            error=None if step_succeeded else str(result.error or ""),
            details={
                "status": status_value,
                "metrics": _json_safe(metrics),
            },
        )
        result_data: dict[str, Any] = {
            "step_id": step.step_id,
            "agent_id": f"{workflow_id}_{step.step_id}",
            "agent_name": selected_name,
            "planned_agent": planned_name,
            "executed_agent": selected_name,
            "status": status_value,
            "outputs": {},
            "output_refs": {},
            "metrics": _public_step_metrics(metrics),
            "error": (
                getattr(getattr(result, "failure", None), "message", None)
                or result.error
            ),
        }
        failure = getattr(result, "failure", None)
        if failure is not None:
            result_data["failure"] = failure.model_dump(mode="json")
        unavailable_outputs: dict[str, str] = {}
        if status_value == StepStatus.SUCCEEDED.value:
            for name, ref in (result.outputs or {}).items():
                if isinstance(ref, ArtifactRef):
                    result_data["output_refs"][name] = ref.model_dump()
                try:
                    value = resolver.resolve(
                        ref,
                        subject=state.get("user_id"),
                        scenario=scenario_ctx,
                        action="read",
                    )
                    # Keep the SSE contract JSON-safe without assuming every remote
                    # provider returns only primitive JSON values.
                    result_data["outputs"][name] = _json_safe(value)
                except Exception as exc:  # noqa: BLE001 - fail closed per output
                    unavailable_outputs[name] = type(exc).__name__
        if unavailable_outputs:
            result_data["unavailable_outputs"] = unavailable_outputs

        # Emit the governed, materialized result before end_of_agent so the Web
        # execution card receives its body before the card is finalized.
        await event_queue.put({"event": "step_result", "data": result_data})
        if not step.is_read_only:
            await event_queue.put(
                {
                    "event": "end_of_agent",
                    "data": {
                        "step_id": step.step_id,
                        "agent_name": f"scheduler【{selected_name}】",
                        "agent_id": f"{workflow_id}_{step.step_id}",
                        "sub_agent_name": selected_name,
                        "planned_agent": planned_name,
                        "executed_agent": selected_name,
                        "status": status_value,
                        "failure": (
                            failure.model_dump(mode="json")
                            if failure is not None
                            else None
                        ),
                    },
                }
            )

    receipt_store = PersistentReceiptStore(task_id)

    def build_final_result(status: str) -> dict[str, Any]:
        """Materialize durable leaf outputs through the governed resolver."""
        leaf_results: dict[str, Any] = {}
        source_refs: list[dict[str, Any]] = []
        unavailable: list[dict[str, str]] = []
        persisted_results = state.get("step_results") or {}

        for step_id in _leaf_step_ids(graph):
            raw_result = persisted_results.get(step_id)
            if not isinstance(raw_result, dict):
                continue
            if _status_value(raw_result.get("status")) != StepStatus.SUCCEEDED.value:
                continue

            resolved_outputs: dict[str, Any] = {}
            for output_name, raw_ref in (raw_result.get("outputs") or {}).items():
                try:
                    ref = raw_ref if isinstance(raw_ref, ArtifactRef) else ArtifactRef(**raw_ref)
                except Exception:  # noqa: BLE001 - malformed refs never reach Web
                    unavailable.append(
                        {
                            "step_id": step_id,
                            "output_name": str(output_name),
                            "reason": "invalid_artifact_ref",
                        }
                    )
                    continue

                source_refs.append(
                    {
                        "step_id": step_id,
                        "output_name": str(output_name),
                        "artifact_ref": ref.model_dump(),
                    }
                )
                try:
                    resolved_outputs[str(output_name)] = _json_safe(
                        resolver.resolve(
                            ref,
                            subject=state.get("user_id"),
                            scenario=scenario_ctx,
                            action="read",
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - fail closed, no payload
                    unavailable.append(
                        {
                            "step_id": step_id,
                            "output_name": str(output_name),
                            "reason": type(exc).__name__,
                        }
                    )
            if resolved_outputs:
                leaf_results[step_id] = resolved_outputs

        display_result: Any = leaf_results
        if len(leaf_results) == 1:
            display_result = next(iter(leaf_results.values()))
            if isinstance(display_result, dict) and len(display_result) == 1:
                display_result = next(iter(display_result.values()))

        return {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "workflow_status": status,
            "available": bool(leaf_results),
            "result": display_result if leaf_results else None,
            "leaf_steps": _leaf_step_ids(graph),
            "source_artifact_refs": source_refs,
            "unavailable_artifacts": unavailable,
        }

    scheduler = TaskScheduler(
        execute_step=execute,
        authorize_step=authorize,
        routing_provider=routing,
        store=store,
        resolver=resolver,
        receipt_store=receipt_store,
        redispatch_enabled=bool(redispatch_enabled),
        retry_delay_seconds=max(0.0, float(retry_delay_seconds)),
    )
    ctx = {
        "user_query": state.get("USER_QUERY", "") or state.get("original_user_query", ""),
        "task_id": task_id,
        "workflow_id": workflow_id,
        "subject": state.get("user_id"),
        "scenario": scenario_ctx,
        "agents": agents,
        "authorized_agent_ids": authorized,
        "metadata": {"scenario_tags": state.get("scenario_tags", [])},
        # Per-step ExecutionContext builder so captured artifacts carry the
        # acting user (owner) and the producing agent.
        "context_factory": _make_context_factory(state),
    }
    initial_completed = set(state.get("completed_steps") or [])
    initial_outputs = _restore_outputs(state, initial_completed)
    initial_results = _restore_completed_step_results(state, initial_completed)
    step_map = graph.step_map()
    stale_completed: set[str] = set()
    for step_id in list(initial_results):
        step = step_map.get(step_id)
        expected = _required_step_outputs(step) if step else []
        refs = initial_outputs.get(step_id, {})
        if expected and (
            any(name not in refs for name in expected)
            or any(
                _ref_unavailable(store, refs[name])
                for name in expected
                if name in refs
            )
        ):
            # A checkpoint can claim completion after its protected Artifact
            # payload has gone missing. Do not count that stale success in the
            # resumed terminal/evidence result.
            stale_completed.add(step_id)
            failure = make_failure(
                "ARTIFACT_NOT_FOUND",
                step_id=step_id,
                message="A completed step's saved Artifact is no longer available.",
                action="Restore the Artifact store or restart from an earlier safe checkpoint.",
            )
            initial_results[step_id] = StepResult(
                step_id=step_id,
                status=StepStatus.FAILED,
                error=failure.message,
                failure=failure,
            )
            initial_outputs.pop(step_id, None)
    if stale_completed:
        initial_completed.difference_update(stale_completed)
        state["completed_steps"] = sorted(initial_completed)
    auto_recovery_enabled = str(
        os.getenv("AUTO_RECOVERY_ENABLED", "false")
    ).strip().lower() in {"1", "true", "yes", "y", "on"}
    max_recovery_attempts = max(
        0, int(os.getenv("SCHEDULER_AUTO_RECOVERY_MAX_ATTEMPTS", "1"))
    )
    recovery_attempt = int(
        (state.get("__dag_recovery__") or {}).get("attempt", 0)
        if isinstance(state.get("__dag_recovery__"), dict)
        else 0
    )
    results = None
    run_task: Optional[asyncio.Task] = None
    try:
        while True:
            completed_for_run = set(state.get("completed_steps") or [])
            outputs_for_run = _restore_outputs(state, completed_for_run)
            run_task = asyncio.create_task(
                scheduler.run(
                    graph,
                    context=ctx,
                    initial_completed=completed_for_run,
                    initial_outputs=outputs_for_run,
                    initial_results=initial_results,
                    on_step_start=on_step_start,
                    on_step_end=on_step_end,
                    on_retry=on_retry,
                    commit_step_result=commit_step_result,
                    on_attempt_start=on_attempt_start,
                    on_attempt_end=on_attempt_end,
                )
            )
            try:
                while True:
                    if run_task.done():
                        while not event_queue.empty():
                            yield await event_queue.get()
                        break
                    try:
                        event = await asyncio.wait_for(
                            event_queue.get(), timeout=0.05
                        )
                        yield event
                    except asyncio.TimeoutError:
                        continue
                results = await run_task
            finally:
                if not run_task.done():
                    run_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await run_task

            terminal_value = str(
                getattr(
                    getattr(results, "terminal_status", None),
                    "value",
                    getattr(results, "terminal_status", ""),
                )
            )
            if terminal_value == WorkflowStatus.SUCCEEDED.value:
                break

            recovery_plan = build_dag_recovery_plan(
                graph,
                results,
                set(state.get("completed_steps") or []),
            )
            plan_data = {
                **recovery_plan.to_dict(),
                "attempt": recovery_attempt,
                "max_attempts": max_recovery_attempts,
                "enabled": auto_recovery_enabled,
            }
            record_governance_event(
                "RECOVERY_EVALUATED",
                task_id=task_id,
                workflow_id=str(workflow_id or ""),
                subject=state.get("user_id"),
                decision=(
                    "AUTO_RECOVER"
                    if recovery_plan.automatic
                    and auto_recovery_enabled
                    and recovery_attempt < max_recovery_attempts
                    else "NO_AUTO_RECOVERY"
                ),
                reason_code=recovery_plan.reason_code,
                details=plan_data,
            )
            await event_queue.put(
                {"event": "recovery_plan", "data": plan_data}
            )
            while not event_queue.empty():
                yield await event_queue.get()

            should_recover = (
                auto_recovery_enabled
                and recovery_plan.automatic
                and recovery_attempt < max_recovery_attempts
            )
            if not should_recover:
                break

            recovery_attempt += 1
            apply_dag_recovery_state(
                state,
                recovery_plan,
                attempt=recovery_attempt,
            )
            recovery_data = {
                **recovery_plan.to_dict(),
                "attempt": recovery_attempt,
                "max_attempts": max_recovery_attempts,
            }
            record_governance_event(
                "ROLLBACK_STARTED",
                task_id=task_id,
                workflow_id=str(workflow_id or ""),
                subject=state.get("user_id"),
                decision="DAG_BRANCH_ROLLBACK",
                reason_code=recovery_plan.reason_code,
                details=recovery_data,
            )
            record_governance_event(
                "RESUME_STARTED",
                task_id=task_id,
                workflow_id=str(workflow_id or ""),
                subject=state.get("user_id"),
                decision="AUTO_RESUME",
                reason_code=recovery_plan.reason_code,
                details=recovery_data,
            )
            await event_queue.put(
                {"event": "recovery_started", "data": recovery_data}
            )
            while not event_queue.empty():
                yield await event_queue.get()
            results = None
    except Exception as exc:  # noqa: BLE001 - guarantee end_of_workflow is emitted
        logger.exception("scheduler.run() raised unexpectedly")
        # Drain any events enqueued before the failure so nothing is lost.
        while not event_queue.empty():
            yield await event_queue.get()
        evidence = aggregate_evidence(
            task_id=task_id,
            workflow_id=str(workflow_id or ""),
            execution_mode="scheduler",
            workflow_status=WorkflowStatus.FAILED.value,
            steps=[],
            task_graph=graph,
            planning_steps=state.get("planning_steps") or [],
        )
        persist_skill_evidence(evidence)
        failure = make_failure(
            "INTERNAL_SCHEDULER_ERROR",
            message="The workflow scheduler stopped unexpectedly.",
            action="Retry the workflow. If the problem persists, inspect the server logs.",
        )
        if task_logger is not None:
            if hasattr(task_logger, "log_failure"):
                task_logger.log_failure(failure.model_dump(mode="json"))
        finalize_task_log(WorkflowStatus.FAILED, error=failure.message)
        yield {
            "event": "end_of_workflow",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "mode": "scheduler",
                "status": WorkflowStatus.FAILED.value,
                "error": failure.message,
                "failures": [failure.model_dump(mode="json")],
                "failed_steps": [],
                "blocked_steps": [],
                "skill_execution_evidence": evidence.model_dump(mode="json"),
            },
        }
        return
    finally:
        if run_task is not None and not run_task.done():
            run_task.cancel()
            with suppress(asyncio.CancelledError):
                await run_task
        if results is None and not task_log_finalized:
            finalize_task_log(
                WorkflowStatus.FAILED,
                error="scheduler stream cancelled before a terminal result",
            )

    # ``results`` is a WorkflowResult: it carries the authoritative workflow-level
    # terminal status so the frontend never infers success from the mere
    # presence of an end_of_workflow event.
    failed = [sid for sid, r in results.items() if r.status == StepStatus.FAILED]
    blocked = list(getattr(results, "blocked_steps", []) or [])
    failures = [
        failure.model_dump(mode="json")
        for result in results.values()
        for failure in [getattr(result, "failure", None)]
        if failure is not None
    ]
    failures.extend(
        failure.model_dump(mode="json")
        for failure in (getattr(results, "additional_failures", []) or [])
    )
    if task_logger is not None and hasattr(task_logger, "log_failure"):
        for failure in (getattr(results, "additional_failures", []) or []):
            task_logger.log_failure(failure.model_dump(mode="json"))
    clarifications = [c for c in (
        getattr(results, "clarifications", []) or []) if c]
    approval_required_steps = list(
        getattr(results, "approval_required_steps", []) or []
    )
    rejected = list(getattr(results, "rejected_steps", []) or [])
    needs_recon = list(getattr(results, "needs_reconciliation", []) or [])
    terminal = getattr(results, "terminal_status", None)
    status = str(getattr(terminal, "value", terminal)
                 or WorkflowStatus.SUCCEEDED.value)
    await trigger_scheduler_hook(
        "workflow_end",
        error=(
            None
            if status == WorkflowStatus.SUCCEEDED.value
            else f"scheduler workflow ended with status {status}"
        ),
        details={
            "status": status,
            "failed_steps": failed,
            "recovery_attempts": recovery_attempt,
        },
    )
    evidence_results = _restore_completed_step_results(
        state, set(state.get("completed_steps") or [])
    )
    evidence_results.update(initial_results)
    evidence_results.update(results)
    evidence = build_scheduler_evidence(
        task_id=task_id,
        workflow_id=str(workflow_id or ""),
        graph=graph,
        results=evidence_results,
        artifact_store=store,
        receipt_store=receipt_store,
        planning_steps=state.get("planning_steps") or [],
        workflow_status=status,
    )
    persist_skill_evidence(evidence)
    terminal_error = None
    if status != WorkflowStatus.SUCCEEDED.value:
        terminal_error = (
            f"scheduler workflow ended with status {status}; "
            f"failed_steps={failed}"
        )
    finalize_task_log(status, error=terminal_error)
    record_governance_event(
        "WORKFLOW_TERMINATED",
        task_id=task_id,
        workflow_id=str(workflow_id or ""),
        subject=state.get("user_id"),
        decision=status,
        reason_code=(
            None if status == WorkflowStatus.SUCCEEDED.value else status
        ),
        details={
            "failed_steps": failed,
            "rejected_steps": rejected,
            "approval_required_steps": approval_required_steps,
            "needs_reconciliation": needs_recon,
        },
    )
    yield {
        "event": "final_result",
        "data": build_final_result(status),
    }
    yield {
        "event": "end_of_workflow",
        "data": {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "mode": "scheduler",
            "status": status,
            "failed_steps": failed,
            "blocked_steps": blocked,
            "failures": failures,
            "rejected_steps": rejected,
            "clarifications": clarifications,
            "approval_required_steps": approval_required_steps,
            "needs_reconciliation": needs_recon,
            "results": {sid: str(r.status) for sid, r in results.items()},
            "skill_execution_evidence": evidence.model_dump(mode="json"),
        },
    }
