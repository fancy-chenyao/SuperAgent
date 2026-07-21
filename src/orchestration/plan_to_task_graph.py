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

        operation_mode = raw.get("operation_mode")
        if not operation_mode:
            operation_mode = "write" if agent_name in write_agents else "read"

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
        )
        steps.append(step)
        if agent_name:
            agent_to_step[agent_name] = step_id

    graph = TaskGraph(spec=TaskSpec(task_id=task_id, goal=goal, subject=subject), steps=steps)
    graph.validate_dag()
    return graph
