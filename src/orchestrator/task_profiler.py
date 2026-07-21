from __future__ import annotations

import re
from typing import Any

from src.contracts import TaskProfile
from src.orchestrator.intent_catalog import INTENT_CATALOG, INTENT_LABELS
from src.orchestrator.intent_recognition import (
    HybridIntentRecognizer,
    IntentCandidate,
    IntentRecognitionResult,
    SemanticIntentProvider,
    extract_entities,
    segment_query,
)


# 兼容可能仍从 task_profiler 导入旧常量的代码；数据源已统一到 INTENT_CATALOG。
DOMAIN_RULES = tuple(
    {
        "task_type": item["task_type"],
        "intent": name,
        "keywords": tuple(item.get("keywords") or ()),
        "capabilities": tuple(item.get("capabilities") or ()),
        "tags": tuple(item.get("tags") or ()),
        "scope": tuple(item.get("scope") or ()),
    }
    for name, item in INTENT_CATALOG.items()
    if item.get("keywords")
)
EXTRA_KEYWORDS = {
    name: tuple(item.get("keywords") or ()) for name, item in INTENT_CATALOG.items()
}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _merge_entities(rule_entities: dict[str, Any], semantic_entities: dict[str, Any]) -> dict[str, Any]:
    result = dict(rule_entities)
    aliases = {
        "person": "employee_name",
        "person_name": "employee_name",
        "employee": "employee_name",
        "recipient_name": "recipient",
    }
    allowed = {"people", "employee_name", "recipient", "time", "count", "document_type", "business_object"}
    document_aliases = {
        "收入证明": "income_proof",
        "在职证明": "employment_certificate",
        "请假申请书": "leave_application",
        "请假书": "leave_application",
        "分析报告": "analysis_report",
        "报告": "report",
    }
    for key, value in semantic_entities.items():
        key = aliases.get(key, key)
        if key not in allowed:
            continue
        if value in (None, "", []):
            continue
        if key == "people" and isinstance(value, list):
            result[key] = _unique(list(result.get(key) or []) + [str(item) for item in value])
        elif key == "document_type":
            result[key] = result.get(key) or document_aliases.get(str(value), value)
        elif key in result:
            continue
        else:
            result[key] = value
    employee_name = str(result.get("employee_name") or "")
    if employee_name:
        result["people"] = _unique([employee_name] + list(result.get("people") or []))
    return result


def _candidate(
    name: str,
    *,
    provenance: str,
    evidence: str,
    confidence: float = 0.78,
) -> IntentCandidate:
    return IntentCandidate(
        name=name,
        confidence=confidence,
        source="rule",
        provenance=provenance,
        text_span=None,
        evidence=[evidence],
    )


def _insert_before(
    candidates: list[IntentCandidate],
    before_names: set[str],
    item: IntentCandidate,
) -> None:
    if any(candidate.name == item.name and not candidate.negated for candidate in candidates):
        return
    index = next(
        (i for i, candidate in enumerate(candidates) if candidate.name in before_names and not candidate.negated),
        len(candidates),
    )
    candidates.insert(index, item)


def _enrich_inferred_dependencies(
    result: IntentRecognitionResult,
    entities: dict[str, Any],
) -> IntentRecognitionResult:
    """补充业务执行必需的前置意图，并明确标记为 inferred。"""
    candidates = [item.model_copy(deep=True) for item in result.intents]
    executable_names = {item.name for item in candidates if not item.negated}
    document_type = str(entities.get("document_type") or "")
    employee_name = str(entities.get("employee_name") or "")
    employee_documents = {
        "leave_application", "income_proof", "employment_certificate", "application_letter"
    }
    has_document_goal = bool(
        executable_names & {"document_generation", "report_generation"}
    )
    if employee_name and document_type in employee_documents and has_document_goal:
        _insert_before(
            candidates,
            {"salary_query", "document_generation", "report_generation"},
            _candidate(
                "employee_information_query",
                provenance="inferred",
                evidence=f"生成 {document_type} 需要员工基础数据",
            ),
        )
    if document_type == "income_proof" and "document_generation" in executable_names:
        _insert_before(
            candidates,
            {"document_generation"},
            _candidate(
                "salary_query",
                provenance="inferred",
                evidence="生成收入证明需要薪资数据",
            ),
        )

    # 相同意图只保留一项；语义来源优先，显式来源优先于推导来源。
    deduplicated: list[IntentCandidate] = []
    index_by_name: dict[str, int] = {}
    for item in candidates:
        if item.name not in index_by_name:
            index_by_name[item.name] = len(deduplicated)
            deduplicated.append(item)
            continue
        existing_index = index_by_name[item.name]
        existing = deduplicated[existing_index]
        if existing.provenance == "inferred" and item.provenance == "explicit":
            deduplicated[existing_index] = item
        elif item.source in {"semantic", "rule+semantic"} and existing.source == "rule":
            deduplicated[existing_index] = item
    result.intents = deduplicated
    return result


def _annotate_rule_conditions(
    query: str,
    result: IntentRecognitionResult,
) -> IntentRecognitionResult:
    """在语义结果缺少条件结构时，保留显式“如果…就…”边界。"""
    match = re.search(r"如果(.+?)(?:，|,)?(?:就|则)(.+?)(?:否则|$)", query)
    if not match:
        return result
    condition_text, consequence_text = match.group(1).strip(), match.group(2).strip()
    condition_sources = [
        item.name
        for item in result.intents
        if not item.negated
        and any(str(evidence) in condition_text for evidence in item.evidence)
    ]
    for item in result.intents:
        if item.name in condition_sources:
            item.condition = None
            item.condition_on = []
    for item in result.intents:
        if item.negated:
            continue
        if item.name in condition_sources:
            continue
        if any(str(evidence) in consequence_text for evidence in item.evidence):
            if item.name == "message_or_email_send":
                continue
            item.condition = condition_text
            item.condition_on = [name for name in condition_sources if name != item.name]
    return result


def _segment_for_candidate(
    candidate: IntentCandidate,
    segments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    span = str(candidate.text_span or "").strip().lower()
    evidence = [str(item).lower() for item in candidate.evidence]
    for segment in segments:
        text = str(segment.get("text") or "").lower()
        if (span and span in text) or any(item and item in text for item in evidence):
            return segment
    return segments[0] if segments and candidate.provenance == "explicit" else None


def _action_for_intent(intent: str, query: str, candidate: IntentCandidate | None = None) -> str:
    normalized = query.lower()
    if intent == "message_or_email_send":
        return "send"
    if intent in {"document_generation", "report_generation"}:
        return "generate"
    if intent == "meeting_arrangement":
        if re.search(r"取消|删除|cancel|delete", normalized):
            return "delete"
        return "write"
    if intent in {"schedule_management", "travel_service"}:
        evidence = " ".join(candidate.evidence) if candidate else ""
        if intent == "schedule_management" and any(
            token in evidence for token in ("日程", "有没有时间", "有空")
        ):
            return "read"
        if re.search(r"创建|新增|修改|更新|保存|安排|create|update|write", normalized):
            return "write"
    return str(INTENT_CATALOG.get(intent, {}).get("default_action") or "read")


def _overall_action(candidates: list[IntentCandidate], query: str) -> tuple[str, bool]:
    actions = [_action_for_intent(item.name, query, item) for item in candidates if not item.negated]
    if "delete" in actions:
        return "delete", True
    if "send" in actions:
        return "send", True
    if "generate" in actions:
        return "generate", False
    if "write" in actions:
        return "write", False
    return "read", False


def _dependency_names(intent: str, prior_names: list[str]) -> list[str]:
    prior = set(prior_names)
    if intent in {"salary_query", "leave_record_query"}:
        return [name for name in ("employee_information_query",) if name in prior]
    if intent == "document_generation":
        return [name for name in ("employee_information_query", "salary_query") if name in prior]
    if intent == "report_generation":
        upstream = [
            name
            for name in (
                "employee_information_query",
                "salary_query",
                "leave_record_query",
                "information_research",
                "risk_analysis",
                "knowledge_lookup",
            )
            if name in prior
        ]
        return upstream
    if intent == "meeting_arrangement":
        return [name for name in ("schedule_management",) if name in prior]
    if intent == "message_or_email_send":
        upstream = [
            name for name in ("document_generation", "report_generation", "meeting_arrangement") if name in prior
        ]
        return upstream or prior_names[-1:]
    return []


def _goal_for_intent(
    intent: str,
    entities: dict[str, Any],
    explicit_text: str = "",
) -> str:
    employee = str(entities.get("employee_name") or "")
    document_type = str(entities.get("document_type") or "")
    if intent == "employee_information_query" and employee:
        return f"查询{employee}员工基础信息"
    if intent == "salary_query" and employee:
        return f"查询{employee}薪资或收入信息"
    if intent == "leave_record_query" and employee:
        return f"查询{employee}请假记录"
    if intent == "report_generation" and explicit_text:
        return explicit_text
    if intent == "document_generation" and employee:
        labels = {
            "leave_application": "请假申请书",
            "income_proof": "收入证明",
            "employment_certificate": "在职证明",
        }
        if document_type in labels:
            return f"生成{employee}{labels[document_type]}"
    return INTENT_LABELS.get(intent, intent)


def _build_subtasks(
    candidates: list[IntentCandidate],
    query: str,
    segments: list[dict[str, Any]],
    entities: dict[str, Any],
) -> list[dict[str, Any]]:
    subtasks: list[dict[str, Any]] = []
    name_to_id: dict[str, str] = {}
    executable = [item for item in candidates if not item.negated]
    for candidate in executable:
        definition = INTENT_CATALOG.get(candidate.name)
        if not definition:
            continue
        segment = _segment_for_candidate(candidate, segments)
        dependency_names = _dependency_names(candidate.name, list(name_to_id))
        dependency_ids = [name_to_id[name] for name in dependency_names if name in name_to_id]
        condition_ids = [name_to_id[name] for name in candidate.condition_on if name in name_to_id]
        inherited_conditional_ids = [
            str(item["id"])
            for item in subtasks
            if str(item.get("id")) in dependency_ids
            and item.get("execution_policy") == "conditional"
        ]
        effective_condition = candidate.condition
        effective_condition_ids = condition_ids
        if candidate.name == "message_or_email_send" and inherited_conditional_ids:
            effective_condition = effective_condition or "前置条件成立且会议创建成功"
            effective_condition_ids = _unique(condition_ids + inherited_conditional_ids)
        subtask_id = f"subtask_{len(subtasks) + 1}"
        subtasks.append(
            {
                "id": subtask_id,
                "intent": candidate.name,
                "task_type": str(definition["task_type"]),
                "goal": _goal_for_intent(
                    candidate.name,
                    entities,
                    str(segment.get("text") or "") if segment else "",
                ),
                "action": _action_for_intent(candidate.name, query, candidate),
                "expected_capabilities": list(definition.get("capabilities") or []),
                "scenario_tags": list(definition.get("tags") or []),
                "data_scope": list(definition.get("scope") or []),
                "depends_on": _unique(dependency_ids + condition_ids),
                "segment_id": str(segment.get("id") or "") if segment else "",
                "text_span": candidate.text_span or (str(segment.get("text") or "") if segment else ""),
                "source": candidate.source,
                "provenance": candidate.provenance,
                "confidence": candidate.confidence,
                "evidence": candidate.evidence,
                "condition": effective_condition,
                "condition_on": effective_condition_ids,
                "execution_policy": "conditional" if effective_condition else "always",
            }
        )
        name_to_id[candidate.name] = subtask_id
    return subtasks


def _build_intent_nodes(
    candidates: list[IntentCandidate],
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for candidate in candidates:
        segment = _segment_for_candidate(candidate, segments)
        nodes.append(
            {
                "name": candidate.name,
                "label": INTENT_LABELS.get(candidate.name, candidate.name),
                "confidence": round(candidate.confidence, 2),
                "text_span": candidate.text_span,
                "segment_id": str(segment.get("id") or "") if segment else "",
                "source": candidate.source,
                "provenance": candidate.provenance,
                "evidence": candidate.evidence,
                "negated": candidate.negated,
                "condition": candidate.condition,
                "condition_on": candidate.condition_on,
            }
        )
    return nodes


def _is_ambiguous(query: str) -> bool:
    return any(
        phrase in query.lower()
        for phrase in ("处理一下", "搞一下", "弄一下", "看着办", "随便", "帮我看看这个", "handle it", "do it")
    )


def _calculate_confidence(
    candidates: list[IntentCandidate],
    *,
    missing_fields: list[str],
    ambiguous: bool,
    irreversible: bool,
    degraded: bool,
) -> float:
    executable = [item for item in candidates if not item.negated]
    if not executable:
        return 0.4
    base = 0.89 if len(executable) == 1 else 0.94
    average = sum(item.confidence for item in executable) / len(executable)
    score = 0.55 * base + 0.45 * average
    if missing_fields:
        score -= min(0.32, 0.16 * len(missing_fields))
    if ambiguous:
        score -= 0.14
    if irreversible:
        score -= 0.03
    if degraded:
        score -= 0.02
    return round(max(0.35, min(0.97, score)), 2)


def _confidence_factors(
    result: IntentRecognitionResult,
    executable: list[IntentCandidate],
    subtasks: list[dict[str, Any]],
    entities: dict[str, Any],
    missing_fields: list[str],
) -> list[str]:
    factors: list[str] = []
    source_counts: dict[str, int] = {}
    for item in result.intents:
        source_counts[item.source] = source_counts.get(item.source, 0) + 1
    if source_counts:
        factors.append(
            "识别来源：" + "、".join(f"{source} {count}" for source, count in source_counts.items())
        )
    if executable:
        explicit_count = sum(item.provenance == "explicit" for item in executable)
        inferred_count = sum(item.provenance == "inferred" for item in executable)
        factors.append(f"可执行意图 {len(executable)} 个：显式 {explicit_count}、推导 {inferred_count}")
    negated = [item.name for item in result.intents if item.negated]
    if negated:
        factors.append(f"已识别并阻止否定动作：{', '.join(negated)}")
    if subtasks:
        conditional_count = sum(item.get("execution_policy") == "conditional" for item in subtasks)
        factors.append(f"生成 {len(subtasks)} 个子任务，其中条件任务 {conditional_count} 个")
    if entities:
        factors.append(f"抽取实体字段：{', '.join(entities)}")
    if missing_fields:
        factors.append(f"缺少关键字段：{', '.join(missing_fields)}")
    if result.ambiguities:
        factors.append(f"存在 {len(result.ambiguities)} 个待确认歧义")
    if result.degraded:
        factors.append("语义服务不可用，本次已按配置降级为规则识别")
    return factors


async def profile_task(
    user_query: str,
    *,
    task_id: str,
    metadata: dict[str, Any] | None = None,
    recognition_mode: str | None = None,
    semantic_provider: SemanticIntentProvider | None = None,
) -> TaskProfile:
    """通过规则、语义或混合识别生成向后兼容的结构化任务画像。"""
    metadata = metadata or {}
    segments = segment_query(user_query)
    recognizer = HybridIntentRecognizer(
        mode=recognition_mode,
        semantic_provider=semantic_provider,
    )
    recognition = await recognizer.recognize(user_query)
    entities = _merge_entities(extract_entities(user_query), recognition.entities)
    recognition.entities = entities
    recognition = _enrich_inferred_dependencies(recognition, entities)
    recognition = _annotate_rule_conditions(user_query, recognition)
    executable = [item for item in recognition.intents if not item.negated]

    subtasks = _build_subtasks(executable, user_query, segments, entities)
    intent_nodes = _build_intent_nodes(recognition.intents, segments)
    legacy_intent = executable[0].name if executable else "general_assistance"
    sub_intents = [item.name for item in executable]
    task_types = _unique(
        [str(INTENT_CATALOG[item.name]["task_type"]) for item in executable if item.name in INTENT_CATALOG]
    )
    task_type = "COMPOSITE" if len(task_types) > 1 else task_types[0] if task_types else "GENERAL"

    capabilities: list[str] = []
    tags: list[str] = []
    scopes: list[str] = []
    for item in executable:
        definition = INTENT_CATALOG.get(item.name, {})
        capabilities.extend(definition.get("capabilities") or [])
        tags.extend(definition.get("tags") or [])
        scopes.extend(definition.get("scope") or [])

    action, irreversible = _overall_action(executable, user_query)
    missing_fields: list[str] = []
    if any(item.name == "message_or_email_send" for item in executable) and not entities.get("recipient"):
        missing_fields.append("recipient")
    clarification_questions = list(recognition.clarification_questions)
    if "recipient" in missing_fields:
        clarification_questions.append("请提供发送任务的收件人。")
    clarification_questions = _unique(clarification_questions)
    needs_clarification = recognition.needs_clarification or bool(missing_fields)

    risk_level = "HIGH" if irreversible else "MEDIUM" if action in {"write", "generate"} else "LOW"
    confidence = _calculate_confidence(
        executable,
        missing_fields=missing_fields,
        ambiguous=_is_ambiguous(user_query),
        irreversible=irreversible,
        degraded=recognition.degraded,
    )
    confidence_factors = _confidence_factors(
        recognition, executable, subtasks, entities, missing_fields
    )
    recognition_payload = recognition.model_dump()
    recognition_payload["needs_clarification"] = needs_clarification
    recognition_payload["clarification_questions"] = clarification_questions

    return TaskProfile(
        task_id=task_id,
        intent=legacy_intent,
        task_type=task_type,
        business_goal=user_query,
        action=action,
        entities=entities,
        data_scope=_unique(scopes) or ["general"],
        scenario_tags=_unique(tags) or ["general"],
        expected_capabilities=_unique(capabilities) or ["General"],
        risk_level=risk_level,
        irreversible=irreversible,
        constraints=list(metadata.get("constraints") or []),
        missing_fields=missing_fields,
        confidence=confidence,
        reason=(
            "hybrid_intent_fusion"
            if recognition.mode == "hybrid" and not recognition.degraded
            else "semantic_intent_recognition"
            if recognition.mode == "semantic" and not recognition.degraded
            else "rule_intent_recognition"
        ),
        sub_intents=sub_intents,
        subtasks=subtasks,
        is_composite=len(executable) > 1,
        segments=segments,
        intent_nodes=intent_nodes,
        confidence_factors=confidence_factors,
        primary_goal_intent=recognition.primary_intent,
        ambiguities=recognition.ambiguities,
        needs_clarification=needs_clarification,
        clarification_questions=clarification_questions,
        recognition_mode=recognition.mode,
        recognition_degraded=recognition.degraded,
        recognition=recognition_payload,
    )
