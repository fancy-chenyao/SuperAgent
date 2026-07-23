"""Routing & reliability providers (Plan §8, Phase 3).

Thin seams the scheduler depends on. Two implementations each:

- **Real** (production default): binds to the teammate code already on ``main``
  (``src.orchestrator.make_routing_decision``). Imported lazily so this module
  stays unit-testable in isolation.
- **Stub** (unit tests): deterministic, dependency-free.

The scheduler only reads ``RoutingResult.selected_agent``; both the stub result
and the real ``RoutingDecision`` expose that attribute (duck-typed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Protocol, runtime_checkable

from src.interface.task_graph import TaskStep


@dataclass
class RoutingResult:
    """Minimal routing outcome consumed by the scheduler.

    ``decision`` mirrors the main agent's verdict (``DISPATCH`` / ``REJECT`` /
    ``CLARIFY`` / ``NO_CAPABLE_AGENT``). The scheduler MUST honor it: only
    ``DISPATCH`` with a concrete ``selected_agent`` may execute; a rejection or
    clarification must never silently fall back to ``preferred_resource_id``.
    """

    selected_agent: Optional[str]
    decision: str = "DISPATCH"
    clarification: Optional[str] = None
    confidence: float = 1.0
    reason_codes: List[str] = field(default_factory=list)
    raw: Any = None  # the underlying RoutingDecision when produced by the real provider


@runtime_checkable
class RoutingProvider(Protocol):
    """Selects the agent that should execute a step."""

    async def decide(
        self,
        step: TaskStep,
        *,
        user_query: str,
        task_id: str,
        workflow_id: str,
        agents: Iterable[Any],
        authorized_agent_ids: set[str],
        metadata: Optional[dict] = None,
    ) -> RoutingResult: ...


@runtime_checkable
class ReliabilityProvider(Protocol):
    """Supplies a historical-reliability prior for an agent in a scenario."""

    def prior(
        self,
        *,
        agent_id: str,
        step: Optional[TaskStep] = None,
        scenario_tags: Optional[Iterable[str]] = None,
    ) -> float: ...


class StubRoutingProvider:
    """Deterministic routing for unit tests: honor ``preferred_resource_id``."""

    async def decide(
        self,
        step: TaskStep,
        *,
        user_query: str = "",
        task_id: str = "",
        workflow_id: str = "",
        agents: Iterable[Any] = (),
        authorized_agent_ids: Optional[set[str]] = None,
        metadata: Optional[dict] = None,
    ) -> RoutingResult:
        return RoutingResult(
            selected_agent=step.preferred_resource_id,
            decision="DISPATCH",
            confidence=1.0,
            reason_codes=["stub:preferred_resource_id"],
        )


class MainAgentRoutingProvider:
    """Production routing: delegate to ``src.orchestrator.make_routing_decision``.

    ``make_routing_decision`` returns ``(TaskProfile, list[AgentCard],
    RoutingDecision)``; we surface the third element's ``selected_agent``.
    """

    async def decide(
        self,
        step: TaskStep,
        *,
        user_query: str,
        task_id: str,
        workflow_id: str,
        agents: Iterable[Any],
        authorized_agent_ids: set[str],
        metadata: Optional[dict] = None,
    ) -> RoutingResult:
        # Lazy import keeps this module importable without the orchestrator stack.
        from src.orchestrator import make_routing_decision

        meta = dict(metadata or {})
        if step.required_capabilities:
            meta.setdefault("required_capabilities", list(step.required_capabilities))
        if step.preferred_resource_id:
            meta.setdefault("preferred_resource_id", step.preferred_resource_id)

        profile, _cards, decision = await make_routing_decision(
            user_query=user_query,
            task_id=task_id,
            workflow_id=workflow_id,
            agents=agents,
            authorized_agent_ids=authorized_agent_ids,
            metadata=meta,
        )
        # Honor the main agent's verdict. Only DISPATCH may execute; on
        # REJECT/CLARIFY/NO_CAPABLE_AGENT we return no agent so the scheduler
        # cannot fall back to ``preferred_resource_id`` and bypass the decision.
        decision_kind = str(getattr(decision, "decision", "DISPATCH") or "DISPATCH").upper()
        selected = getattr(decision, "selected_agent", None)
        clarification: Optional[str] = None
        if decision_kind != "DISPATCH":
            selected = None
            questions = list(getattr(profile, "clarification_questions", []) or [])
            clarification = "; ".join(questions) if questions else None
        return RoutingResult(
            selected_agent=selected,
            decision=decision_kind,
            clarification=clarification,
            confidence=float(getattr(decision, "confidence", 0.0) or 0.0),
            reason_codes=list(getattr(decision, "reason_codes", []) or []),
            raw=decision,
        )


class ScenarioPriorReliabilityProvider:
    """Heuristic reliability prior.

    NOTE: ``src/memory`` currently exposes session/long-term memory only, not an
    agent success-rate API, so this returns a scenario-informed prior rather than
    a learned score. FUTURE: aggregate historical success from
    ``store/task_logs`` / memory to replace the static prior.
    """

    def __init__(self, default_prior: float = 0.8, risk_penalty: float = 0.2) -> None:
        self._default = default_prior
        self._risk_penalty = risk_penalty

    def prior(
        self,
        *,
        agent_id: str,
        step: Optional[TaskStep] = None,
        scenario_tags: Optional[Iterable[str]] = None,
    ) -> float:
        score = self._default
        if step is not None and str(step.risk_level).upper() in {"HIGH", "CRITICAL"}:
            score -= self._risk_penalty
        return max(0.0, min(1.0, score))
