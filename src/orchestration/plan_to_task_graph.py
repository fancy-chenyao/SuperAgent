"""Convert planner output (``planning_steps``) into a :class:`TaskGraph` (Plan §8, R4).

The existing planner already emits an implicit DAG: each step may declare
``inputs: [{parameter_name, source_step, source_output}]`` where ``source_step``
is the ``agent_name`` of an upstream step (see
``coor_task._validate_plan_data_flow``). This module makes that graph explicit so
the scheduler can execute it.

``depends_on`` is derived from ``inputs[].source_step``; the raw symbolic input
mappings are preserved on each step as an extra ``input_bindings`` field for the
scheduler to resolve to concrete :class:`ArtifactRef` at runtime.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep

# Effective operation modes (ignoring the ubiquitous "delegate") that imply a
# side effect. An agent whose config declares any of these is NOT read-only.
_SEND_MODES = {"send"}
_WRITE_MODES = {"write", "generate", "execute",
                "export", "create", "update", "delete"}
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


def _config_operation_modes(agent_name: str) -> Optional[List[str]]:
    """Return an agent's declared ``allowed_operation_modes`` from S-ABAC config.

    Lazy import keeps this module importable without the security/config stack.
    Returns ``None`` when the agent is not registered.
    """
    if not agent_name:
        return None
    try:
        from config.s_abac_config import RESOURCE_SECURITY_ATTRIBUTES
    except Exception:  # pragma: no cover - config always present in-repo
        return None
    attrs = RESOURCE_SECURITY_ATTRIBUTES.get(agent_name)
    if not isinstance(attrs, dict):
        return None
    return list(attrs.get("allowed_operation_modes", []) or [])


def _derive_operation_mode(
    agent_name: str,
    explicit_mode: Optional[str],
    write_agents: set[str],
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
        base_source = "agent_config"
        base_reason = f"agent_config modes={sorted({str(m).lower() for m in config_modes})}"
        # A caller "write" hint may raise a read baseline to write.
        if agent_name in write_agents and _MODE_RANK["write"] > _MODE_RANK[base_mode]:
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
        # Planner may only escalate risk, never de-escalate a side effect.
        if _MODE_RANK[exp_mode] > _MODE_RANK[base_mode]:
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
    explicit = raw.get("step_id")
    return str(explicit) if explicit else f"step_{index + 1}"


def plan_to_task_graph(
    planning_steps: List[Dict[str, Any]],
    *,
    task_id: str,
    subject: Optional[str] = None,
    goal: str = "",
    agent_produces: Optional[Dict[str, List[str]]] = None,
    write_agents: Optional[set[str]] = None,
) -> TaskGraph:
    """Build (and validate) a :class:`TaskGraph` from ``planning_steps``.

    Args:
        planning_steps: planner output (list of step dicts).
        task_id: id for the resulting :class:`TaskSpec`.
        subject: acting user id (for downstream authorization).
        goal: optional human-readable task goal.
        agent_produces: ``{agent_name: [logical_output, ...]}`` used to fill
            ``expected_outputs`` when a step does not declare it.
        write_agents: agent names whose steps should default to
            ``operation_mode="write"`` when the step does not declare a mode.

    Returns:
        A validated :class:`TaskGraph` (raises ``TaskGraphValidationError`` if the
        derived graph is structurally invalid).
    """
    agent_produces = agent_produces or {}
    write_agents = write_agents or set()

    steps: List[TaskStep] = []
    # Map agent_name -> most-recent prior step_id (source_step references agents).
    agent_to_step: Dict[str, str] = {}

    for idx, raw in enumerate(planning_steps or []):
        if not isinstance(raw, dict):
            continue
        agent_name = raw.get("agent_name") or raw.get("agent") or ""
        step_id = _step_id_for(idx, raw)

        inputs = raw.get("inputs") or []
        depends_on: List[str] = []
        for binding in inputs:
            if not isinstance(binding, dict):
                continue
            source_step = binding.get("source_step")
            if not source_step:
                continue
            resolved = agent_to_step.get(source_step)
            if resolved and resolved not in depends_on:
                depends_on.append(resolved)

        expected_outputs = (
            raw.get("expected_outputs")
            or raw.get("produces")
            or agent_produces.get(agent_name, [])
        )

        operation_mode, operation_mode_source, operation_mode_reason = _derive_operation_mode(
            agent_name, raw.get("operation_mode"), write_agents
        )

        step = TaskStep(
            step_id=step_id,
            required_capabilities=raw.get("required_capabilities", []) or [],
            expected_outputs=list(expected_outputs),
            depends_on=depends_on,
            operation_mode=operation_mode,
            risk_level=raw.get("risk_level", "LOW"),
            resource_locks=raw.get("resource_locks", []) or [],
            preferred_resource_id=agent_name or None,
            # extras (TaskStep has extra="allow"):
            agent_name=agent_name,
            input_bindings=inputs,
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            # trusted classification audit trail
            operation_mode_source=operation_mode_source,
            operation_mode_reason=operation_mode_reason,
        )
        steps.append(step)
        if agent_name:
            agent_to_step[agent_name] = step_id

    graph = TaskGraph(spec=TaskSpec(
        task_id=task_id, goal=goal, subject=subject), steps=steps)
    graph.validate_dag()
    return graph
