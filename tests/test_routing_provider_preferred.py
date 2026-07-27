"""MainAgentRoutingProvider must honor a step's ``preferred_resource_id``.

Regression for the multi-agent misrouting bug: the candidate scoring runs
against the GLOBAL user_query (identical for every step), so without honoring
the per-step preferred agent, every step of a composite plan collapses onto the
same top-scoring agent (e.g. an HR-query step and a knowledge-lookup step both
routed to the knowledge agent -> two concurrent calls contend and one times
out). The provider must narrow to the plan's per-step agent WHEN it is an
authorized/capable candidate, and must never bypass a REJECT/CLARIFY verdict.
"""

import asyncio

import src.orchestrator as orchestrator_pkg
from src.contracts import RoutingCandidate, RoutingDecision
from src.interface.task_graph import TaskStep
from src.orchestration.providers import MainAgentRoutingProvider


class _Profile:
    clarification_questions: list = []


def _decision(*, decision, selected, candidates):
    return RoutingDecision(
        decision_id="d1",
        task_id="t1",
        selected_agent=selected,
        candidate_agents=[
            RoutingCandidate(agent_id=a, score=s) for a, s in candidates
        ],
        decision=decision,
        confidence=0.9,
        reason_codes=["HIGH_CONFIDENCE_ROUTE"],
        trace_id="trace",
    )


def _patch(monkeypatch, decision):
    async def _fake(**_kwargs):
        return _Profile(), [], decision

    monkeypatch.setattr(orchestrator_pkg, "make_routing_decision", _fake)


def _decide(step):
    provider = MainAgentRoutingProvider()
    return asyncio.run(
        provider.decide(
            step,
            user_query="需要查询王强档案、年假制度并汇总",
            task_id="t1",
            workflow_id="wf1",
            agents=(),
            authorized_agent_ids={"RemoteHRAssistantAgent", "RemoteKnowledgeAgent"},
        )
    )


def test_honor_preferred_when_it_is_a_candidate(monkeypatch):
    # Global-query scoring picked Knowledge for everything, but this step's plan
    # assigned HR; HR is an authorized candidate -> honor it.
    _patch(
        monkeypatch,
        _decision(
            decision="DISPATCH",
            selected="RemoteKnowledgeAgent",
            candidates=[("RemoteKnowledgeAgent", 0.9), ("RemoteHRAssistantAgent", 0.7)],
        ),
    )
    step = TaskStep(step_id="step_1", preferred_resource_id="RemoteHRAssistantAgent")
    result = _decide(step)
    assert result.selected_agent == "RemoteHRAssistantAgent"
    assert "HONOR_PREFERRED_RESOURCE" in result.reason_codes


def test_keep_routing_pick_when_preferred_not_a_candidate(monkeypatch):
    # Preferred agent did not pass the permission/capability gate -> do NOT
    # override; keep the routing verdict (fail safe, never bypass the gate).
    _patch(
        monkeypatch,
        _decision(
            decision="DISPATCH",
            selected="RemoteKnowledgeAgent",
            candidates=[("RemoteKnowledgeAgent", 0.9)],
        ),
    )
    step = TaskStep(step_id="step_1", preferred_resource_id="RemoteHRAssistantAgent")
    result = _decide(step)
    assert result.selected_agent == "RemoteKnowledgeAgent"
    assert "HONOR_PREFERRED_RESOURCE" not in result.reason_codes


def test_reject_is_never_overridden_by_preferred(monkeypatch):
    # A non-DISPATCH verdict must clear the agent even if the plan preferred one.
    _patch(
        monkeypatch,
        _decision(
            decision="REJECT",
            selected=None,
            candidates=[("RemoteHRAssistantAgent", 0.7)],
        ),
    )
    step = TaskStep(step_id="step_1", preferred_resource_id="RemoteHRAssistantAgent")
    result = _decide(step)
    assert result.selected_agent is None
    assert result.decision == "REJECT"


def test_no_preferred_keeps_routing_pick(monkeypatch):
    _patch(
        monkeypatch,
        _decision(
            decision="DISPATCH",
            selected="RemoteKnowledgeAgent",
            candidates=[("RemoteKnowledgeAgent", 0.9), ("RemoteHRAssistantAgent", 0.7)],
        ),
    )
    step = TaskStep(step_id="step_2")  # no preferred_resource_id
    result = _decide(step)
    assert result.selected_agent == "RemoteKnowledgeAgent"
