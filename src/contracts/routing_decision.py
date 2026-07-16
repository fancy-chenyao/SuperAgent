from __future__ import annotations

from pydantic import BaseModel, Field


class RoutingCandidate(BaseModel):
    agent_id: str
    score: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, float] = Field(default_factory=dict)


class ExcludedAgent(BaseModel):
    agent_id: str
    reason: str
    reason_code: str


class RoutingDecision(BaseModel):
    decision_id: str
    task_id: str
    selected_agent: str | None = None
    candidate_agents: list[RoutingCandidate] = Field(default_factory=list)
    decision: str = "CLARIFY"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    required_grants: list[str] = Field(default_factory=list)
    excluded_agents: list[ExcludedAgent] = Field(default_factory=list)
    trace_id: str

