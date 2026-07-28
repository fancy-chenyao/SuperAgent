from __future__ import annotations

from typing import Any

from src.contracts import ContextReference, ResolvedRequest
from src.orchestrator.intent_recognition import extract_entities, is_person_candidate


_ALLOWED_ENTITY_KEYS = {
    "people",
    "employee_name",
    "employee_id",
    "recipient",
    "location",
    "time",
    "count",
    "document_type",
    "business_object",
    "communication.content",
}

def _clean_entities(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if key in _ALLOWED_ENTITY_KEYS and item not in (None, "", [])
    }


def _canonical_missing_field(field: str) -> str:
    normalized = str(field or "").strip().lower()
    if any(token in normalized for token in (
        "employee", "person", "target", "participant",
        "员工", "姓名", "人员", "参会人",
    )):
        return "employee_name"
    if any(token in normalized for token in ("recipient", "收件", "接收人")):
        return "recipient"
    if any(token in normalized for token in ("location", "地点", "城市", "地区")):
        return "location"
    if any(token in normalized for token in ("time", "date", "时间", "日期")):
        return "time"
    if any(token in normalized for token in ("document_type", "文档类型")):
        return "document_type"
    if any(token in normalized for token in (
        "communication.content", "content", "正文", "内容",
    )):
        return "communication.content"
    if any(token in normalized for token in (
        "task_goal", "expected_result", "document.source",
        "任务目标", "预期结果", "内容来源",
    )):
        return normalized
    return str(field or "").strip()


def _bind_clarification_answer(
    answer: str,
    pending_fields: list[str],
) -> tuple[dict[str, Any], list[ContextReference], list[str]]:
    answer_entities = extract_entities(answer)
    overrides: dict[str, Any] = {}
    references: list[ContextReference] = []
    unresolved: list[str] = []
    resolved_message_override = ""

    canonical_fields = [
        (str(item), _canonical_missing_field(str(item)))
        for item in pending_fields
    ]
    single_field = len(canonical_fields) == 1

    for original_field, field in canonical_fields:
        value: Any = None

        if field == "employee_name":
            value = answer_entities.get("employee_name")
            if not value and single_field and is_person_candidate(answer):
                value = answer.strip()
            if value:
                overrides["employee_name"] = value
                overrides["people"] = [value]
        elif field == "recipient":
            value = answer_entities.get("recipient")
            if not value and single_field:
                value = answer.strip()
            if value:
                overrides["recipient"] = value
        elif field == "location":
            value = answer_entities.get("location")
            if not value and single_field:
                value = answer.strip()
            if value:
                overrides["location"] = value
        elif field == "time":
            value = answer_entities.get("time")
            if not value and single_field:
                value = answer.strip()
            if value:
                overrides["time"] = value
        elif field == "document_type":
            value = answer_entities.get("document_type")
            if value:
                overrides["document_type"] = value
        elif field == "communication.content" and single_field:
            value = answer.strip()
            overrides[field] = value
        elif field in {"task_goal", "expected_result", "document.source"}:
            value = answer.strip()
            if value:
                # 这些字段本身就是自然语言任务描述，不伪装成实体。
                # task_goal 表示原请求没有可执行目标，直接以用户补充为本轮请求；
                # 其余字段保留原目标，在调用方追加为同一请求的补充限定。
                resolved_message_override = value

        if value not in (None, "", []):
            references.append(
                ContextReference(
                    mention=answer,
                    kind="clarification",
                    key=field,
                    value=value,
                    source="pending_clarification",
                    confidence=1.0,
                )
            )
        else:
            unresolved.append(original_field)

    if resolved_message_override:
        overrides["__resolved_message__"] = resolved_message_override
    return overrides, references, unresolved


def resolve_conversation_request(
    *,
    current_message: str,
    turn_type: str = "request",
    clarification_context: dict[str, Any] | None = None,
    context_entities: dict[str, Any] | None = None,
    context_artifacts: list[dict[str, Any]] | None = None,
) -> ResolvedRequest:
    """把当前对话轮次解析为独立画像输入，并只继承明确相关的上下文。"""

    raw_message = str(current_message or "").strip()
    clarification_context = (
        clarification_context if isinstance(clarification_context, dict) else {}
    )
    available_artifacts = [
        dict(item)
        for item in (context_artifacts or [])
        if isinstance(item, dict)
    ]
    # 这里只提供最近的结构化候选；是否发生指代由语义识别器判断。
    artifact_inputs = available_artifacts[-3:]

    if turn_type == "clarification_answer":
        base_query = str(
            clarification_context.get("base_query")
            or clarification_context.get("resolved_message")
            or ""
        ).strip()
        base_entities = _clean_entities(clarification_context.get("entities"))
        pending_fields = [
            str(item)
            for item in clarification_context.get("missing_fields") or []
            if str(item).strip()
        ]
        overrides, references, unresolved = _bind_clarification_answer(
            raw_message,
            pending_fields,
        )
        clarified_text = str(overrides.pop("__resolved_message__", "") or "").strip()
        if clarified_text:
            if any(
                _canonical_missing_field(item) == "task_goal"
                for item in pending_fields
            ):
                base_query = clarified_text
            else:
                base_query = f"{base_query}，{clarified_text}".strip("，")
        merged_entities = {**base_entities, **overrides}
        return ResolvedRequest(
            raw_message=raw_message,
            resolved_message=base_query or raw_message,
            turn_type="clarification_answer",
            entity_overrides=merged_entities,
            artifact_inputs=artifact_inputs,
            context_references=references,
            unresolved_fields=unresolved,
        )

    return ResolvedRequest(
        raw_message=raw_message,
        resolved_message=raw_message,
        turn_type="request",
        entity_overrides={},
        artifact_inputs=artifact_inputs,
        context_references=[],
    )
