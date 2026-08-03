"""TaskGraph types (Plan §8).

Pure pydantic v2 models describing a multi-step task as a DAG. No dependency on
the workflow/manager/LLM stack, so importable and unit-testable in isolation.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.agent_contract import AgentContract

from .artifact import ArtifactRef


class WorkflowStatus(str, Enum):
    """Canonical terminal status of a scheduler workflow run.

    The scheduler returns exactly one of these; the runtime maps it verbatim to
    the ``end_of_workflow`` SSE ``status`` so the frontend never has to guess
    success from the mere presence of an ``end_of_workflow`` event.
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    CLARIFY_REQUIRED = "CLARIFY_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    REJECTED = "REJECTED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


class TaskGraphValidationError(ValueError):
    """Raised by :meth:`TaskGraph.validate_dag` for a structurally invalid graph."""


class CompletionCondition(BaseModel):
    """A step-completion predicate.

    ``expression`` is a *restricted* mini-DSL evaluated by
    :mod:`src.orchestration.completion` (Phase 4). It is NEVER passed to
    ``eval``/``exec``; only a whitelisted grammar is interpreted.
    """

    expression: str
    description: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class TaskStep(BaseModel):
    """A single node in a :class:`TaskGraph`.

    Dependencies are expressed via ``depends_on`` (a list of upstream
    ``step_id``); data dependencies are captured in ``required_inputs``.
    """

    step_id: str
    required_capabilities: List[str] = Field(default_factory=list)
    # param_name -> ArtifactRef (filled at author time for hand-written graphs,
    # or by the scheduler as upstream steps complete).
    required_inputs: Dict[str, ArtifactRef] = Field(default_factory=dict)
    # Planner-authored symbolic bindings. A binding uses either the legacy
    # single source_step/source_output form or the fan-in source_artifacts form.
    input_bindings: List[Dict[str, Any]] = Field(default_factory=list)
    expected_outputs: List[str] = Field(default_factory=list)  # logical names
    expected_schema_ref: Optional[str] = None  # legacy primary-output schema
    expected_schema_refs: Dict[str, str] = Field(default_factory=dict)
    agent_contract: Optional[AgentContract] = None
    depends_on: List[str] = Field(default_factory=list)  # upstream step_ids
    completion_conditions: List[CompletionCondition] = Field(
        default_factory=list)

    operation_mode: str = "read"  # "read" | "write"
    risk_level: str = "LOW"
    timeout: Optional[float] = None  # seconds
    retry: int = 0  # max retries
    resource_locks: List[str] = Field(default_factory=list)
    # Optional business compensation contract. Recovery planning surfaces this
    # for a human/operator; it is never executed blindly as an ordinary retry.
    compensation_action: Optional[Dict[str, Any]] = None
    preferred_resource_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")

    @property
    def is_read_only(self) -> bool:
        return str(self.operation_mode).lower() == "read"


class TaskSpec(BaseModel):
    """Task-level metadata (the "what") that a :class:`TaskGraph` fulfils."""

    task_id: str
    goal: str = ""
    # acting user_id, for downstream authorization
    subject: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


class TaskGraph(BaseModel):
    """A DAG of :class:`TaskStep` with structural validation helpers."""

    spec: TaskSpec
    steps: List[TaskStep] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")

    def step_map(self) -> Dict[str, TaskStep]:
        """Return ``{step_id: TaskStep}``. Assumes unique ids (see validate_dag)."""
        return {step.step_id: step for step in self.steps}

    def validate_dag(self) -> "TaskGraph":
        """Validate: unique step ids, existing dependencies, and acyclicity.

        Returns ``self`` on success; raises :class:`TaskGraphValidationError`
        otherwise.
        """
        # 1) Unique step ids
        seen: set[str] = set()
        duplicates: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                duplicates.add(step.step_id)
            seen.add(step.step_id)
        if duplicates:
            raise TaskGraphValidationError(
                f"duplicate step_id(s): {sorted(duplicates)}"
            )

        # 2) Dependencies must reference existing steps (and not self)
        for step in self.steps:
            for dep in step.depends_on:
                if dep == step.step_id:
                    raise TaskGraphValidationError(
                        f"step '{step.step_id}' depends on itself"
                    )
                if dep not in seen:
                    raise TaskGraphValidationError(
                        f"step '{step.step_id}' depends on unknown step '{dep}'"
                    )

        # 3) Acyclicity via Kahn's algorithm
        self._assert_acyclic()
        return self

    def _assert_acyclic(self) -> None:
        smap = self.step_map()
        in_degree: Dict[str, int] = {sid: 0 for sid in smap}
        for step in self.steps:
            for _dep in step.depends_on:
                in_degree[step.step_id] += 1

        ready = [sid for sid, deg in in_degree.items() if deg == 0]
        resolved = 0
        while ready:
            current = ready.pop()
            resolved += 1
            for step in self.steps:
                if current in step.depends_on:
                    in_degree[step.step_id] -= 1
                    if in_degree[step.step_id] == 0:
                        ready.append(step.step_id)

        if resolved != len(smap):
            remaining = sorted(
                sid for sid, deg in in_degree.items() if deg > 0)
            raise TaskGraphValidationError(
                f"cycle detected among steps: {remaining}")

    def topological_order(self) -> List[str]:
        """Return step ids in a valid execution order (validates first)."""
        self.validate_dag()
        smap = self.step_map()
        in_degree: Dict[str, int] = {sid: 0 for sid in smap}
        for step in self.steps:
            in_degree[step.step_id] += len(step.depends_on)

        # Deterministic order: process zero-in-degree nodes in insertion order.
        order: List[str] = []
        ready = [step.step_id for step in self.steps if in_degree[step.step_id] == 0]
        while ready:
            current = ready.pop(0)
            order.append(current)
            for step in self.steps:
                if current in step.depends_on:
                    in_degree[step.step_id] -= 1
                    if in_degree[step.step_id] == 0:
                        ready.append(step.step_id)
        return order

    def ready_steps(self, completed: set[str]) -> List[str]:
        """Return ids of steps whose dependencies are all in ``completed``.

        Excludes steps already in ``completed``. Used by the Phase 3 scheduler.
        """
        result: List[str] = []
        for step in self.steps:
            if step.step_id in completed:
                continue
            if all(dep in completed for dep in step.depends_on):
                result.append(step.step_id)
        return result
