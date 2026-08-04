from __future__ import annotations

import hashlib
from typing import Any, Iterable

from config.s_abac_config import AGENT_SECURITY_ATTRIBUTES, RESOURCE_SECURITY_ATTRIBUTES
from src.contracts import AgentCard, ExcludedAgent, RoutingCandidate, RoutingDecision, TaskProfile
from src.orchestration.output_contracts import get_agent_output_schema


KNOWN_AGENT_CAPABILITIES = {
    "researcher": (["Research"], ["information_research", "knowledge_lookup"]),
    "browser": (["Research"], ["information_research", "web_lookup"]),
    "coder": (["Engineering", "Learning"], ["programming_learning", "technology_support", "code_execution"]),
    "reporter": (["Document"], ["report_generation", "analysis_summary"]),
    "RemoteWeatherAgent": (["Weather"], ["weather_query"]),
    "RemoteHRAssistantAgent": (["HR"], ["employee_information_query", "salary_query"]),
    "RemoteUnicornSelectorAgent": (["Research"], ["information_research", "business_research"]),
    "RemoteBusinessRiskAgent": (["Risk"], ["risk_analysis"]),
    "RemoteReportAgent": (["Document"], ["report_generation", "analysis_summary"]),
    "RemoteEmailDispatchAgent": (["Communication"], ["message_or_email_send"]),
    "RemoteScheduleAgent": (["Office"], ["schedule_management"]),
    "RemoteTodoAgent": (["Office"], ["schedule_management", "todo_management"]),
    "RemoteHRCalendarAgent": (["HR", "Office"], ["schedule_management", "hr_calendar"]),
    "RemoteKnowledgeAgent": (["Knowledge"], ["knowledge_lookup"]),
    "RemoteDocumentGeneratorAgent": (["Document"], ["document_generation", "report_generation"]),
    "RemoteOfficeAssistantAgent": (
        ["HR", "Office"],
        ["leave_record_query", "travel_service", "office_assistance"],
    ),
    "RemoteMeetingManagerAgent": (["Meeting", "Office"], ["meeting_arrangement"]),
    "RemoteCommunicationAgent": (["Communication"], ["message_or_email_send"]),
}

KNOWN_ACTIONS = {
    "coder": ["read", "generate", "execute", "write"],
    "researcher": ["read", "query"],
    "browser": ["read", "query"],
    "reporter": ["generate"],
    "RemoteHRAssistantAgent": ["read", "query"],
    "RemoteDocumentGeneratorAgent": ["generate"],
    "RemoteReportAgent": ["generate"],
    "RemoteEmailDispatchAgent": ["send"],
    "RemoteCommunicationAgent": ["send", "generate"],
    "RemoteMeetingManagerAgent": ["read", "write", "delete"],
    "RemoteScheduleAgent": ["read", "write", "delete"],
    "RemoteTodoAgent": ["read", "write", "delete"],
    "RemoteHRCalendarAgent": ["read", "query", "write"],
    "RemoteWeatherAgent": ["read", "query"],
    "RemoteOfficeAssistantAgent": ["read", "query", "write"],
}

RISK_LEVEL = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if item is not None]


def _normalized(values: Iterable[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _overlap(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = _normalized(left)
    right_set = _normalized(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set)


def _any_overlap(left: Iterable[str], right: Iterable[str]) -> float:
    return 1.0 if _normalized(left) & _normalized(right) else 0.0


def _coverage_score(items: Iterable[dict[str, Any]], field: str, card_values: Iterable[str]) -> float:
    subtasks = list(items or [])
    if not subtasks:
        return 0.0
    card_set = _normalized(card_values)
    if not card_set:
        return 0.0
    matched = 0
    for item in subtasks:
        values = item.get(field)
        if isinstance(values, str):
            item_values = [values]
        else:
            item_values = list(values or [])
        if _normalized(item_values) & card_set:
            matched += 1
    return matched / len(subtasks)


def _composite_candidate_coverage(
    task_profile: TaskProfile,
    candidates: list[RoutingCandidate],
    agent_cards: list[AgentCard],
) -> float:
    subtasks = list(getattr(task_profile, "subtasks", []) or [])
    if not subtasks:
        return 0.0
    candidate_ids = {item.agent_id for item in candidates}
    candidate_cards = [card for card in agent_cards if card.agent_id in candidate_ids]
    covered = 0
    for subtask in subtasks:
        intent = _normalized([subtask.get("intent")])
        capabilities = _normalized(subtask.get("expected_capabilities") or [])
        scenario_tags = _normalized(subtask.get("scenario_tags") or [])
        for card in candidate_cards:
            if (
                intent & _normalized(card.intents)
                or capabilities & _normalized(card.capabilities)
                or scenario_tags & _normalized(card.scenario_tags + card.intents)
            ):
                covered += 1
                break
    return covered / len(subtasks)


def _tool_names(agent: Any) -> list[str]:
    result = []
    for tool in getattr(agent, "selected_tools", []) or []:
        name = getattr(tool, "name", None)
        if name:
            result.append(str(name))
    return result


def build_agent_cards(agents: Iterable[Any]) -> list[AgentCard]:
    cards = []
    for agent in agents:
        name = str(getattr(agent, "agent_name", ""))
        if not name:
            continue
        subject_attrs = AGENT_SECURITY_ATTRIBUTES.get(name, {})
        object_attrs = RESOURCE_SECURITY_ATTRIBUTES.get(name, {})
        known_capabilities, known_intents = KNOWN_AGENT_CAPABILITIES.get(name, ([], []))
        capabilities = list(dict.fromkeys(
            known_capabilities + _list(object_attrs.get("expected_capabilities"))
        ))
        intents = list(dict.fromkeys(
            known_intents + _list(object_attrs.get("scenario_tags"))
        ))
        actions = list(dict.fromkeys(
            KNOWN_ACTIONS.get(name, []) + _list(object_attrs.get("allowed_operation_modes"))
        ))
        risk_ceiling = (
            "HIGH"
            if any(action in actions for action in ("send", "delete"))
            else "MEDIUM"
            if any(action in actions for action in ("generate", "write", "execute"))
            else "LOW"
        )
        tools = _tool_names(agent)
        source = getattr(agent, "source", "local")
        source_value = getattr(source, "value", source)
        agent_contract = getattr(agent, "agent_contract", None)
        cards.append(
            AgentCard(
                agent_id=name,
                name=str(getattr(agent, "nick_name", "") or name),
                department=str(
                    object_attrs.get("department_domain")
                    or subject_attrs.get("department")
                    or "General"
                ),
                capabilities=capabilities or ["General"],
                intents=intents or ["general_assistance"],
                supported_actions=actions or ["read"],
                accepted_data_scopes=_list(object_attrs.get("accepted_data_scopes")) or ["general"],
                scenario_tags=_list(object_attrs.get("scenario_tags")),
                risk_ceiling=risk_ceiling,
                required_grants=_list(object_attrs.get("grants_required")),
                tool_scopes=tools,
                output_schema=get_agent_output_schema(name),
                version=str(getattr(agent, "version", "1.0.0") or "1.0.0"),
                status="ONLINE",
                description=str(getattr(agent, "description", "") or ""),
                source=str(source_value),
                contract_version=getattr(agent, "contract_version", None),
                requires=list(agent_contract.requires) if agent_contract else [],
                produces=list(agent_contract.produces) if agent_contract else [],
                input_schema_refs=dict(
                    getattr(agent, "input_schema_refs", {}) or {}
                ),
                output_schema_refs=dict(
                    getattr(agent, "output_schema_refs", {}) or {}
                ),
                agent_contract=agent_contract,
            )
        )
    return cards


def route_task(
    task_profile: TaskProfile,
    agent_cards: list[AgentCard],
    *,
    authorized_agent_ids: set[str],
    workflow_id: str,
    top_k: int = 5,
) -> RoutingDecision:
    candidates: list[RoutingCandidate] = []
    excluded: list[ExcludedAgent] = []
    composite_task = len(_normalized(task_profile.expected_capabilities)) > 1

    for card in agent_cards:
        if card.agent_id not in authorized_agent_ids:
            excluded.append(
                ExcludedAgent(
                    agent_id=card.agent_id,
                    reason="当前用户无权访问该 Agent",
                    reason_code="PERMISSION_DENIED",
                )
            )
            continue
        if card.status != "ONLINE":
            excluded.append(
                ExcludedAgent(
                    agent_id=card.agent_id,
                    reason="Agent 当前不在线",
                    reason_code="AGENT_UNAVAILABLE",
                )
            )
            continue
        if (
            card.supported_actions
            and task_profile.action not in card.supported_actions
            and task_profile.action != "read"
            and not composite_task
        ):
            excluded.append(
                ExcludedAgent(
                    agent_id=card.agent_id,
                    reason=f"不支持动作 {task_profile.action}",
                    reason_code="ACTION_UNSUPPORTED",
                )
            )
            continue
        if (
            not composite_task
            and RISK_LEVEL.get(task_profile.risk_level, 1) > RISK_LEVEL.get(card.risk_ceiling, 1)
        ):
            excluded.append(
                ExcludedAgent(
                    agent_id=card.agent_id,
                    reason=f"任务风险 {task_profile.risk_level} 超过 Agent 上限 {card.risk_ceiling}",
                    reason_code="RISK_CEILING_EXCEEDED",
                )
            )
            continue

        task_intents = _normalized(
            [task_profile.intent] + list(getattr(task_profile, "sub_intents", []) or [])
        )
        card_intents = _normalized(card.intents)
        subtasks = list(getattr(task_profile, "subtasks", []) or [])
        if composite_task and subtasks:
            intent_score = _coverage_score(subtasks, "intent", card.intents)
            capability_score = _coverage_score(subtasks, "expected_capabilities", card.capabilities)
            scenario_score = _coverage_score(subtasks, "scenario_tags", card.scenario_tags + card.intents)
        else:
            intent_score = 1.0 if task_intents & card_intents else 0.0
            capability_score = _any_overlap(task_profile.expected_capabilities, card.capabilities)
            scenario_score = _any_overlap(task_profile.scenario_tags, card.scenario_tags + card.intents)
        data_score = 1.0 if "general" in card.accepted_data_scopes else _overlap(
            task_profile.data_scope,
            card.accepted_data_scopes,
        )
        history_score = 0.5
        availability_score = 1.0
        total = min(
            1.0,
            0.35 * intent_score
            + 0.25 * capability_score
            + 0.15 * scenario_score
            + 0.10 * data_score
            + 0.10 * history_score
            + 0.05 * availability_score,
        )
        reasons = ["AUTHORIZED", "AGENT_ONLINE"]
        if composite_task and task_profile.action not in card.supported_actions:
            reasons.append("COMPOSITE_SUBTASK_ACTION")
        if intent_score:
            reasons.append("INTENT_MATCH")
        if capability_score:
            reasons.append("CAPABILITY_MATCH")
        if scenario_score:
            reasons.append("SCENARIO_MATCH")
        if total <= 0.15:
            excluded.append(
                ExcludedAgent(
                    agent_id=card.agent_id,
                    reason="与任务意图、能力和场景均无明显匹配",
                    reason_code="CAPABILITY_MISMATCH",
                )
            )
            continue
        candidates.append(
            RoutingCandidate(
                agent_id=card.agent_id,
                score=round(total, 4),
                reason_codes=reasons,
                score_breakdown={
                    "intent": round(intent_score, 4),
                    "capability": round(capability_score, 4),
                    "scenario": round(scenario_score, 4),
                    "data_scope": round(data_score, 4),
                    "history": history_score,
                    "availability": availability_score,
                },
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.agent_id))
    candidates = candidates[:top_k]
    top_score = candidates[0].score if candidates else 0.0
    composite_coverage = (
        _composite_candidate_coverage(task_profile, candidates, agent_cards)
        if composite_task
        else 0.0
    )
    if task_profile.missing_fields or getattr(task_profile, "needs_clarification", False):
        decision = "CLARIFY"
        reason_codes = (
            ["MISSING_REQUIRED_FIELDS"]
            if task_profile.missing_fields
            else ["INTENT_CLARIFICATION_REQUIRED"]
        )
    elif composite_task and composite_coverage >= 0.80:
        decision = "DISPATCH"
        reason_codes = ["COMPOSITE_ROUTE_COVERED"]
    elif top_score >= 0.80:
        decision = "DISPATCH"
        reason_codes = ["HIGH_CONFIDENCE_ROUTE"]
    elif top_score >= 0.55:
        # Agent 匹配分数不是用户缺失信息，不能触发没有具体问题的追问。
        # 追问只由 ClarificationAnalyzer 根据任务字段契约产生。
        decision = "DISPATCH"
        reason_codes = ["CAPABLE_ROUTE"]
    else:
        decision = "REJECT"
        reason_codes = ["NO_CAPABLE_AGENT"]

    digest = hashlib.sha256(f"{workflow_id}:{task_profile.task_id}".encode("utf-8")).hexdigest()[:16]
    selected = candidates[0].agent_id if decision == "DISPATCH" and candidates else None
    required_grants = []
    if selected:
        card = next((item for item in agent_cards if item.agent_id == selected), None)
        required_grants = card.required_grants if card else []
    return RoutingDecision(
        decision_id=f"route_{digest}",
        task_id=task_profile.task_id,
        selected_agent=selected,
        candidate_agents=candidates,
        decision=decision,
        confidence=max(top_score, composite_coverage) if composite_task else top_score,
        reason_codes=reason_codes,
        required_grants=required_grants,
        excluded_agents=excluded,
        trace_id=f"trace_{digest}",
    )
