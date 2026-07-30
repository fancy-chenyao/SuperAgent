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


def build_task_graph_from_state(state: dict) -> TaskGraph:
    """Resolve a :class:`TaskGraph` from state.

    Accepts an explicit ``state["task_graph"]`` (a ``TaskGraph`` or a dict) or
    falls back to converting ``state["planning_steps"]``.
    """
    tg = state.get("task_graph")
    if isinstance(tg, TaskGraph):
        return tg.validate_dag()
    if isinstance(tg, dict):
        return TaskGraph(**tg).validate_dag()
    steps = state.get("planning_steps") or []
    task_id = state.get("task_id") or state.get("workflow_id") or "task"
    return plan_to_task_graph(steps, task_id=task_id, subject=state.get("user_id"))


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
    }
)


def _public_step_metrics(metrics: Any) -> dict[str, Any]:
    """Return the small operational metric allow-list safe for SSE clients."""

    if not isinstance(metrics, dict):
        return {}
    public: dict[str, Any] = {}
    for key in _PUBLIC_STEP_METRIC_KEYS:
        value = metrics.get(key)
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
    payload["metrics"] = {
        key: value[:256] if isinstance(value, str) else value
        for key in _CHECKPOINT_STEP_METRIC_KEYS
        for value in [metrics.get(key)]
        if value is not None and isinstance(value, (str, int, float, bool))
    }
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

    S-ABAC evaluates one target at a time. Reusing an HR workflow profile for a
    downstream reporting step makes a legitimate cross-agent DAG look like a
    domain mismatch. Step-authored constraints take precedence; trusted
    resource attributes provide a conservative fallback for older plans that
    omitted the optional capability and scenario fields.
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

    required_capabilities = _list_value(
        getattr(step, "required_capabilities", None)
    ) or _list_value(trusted_attrs.get("expected_capabilities"))
    scenario_tags = _list_value(
        getattr(step, "scenario_tags", None)
    ) or _list_value(trusted_attrs.get("scenario_tags"))

    global_risk = str(
        global_profile.get("risk_profile")
        or state.get("risk_profile")
        or "LOW"
    ).upper()
    step_risk = str(getattr(step, "risk_level", "") or global_risk).upper()
    risk_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    risk_profile = max(
        (global_risk, step_risk),
        key=lambda value: risk_order.get(value, risk_order["CRITICAL"]),
    )

    business_goal = str(
        getattr(step, "description", "")
        or getattr(step, "title", "")
        or global_profile.get("business_goal")
        or state.get("original_user_query")
        or state.get("USER_QUERY")
        or ""
    )
    return {
        **global_profile,
        "business_goal": business_goal,
        "task_type": str(
            getattr(step, "task_type", "")
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
            getattr(step, "data_scope", "")
            or global_profile.get("data_scope")
            or "task"
        ),
        "risk_profile": risk_profile,
        "profile_scope": "step",
        "step_id": str(getattr(step, "step_id", "")),
    }


def _build_execution_context(state: dict, step, selected_agent):
    """Build a per-step ExecutionContext carrying acting user + producer agent.

    Isolated per step (never shared across concurrent steps) so captured
    artifacts get correct owner/producer/provenance metadata.
    """
    from src.manager.executor.base import ExecutionContext

    task_profile = _build_step_task_profile(state, step, selected_agent)
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
        await enforce_agent_dispatch(agent, exec_ctx)

        brief = {
            "original_user_query": state.get("original_user_query")
            or state.get("USER_QUERY")
            or "",
            "assigned_agent": selected_agent,
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


async def run_scheduler_workflow(
    state: dict,
    *,
    task_id: str,
    checkpoint_manager: Any = None,
    task_logger: Any = None,
    hook_engine: Any = None,
    execute_step: Optional[ExecuteStep] = None,
    routing_provider: Optional[RoutingProvider] = None,
) -> AsyncGenerator[dict, None]:
    """Drive the scheduler over the state's TaskGraph, yielding workflow events.

    Mirrors the legacy event stream so the frontend/consumers are unaffected.
    """
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

    if routing_provider is None:
        routing = MainAgentRoutingProvider()
        agents, authorized = await _list_agents_and_authorized(state)
    else:
        routing = routing_provider
        agents, authorized = (), set()

    execute = execute_step or _make_real_execute_step(state)

    event_queue: asyncio.Queue[dict] = asyncio.Queue()
    counter = {"step": int(state.get("current_step") or 0)}
    step_numbers: dict[str, int] = {}
    step_agents: dict[str, str] = {}

    def step_number(step_id: str) -> int:
        if step_id not in step_numbers:
            step_numbers[step_id] = counter["step"]
            counter["step"] += 1
        return step_numbers[step_id]

    async def on_step_start(*, step, selected_agent, inputs):
        selected_name = selected_agent or getattr(step, "agent_name", None) or step.step_id
        step_agents[step.step_id] = selected_name
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
                task_logger.log_agent_end(
                    node_name="scheduler",
                    next_node="scheduler",
                    step=step_number(step.step_id),
                    sub_agent_name=getattr(step, "agent_name", None),
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
            step_agents.get(step.step_id)
            or (result.metrics or {}).get("selected_agent")
            or getattr(step, "agent_name", None)
            or step.step_id
        )
        result_data: dict[str, Any] = {
            "step_id": step.step_id,
            "agent_id": f"{workflow_id}_{step.step_id}",
            "agent_name": selected_name,
            "status": status_value,
            "outputs": {},
            "output_refs": {},
            "metrics": _public_step_metrics(result.metrics),
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
        await event_queue.put(
            {
                "event": "end_of_agent",
                "data": {
                    "step_id": step.step_id,
                    "agent_name": f"scheduler【{selected_name}】",
                    "agent_id": f"{workflow_id}_{step.step_id}",
                    "sub_agent_name": selected_name,
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
        routing_provider=routing,
        store=store,
        resolver=resolver,
        receipt_store=receipt_store,
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

    run_task = asyncio.create_task(
        scheduler.run(
            graph,
            context=ctx,
            initial_completed=initial_completed,
            initial_outputs=initial_outputs,
            initial_results=initial_results,
            on_step_start=on_step_start,
            on_step_end=on_step_end,
            commit_step_result=commit_step_result,
        )
    )

    results = None
    try:
        while True:
            if run_task.done():
                while not event_queue.empty():
                    yield await event_queue.get()
                break
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.05)
                yield event
            except asyncio.TimeoutError:
                continue
        results = await run_task
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
        if not run_task.done():
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
    rejected = list(getattr(results, "rejected_steps", []) or [])
    needs_recon = list(getattr(results, "needs_reconciliation", []) or [])
    terminal = getattr(results, "terminal_status", None)
    status = str(getattr(terminal, "value", terminal)
                 or WorkflowStatus.SUCCEEDED.value)
    evidence_results = dict(initial_results)
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
            "needs_reconciliation": needs_recon,
            "results": {sid: str(r.status) for sid, r in results.items()},
            "skill_execution_evidence": evidence.model_dump(mode="json"),
        },
    }
