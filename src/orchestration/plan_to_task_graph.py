"""Convert planner output (``planning_steps``) into a :class:`TaskGraph` (Plan §8, R4).

The existing planner already emits an implicit DAG: each step may declare
``inputs: [{parameter_name, source_step, source_output}]`` where ``source_step``
is the ``agent_name`` of an upstream step (see
``coor_task._validate_plan_data_flow``). This module makes that graph explicit so
the scheduler can execute it.

``depends_on`` is derived from ``inputs[].source_step`` and every
``inputs[].source_artifacts[].source_step``; the raw symbolic input mappings are
preserved on each step as an extra ``input_bindings`` field for the scheduler to
resolve to concrete :class:`ArtifactRef` at runtime.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.contracts.agent_contract import AgentContract
from src.interface.task_graph import (
    CompletionCondition,
    TaskGraph,
    TaskGraphValidationError,
    TaskSpec,
    TaskStep,
)
from src.orchestration.output_contracts import (
    get_agent_output_logical_names,
    get_agent_output_schema_ref,
)

# Effective operation modes (ignoring the ubiquitous "delegate") that imply a
# side effect. An agent whose config declares any of these is NOT read-only.
_SEND_MODES = {"send"}
_WRITE_MODES = {
    "write",
    "generate",
    "execute",
    "export",
    "create",
    "update",
    "delete",
    "submit",
    "approve",
}
_READ_MODES = {"read", "query", "lookup", "search"}

# Risk ranking of the four classified modes. Higher = more dangerous. Used to
# enforce "Planner output is untrusted": an explicit step declaration may only
# RAISE the risk level, never lower a send/write down to read. ``unknown`` is
# the most dangerous (the runtime fails closed on it).
_MODE_RANK = {"read": 0, "write": 1, "send": 2, "unknown": 3}


def _classify_modes(modes: Optional[List[str]]) -> str:
    """Classify a set of declared operation modes into read/send/write/unknown.

    ``None`` (unregistered agent) or an empty effective set -> ``unknown`` so a
    potential side effect is never silently treated as read-only.
    """
    if modes is None:
        return "unknown"
    effective = {str(m).lower() for m in modes} - {"delegate"}
    if not effective:
        return "unknown"
    if effective & _SEND_MODES:
        return "send"
    if effective & _WRITE_MODES:
        return "write"
    if effective <= _READ_MODES:
        return "read"
    return "unknown"


def _classify_single(mode: str) -> str:
    """Classify a single explicit mode string into read/send/write/unknown."""
    low = str(mode).lower()
    if low in _SEND_MODES:
        return "send"
    if low in _WRITE_MODES:
        return "write"
    if low in _READ_MODES:
        return "read"
    return "unknown"


def _config_security_attributes(agent_name: str) -> Optional[dict[str, Any]]:
    """Return one Agent's trusted S-ABAC resource attributes."""

    if not agent_name:
        return None
    try:
        from config.s_abac_config import RESOURCE_SECURITY_ATTRIBUTES
    except Exception:  # pragma: no cover - config always present in-repo
        return None
    attrs = RESOURCE_SECURITY_ATTRIBUTES.get(agent_name)
    if not isinstance(attrs, dict):
        return None
    return dict(attrs)


def _config_operation_modes(agent_name: str) -> Optional[List[str]]:
    """Return an agent's declared ``allowed_operation_modes`` from S-ABAC config.

    Lazy import keeps this module importable without the security/config stack.
    Returns ``None`` when the agent is not registered.
    """

    attrs = _config_security_attributes(agent_name)
    if attrs is None:
        return None
    return list(attrs.get("allowed_operation_modes", []) or [])


def _constraint_tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return {
        str(item).strip().lower()
        for item in values
        if str(item).strip()
    }


def _validate_planner_security_constraints(
    agent_name: str,
    raw_step: dict[str, Any],
) -> None:
    """Reject Planner claims that contradict trusted Agent classification.

    Planner security fields remain useful as audit/deny-only constraints, but
    they must never widen the target's trusted capability or scenario domain.
    Runtime authorization is built from the global task profile plus these
    trusted resource attributes, not from the raw fields validated here.
    """

    attrs = _config_security_attributes(agent_name)
    if attrs is None:
        return

    for planner_field, trusted_field in (
        ("required_capabilities", "expected_capabilities"),
        ("scenario_tags", "scenario_tags"),
    ):
        claimed = _constraint_tokens(raw_step.get(planner_field))
        trusted = _constraint_tokens(attrs.get(trusted_field))
        if claimed and not claimed.issubset(trusted):
            raise TaskGraphValidationError(
                f"step for {agent_name!r} has Planner {planner_field} "
                "outside trusted Agent security attributes"
            )

    claimed_task_type = str(raw_step.get("task_type") or "").strip().lower()
    trusted_task_type = str(attrs.get("capability_domain") or "").strip().lower()
    if (
        claimed_task_type
        and trusted_task_type
        and claimed_task_type != trusted_task_type
    ):
        raise TaskGraphValidationError(
            f"step for {agent_name!r} has Planner task_type outside trusted "
            "Agent security attributes"
        )


def _derive_operation_mode(
    agent_name: str,
    explicit_mode: Optional[str],
    write_agents: set[str],
    trusted_task_mode: Optional[str] = None,
) -> tuple[str, str, str]:
    """Derive a step's ``(operation_mode, source, reason)`` (read/write/send/unknown).

    Planner output is treated as UNTRUSTED input: the trusted baseline is the
    agent's declared ``allowed_operation_modes`` (S-ABAC config), optionally
    raised by the caller-supplied ``write_agents`` hint. An explicit per-step
    ``operation_mode`` from the plan can only RAISE the risk level (e.g.
    read -> write/send) -- it can NEVER lower a declared ``send``/``write`` down
    to ``read``. When the mode cannot be established it is ``"unknown"`` (never
    silently ``read``) so the runtime refuses to schedule the side effect.

    The returned ``source``/``reason`` provide a trusted audit trail of where
    the classification came from.
    """
    config_modes = _config_operation_modes(agent_name)
    registered = config_modes is not None

    if registered:
        base_mode = _classify_modes(config_modes)
        effective_config_modes = {
            str(mode).lower()
            for mode in config_modes
            if str(mode).lower() != "delegate"
        }
        # Preserve a single concrete mutation verb (generate/export/create/...)
        # end-to-end.  Collapsing it to generic ``write`` makes the scheduler's
        # trusted action disagree with the resource policy, e.g. a document
        # generator that allows ``generate`` is incorrectly denied as
        # ``write``.  Mixed-mode agents still use the conservative class below
        # until a trusted task-profile action selects the invocation mode.
        if len(effective_config_modes) == 1:
            concrete_mode = next(iter(effective_config_modes))
            if concrete_mode not in _READ_MODES:
                base_mode = concrete_mode
        base_source = "agent_config"
        base_reason = f"agent_config modes={sorted({str(m).lower() for m in config_modes})}"
        # Some Agents deliberately expose both query and mutation tools.  The
        # server-derived TaskProfile subtask action selects the concrete mode
        # for this invocation; unlike Planner text, it cannot invent a mode the
        # Agent config does not allow.  This keeps calendar/leave queries read-
        # only while create/update operations remain writes.
        configured_mode_classes = {
            _classify_single(mode)
            for mode in config_modes
            if str(mode).lower() != "delegate"
        }
        trusted_task_class = (
            _classify_single(trusted_task_mode)
            if trusted_task_mode
            else None
        )
        if trusted_task_mode in effective_config_modes:
            # The server-owned task profile chose an exact verb supported by
            # the resource.  Preserve it (notably ``generate``) for policy
            # enforcement and audit output.
            base_mode = str(trusted_task_mode)
            base_source = "task_profile_action"
            base_reason = (
                f"server task-profile action selected exact mode "
                f"{trusted_task_mode} from configured modes="
                f"{sorted(effective_config_modes)}"
            )
        elif trusted_task_class in configured_mode_classes:
            base_mode = str(trusted_task_class)
            base_source = "task_profile_action"
            base_reason = (
                f"server task-profile action selected {trusted_task_class} "
                f"from configured modes={sorted(configured_mode_classes)}"
            )
        # A caller "write" hint may raise a read baseline to write.
        base_rank = _MODE_RANK[_classify_single(base_mode)]
        if agent_name in write_agents and _MODE_RANK["write"] > base_rank:
            base_mode = "write"
            base_source = "caller_write_agents"
            base_reason = "caller-declared write raised read baseline"
    elif agent_name in write_agents:
        # Unregistered but the caller explicitly asserts a write side effect.
        base_mode = "write"
        base_source = "caller_write_agents"
        base_reason = "caller-declared write for unregistered agent"
    else:
        base_mode = "unknown"
        base_source = "unregistered"
        base_reason = "agent not in S-ABAC config; cannot classify side effect"

    if explicit_mode:
        exp_mode = _classify_single(explicit_mode)
        base_rank = _MODE_RANK[_classify_single(base_mode)]
        # Planner may only escalate risk, never de-escalate a side effect.
        if _MODE_RANK[exp_mode] > base_rank:
            return (
                exp_mode,
                "planner_upgrade",
                f"planner raised {base_mode}->{exp_mode} (declared={str(explicit_mode).lower()})",
            )
        return (
            base_mode,
            base_source,
            f"{base_reason}; planner declared={str(explicit_mode).lower()} (not lowered)",
        )

    return base_mode, base_source, base_reason


def _step_id_for(index: int, raw: Dict[str, Any]) -> str:
    explicit = raw.get("step_id") or raw.get("subtask_id")
    return str(explicit) if explicit else f"step_{index + 1}"


def _reference_list(value: Any) -> List[str]:
    """Normalize a single-value/array reference field to a deduplicated list.

    Mirrors ``coor_task._string_list``: upstream validation accepts
    ``"depends_on": "subtask_1"`` as a legal single-value form, so iterating
    the raw field here would silently split the string into characters and
    drop every dependency edge.
    """
    if value is None:
        return []
    raw_items = value if isinstance(value, (list, tuple, set)) else [value]
    return list(dict.fromkeys(
        str(item).strip() for item in raw_items if str(item).strip()
    ))


def _subtask_ids_for(raw: Dict[str, Any]) -> List[str]:
    values = raw.get("subtask_ids")
    if not values:
        values = [raw.get("subtask_id")] if raw.get("subtask_id") else []
    elif not isinstance(values, list):
        values = [values]
    return [str(value) for value in values if value]


def _list_field(raw: Dict[str, Any], plural: str, singular: str) -> List[Any]:
    values = raw.get(plural)
    if not values:
        value = raw.get(singular)
        return [value] if value else []
    return values if isinstance(values, list) else [values]


def _as_list(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def derive_step_dependencies(
    planning_steps: List[Dict[str, Any]],
    subtasks: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Recover dropped data-flow edges from the task profile's ``subtasks``.

    The Planner expresses ordering through ``inputs[].source_step``. But the
    planner prompt tells it to leave ``inputs`` EMPTY for *autonomous* remote
    agents (those without a ``Requires`` field). A report/summary step that
    consumes upstream query outputs is exactly such an autonomous agent, so its
    dependency is lost and the scheduler runs it in parallel with -- instead of
    after -- the steps it depends on.

    The task profile's ``subtasks`` already carry the correct ``depends_on`` DAG
    (computed by the intent profiler). When the Planner declared NO explicit
    dependency on ANY step, and the plan aligns 1:1 with the subtasks (same
    count, emitted in the same topological order), copy each subtask's backward
    ``depends_on`` edges onto the matching plan step as ``depends_on`` expressed
    as upstream ``agent_name`` values -- which :func:`plan_to_task_graph`
    already resolves to concrete ``step_id`` references.

    This is a pure, best-effort *correction*: it never overrides an explicit
    Planner edge and returns ``planning_steps`` unchanged whenever it cannot map
    confidently, so conversion is byte-identical for every plan that already
    carries its own dependencies.
    """
    if not planning_steps or not subtasks:
        return planning_steps
    steps = [s for s in planning_steps if isinstance(s, dict)]
    if len(steps) != len(planning_steps):
        return planning_steps  # non-dict entries -> refuse to guess
    # Trust the Planner: if it declared ANY dependency edge, keep its DAG as-is.
    for step in steps:
        if step.get("depends_on") or step.get("inputs"):
            return planning_steps
    subs = [t for t in subtasks if isinstance(t, dict)]
    if len(subs) != len(steps):
        return planning_steps  # cannot align plan steps to subtasks confidently

    index_by_subtask_id: Dict[str, int] = {}
    for i, task in enumerate(subs):
        sid = task.get("id")
        if sid:
            index_by_subtask_id[str(sid)] = i

    augmented: List[Dict[str, Any]] = [dict(s) for s in steps]
    changed = False
    for i, task in enumerate(subs):
        dep_agents: List[str] = []
        for dep_id in _reference_list(task.get("depends_on")):
            j = index_by_subtask_id.get(str(dep_id))
            # Only backward edges (upstream step precedes this one). Skipping
            # forward/unknown references keeps the derived graph a valid DAG.
            if j is None or j >= i:
                continue
            dep_agent = augmented[j].get("agent_name")
            if dep_agent and dep_agent not in dep_agents:
                dep_agents.append(dep_agent)
        if dep_agents:
            augmented[i]["depends_on"] = dep_agents
            changed = True
    return augmented if changed else planning_steps


def plan_to_task_graph(
    planning_steps: List[Dict[str, Any]],
    *,
    task_id: str,
    subject: Optional[str] = None,
    goal: str = "",
    agent_produces: Optional[Dict[str, List[str]]] = None,
    agent_contracts: Optional[Dict[str, AgentContract | Dict[str, Any]]] = None,
    write_agents: Optional[set[str]] = None,
    subtasks: Optional[List[Dict[str, Any]]] = None,
) -> TaskGraph:
    """Build (and validate) a :class:`TaskGraph` from ``planning_steps``.

    Args:
        planning_steps: planner output (list of step dicts).
        task_id: id for the resulting :class:`TaskSpec`.
        subject: acting user id (for downstream authorization).
        goal: optional human-readable task goal.
        agent_produces: ``{agent_name: [logical_output, ...]}`` used to fill
            ``expected_outputs`` when a step does not declare it.
        agent_contracts: versioned Agent contracts copied onto TaskSteps so the
            Scheduler can normalize and validate actual results.
        write_agents: agent names whose steps should default to
            ``operation_mode="write"`` when the step does not declare a mode.
        subtasks: optional task-profile ``subtasks`` used as a fail-safe to
            recover dependency edges the Planner drops for autonomous agents
            (see :func:`derive_step_dependencies`). Ignored when the plan
            already declares its own edges.

    Returns:
        A validated :class:`TaskGraph` (raises ``TaskGraphValidationError`` if the
        derived graph is structurally invalid).
    """
    agent_produces = agent_produces or {}
    agent_contracts = agent_contracts or {}
    write_agents = write_agents or set()
    if subtasks:
        planning_steps = derive_step_dependencies(planning_steps, subtasks)

    subtask_by_id = {
        str(item.get("id")): item
        for item in (subtasks or [])
        if isinstance(item, dict) and item.get("id")
    }

    raw_steps = [
        (idx, raw)
        for idx, raw in enumerate(planning_steps or [])
        if isinstance(raw, dict)
    ]

    # Build structural aliases before resolving edges so a valid TaskGraph can
    # reference any step_id/subtask_id, including a step that appears later in
    # the Planner's list. Agent-name aliases remain a legacy, backward-only
    # fallback because the same agent may legitimately execute multiple steps.
    reference_to_step: Dict[str, str] = {}
    for idx, raw in raw_steps:
        step_id = _step_id_for(idx, raw)
        aliases = [step_id, *_subtask_ids_for(raw)]
        for alias in aliases:
            existing = reference_to_step.get(alias)
            if existing and existing != step_id:
                raise TaskGraphValidationError(
                    f"reference '{alias}' maps to multiple steps: "
                    f"'{existing}' and '{step_id}'"
                )
            reference_to_step[alias] = step_id

    steps: List[TaskStep] = []
    prior_agent_to_step: Dict[str, str] = {}

    def resolve_reference(reference: Any) -> str:
        key = str(reference)
        return reference_to_step.get(key) or prior_agent_to_step.get(key) or key

    for idx, raw in raw_steps:
        agent_name = raw.get("agent_name") or raw.get("agent") or ""
        step_id = _step_id_for(idx, raw)

        inputs = list(raw.get("inputs") or [])
        depends_on: List[str] = []
        for dependency_ref in _reference_list(raw.get("depends_on")):
            resolved = resolve_reference(dependency_ref)
            if resolved not in depends_on:
                depends_on.append(resolved)
        for binding in inputs:
            if not isinstance(binding, dict):
                continue
            sources = binding.get("source_artifacts")
            if isinstance(sources, list):
                if binding.get("source_step") or binding.get("source_output"):
                    raise TaskGraphValidationError(
                        "input binding cannot mix source_artifacts with "
                        "source_step/source_output"
                    )
                source_bindings = sources
            elif sources is not None:
                raise TaskGraphValidationError(
                    "source_artifacts must be a list"
                )
            else:
                source_bindings = [binding]
            for source_binding in source_bindings:
                if not isinstance(source_binding, dict):
                    raise TaskGraphValidationError(
                        "source_artifacts entries must be objects"
                    )
                source_step = source_binding.get("source_step")
                if not source_step:
                    continue
                resolved = resolve_reference(source_step)
                if resolved not in depends_on:
                    depends_on.append(resolved)

        # The registry-provided contract is trusted platform metadata. Planner
        # output is untrusted and must never inject a contract: a step-level
        # ``agent_contract`` in the plan is ignored outright, so a fabricated
        # or weakened contract can never reach the Scheduler.
        raw_contract = agent_contracts.get(agent_name)
        contract = (
            raw_contract
            if isinstance(raw_contract, AgentContract)
            else AgentContract.model_validate(raw_contract)
            if raw_contract
            else None
        )
        declared_outputs = raw.get("expected_outputs") or raw.get("produces")
        if isinstance(declared_outputs, str):
            declared_outputs = [declared_outputs]
        if contract:
            contract_outputs = [ref.name for ref in contract.produces]
            undeclared = set(declared_outputs or []) - set(contract_outputs)
            if undeclared:
                raise TaskGraphValidationError(
                    f"step {step_id!r} declares outputs not present in trusted "
                    f"Agent contract: {sorted(undeclared)}"
                )
            expected_outputs = contract_outputs
        else:
            expected_outputs = (
                declared_outputs
                or agent_produces.get(agent_name, [])
                or get_agent_output_logical_names(agent_name)
            )
        if isinstance(expected_outputs, str):
            expected_outputs = [expected_outputs]

        # ``depends_on`` is an execution-order edge.  For a governed Agent
        # chain it must also carry the producer Artifact into the consumer.
        # Planner-generated autonomous steps commonly omit ``inputs``; when a
        # dependency has a trusted primary output, materialize one unambiguous
        # binding.  Explicit Planner bindings are always preserved unchanged.
        if not inputs and depends_on:
            prior_steps = {item.step_id: item for item in steps}
            for dependency_id in depends_on:
                producer = prior_steps.get(dependency_id)
                producer_outputs = list(
                    getattr(producer, "expected_outputs", []) or []
                )
                if not producer_outputs:
                    continue
                inputs.append(
                    {
                        "parameter_name": f"upstream_{dependency_id}",
                        "source_step": dependency_id,
                        "source_output": producer_outputs[0],
                    }
                )

        completion_conditions = []
        for condition in raw.get("completion_conditions") or []:
            if isinstance(condition, str) and condition.strip():
                completion_conditions.append(
                    CompletionCondition(expression=condition.strip())
                )
            elif isinstance(condition, dict) and condition.get("expression"):
                completion_conditions.append(CompletionCondition(**condition))

        trusted_modes = [
            str(subtask_by_id[subtask_id].get("action") or "").lower()
            for subtask_id in _subtask_ids_for(raw)
            if subtask_id in subtask_by_id
        ]
        trusted_task_mode = (
            max(
                trusted_modes,
                key=lambda value: _MODE_RANK[_classify_single(value)],
            )
            if trusted_modes
            else None
        )
        operation_mode, operation_mode_source, operation_mode_reason = _derive_operation_mode(
            agent_name,
            raw.get("operation_mode"),
            write_agents,
            trusted_task_mode,
        )
        _validate_planner_security_constraints(agent_name, raw)

        step = TaskStep(
            step_id=step_id,
            required_capabilities=raw.get("required_capabilities", []) or [],
            expected_outputs=list(expected_outputs),
            depends_on=depends_on,
            completion_conditions=completion_conditions,
            operation_mode=operation_mode,
            risk_level=raw.get("risk_level", "LOW"),
            timeout=raw.get("timeout"),
            retry=max(0, int(raw.get("retry") or 0)),
            resource_locks=raw.get("resource_locks", []) or [],
            preferred_resource_id=agent_name or None,
            # extras (TaskStep has extra="allow"):
            agent_name=agent_name,
            input_bindings=inputs,
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            task_type=raw.get("task_type", ""),
            scenario_tags=raw.get("scenario_tags", []) or [],
            data_scope=raw.get("data_scope", ""),
            expected_schema_ref=(
                raw.get("expected_schema_ref")
                or raw.get("output_schema_ref")
                # A caller-supplied output vocabulary may describe a legacy or
                # custom contract.  Do not attach the built-in schema to that
                # payload; the trusted schema is selected only together with
                # the server-owned logical-output contract.
                or (
                    get_agent_output_schema_ref(agent_name)
                    if not declared_outputs
                    else None
                )
            ),
            expected_schema_refs=(
                dict(contract.output_schema_refs) if contract else {}
            ),
            agent_contract=(
                contract.model_dump(mode="json") if contract else None
            ),
            verification_contract=dict(raw.get("verification_contract") or {}),
            subtask_ids=_subtask_ids_for(raw),
            intents=_list_field(raw, "intents", "intent"),
            # trusted classification audit trail
            operation_mode_source=operation_mode_source,
            operation_mode_reason=operation_mode_reason,
        )
        steps.append(step)
        if agent_name:
            prior_agent_to_step[agent_name] = step_id

    graph = TaskGraph(spec=TaskSpec(
        task_id=task_id, goal=goal, subject=subject), steps=steps)
    graph.validate_dag()
    return graph
