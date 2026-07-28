from __future__ import annotations

from typing import Any, Iterable

from src.contracts import AgentCard, RoutingDecision, TaskProfile
from src.orchestrator.department_router import build_agent_cards, route_task
from src.orchestrator.task_profiler import profile_task


async def make_routing_decision(
    *,
    user_query: str,
    task_id: str,
    workflow_id: str,
    agents: Iterable[Any],
    authorized_agent_ids: set[str],
    metadata: dict | None = None,
    entity_overrides: dict[str, Any] | None = None,
    context_references: list[dict[str, Any]] | None = None,
    context_artifacts: list[dict[str, Any]] | None = None,
    conversation_context: dict[str, Any] | None = None,
    raw_request: str | None = None,
) -> tuple[TaskProfile, list[AgentCard], RoutingDecision]:
    """主 Agent 的统一入口：任务画像 → 能力卡 → 权限约束候选 → 路由决策。"""
    task_profile = await profile_task(
        user_query,
        task_id=task_id,
        metadata=metadata,
        entity_overrides=entity_overrides,
        context_references=context_references,
        context_artifacts=context_artifacts,
        conversation_context=conversation_context,
        raw_request=raw_request,
    )
    cards = build_agent_cards(agents)
    decision = route_task(
        task_profile,
        cards,
        authorized_agent_ids=authorized_agent_ids,
        workflow_id=workflow_id,
    )
    return task_profile, cards, decision

