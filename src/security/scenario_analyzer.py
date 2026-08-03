from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

try:
    from langchain_core.messages import HumanMessage, SystemMessage
except Exception:  # pragma: no cover
    HumanMessage = None  # type: ignore
    SystemMessage = None  # type: ignore

try:
    from src.llm.llm import get_llm_by_type
except Exception:  # pragma: no cover
    def get_llm_by_type(*_args, **_kwargs):  # type: ignore
        raise RuntimeError("LLM dependencies are not available")


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _normalize_token(token: str) -> str:
    return str(token or "").strip().lower()


def _score_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    inter = left.intersection(right)
    union = left.union(right)
    if not union:
        return 0.0
    return len(inter) / len(union)


def _ordered_union(*values: Any) -> list[str]:
    """Merge labels in source order with case-insensitive de-duplication."""
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _normalize_list(value):
            cleaned = item.strip()
            normalized = _normalize_token(cleaned)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(cleaned)
    return merged


def _merge_lists(primary: Any, fallback: Any) -> list[str]:
    """Keep model detail without dropping canonical security domains."""

    merged: list[str] = []
    seen: set[str] = set()
    for item in [*_normalize_list(fallback), *_normalize_list(primary)]:
        normalized = _normalize_token(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(item)
    return merged


def _merge_task_profile(fallback: Dict[str, Any], llm_result: Dict[str, Any]) -> Dict[str, Any]:
    merged = fallback.copy()
    merged.update(llm_result or {})

    fallback_type = str(fallback.get("task_type") or "GENERAL").upper()
    llm_type = str(merged.get("task_type") or fallback_type).upper()
    fallback_caps = {
        _normalize_token(item)
        for item in _normalize_list(fallback.get("expected_capabilities"))
        if _normalize_token(item)
    }
    llm_caps = {
        _normalize_token(item)
        for item in _normalize_list(merged.get("expected_capabilities"))
        if _normalize_token(item)
    }
    fallback_tags = {
        _normalize_token(item)
        for item in _normalize_list(fallback.get("scenario_tags"))
        if _normalize_token(item)
    }
    llm_tags = {
        _normalize_token(item)
        for item in _normalize_list(merged.get("scenario_tags"))
        if _normalize_token(item)
    }

    # Do not allow the LLM to downgrade a strongly identified domain task
    # back to GENERAL when the heuristic profile already found a specific domain.
    if fallback_type != "GENERAL" and llm_type == "GENERAL":
        merged["task_type"] = fallback_type
        merged["expected_capabilities"] = _normalize_list(fallback.get("expected_capabilities"))
        merged["scenario_tags"] = _normalize_list(fallback.get("scenario_tags"))
        merged["business_goal"] = fallback.get("business_goal") or merged.get("business_goal") or ""
        merged["operation_mode"] = fallback.get("operation_mode") or merged.get("operation_mode") or "read"
        merged["data_scope"] = fallback.get("data_scope") or merged.get("data_scope") or "targeted"
        merged["reason"] = "heuristic domain preserved over llm downgrade"
        return merged

    # If the LLM proposes a different domain with zero overlap, keep the heuristic
    # domain and enrich only non-domain fields.
    if (
        fallback_type != "GENERAL"
        and llm_type not in {"", fallback_type, "GENERAL"}
        and fallback_caps
        and llm_caps
        and fallback_caps.isdisjoint(llm_caps)
        and fallback_tags
        and llm_tags
        and fallback_tags.isdisjoint(llm_tags)
    ):
        merged["task_type"] = fallback_type
        merged["expected_capabilities"] = _normalize_list(fallback.get("expected_capabilities"))
        merged["scenario_tags"] = _normalize_list(fallback.get("scenario_tags"))
        merged["business_goal"] = fallback.get("business_goal") or merged.get("business_goal") or ""
        merged["reason"] = "heuristic domain preserved over conflicting llm result"
        return merged

    # Keep heuristic domain labels as authorization-compatible anchors while
    # retaining the LLM's finer-grained labels as additional scenario context.
    merged["scenario_tags"] = _ordered_union(
        fallback.get("scenario_tags"),
        merged.get("scenario_tags"),
    )
    merged["expected_capabilities"] = _ordered_union(
        fallback.get("expected_capabilities"),
        merged.get("expected_capabilities"),
    )
    merged["business_goal"] = merged.get("business_goal") or fallback.get("business_goal") or ""
    merged["operation_mode"] = merged.get("operation_mode") or fallback.get("operation_mode") or "read"
    merged["data_scope"] = merged.get("data_scope") or fallback.get("data_scope") or "targeted"
    merged["risk_profile"] = str(merged.get("risk_profile") or fallback.get("risk_profile") or "LOW").upper()
    merged["task_type"] = str(merged.get("task_type") or fallback_type).upper()
    return merged


def _merge_fit_result(fallback: Dict[str, Any], llm_result: Dict[str, Any]) -> Dict[str, Any]:
    merged = fallback.copy()
    merged.update(llm_result or {})

    fallback_fit = str(fallback.get("fit") or "uncertain").lower()
    llm_fit = str(merged.get("fit") or fallback_fit).lower()
    fallback_reason = fallback.get("reason") or ""
    merged_reason = str(merged.get("reason") or "")

    positive_reason_tokens = (
        "align",
        "aligned",
        "matches",
        "match",
        "correspond",
        "corresponds",
        "fits",
        "fit the target",
        "responsibility domain",
        "domain match",
    )
    negative_reason_tokens = (
        "does not align",
        "do not align",
        "inconsistent",
        "mismatch",
        "no overlap",
        "not match",
        "conflict",
    )

    # If the structured fit says uncertain but the natural-language reason is
    # explicitly affirmative, prefer match unless heuristics already found a mismatch.
    if (
        llm_fit == "uncertain"
        and fallback_fit != "mismatch"
        and merged_reason
        and any(token in merged_reason.lower() for token in positive_reason_tokens)
        and not any(token in merged_reason.lower() for token in negative_reason_tokens)
    ):
        merged["fit"] = "match"
        merged["confidence"] = max(float(merged.get("confidence", 0.5) or 0.5), 0.5)
        return merged

    # Preserve a strong heuristic mismatch/match signal over a weaker LLM override.
    if fallback_fit in {"match", "mismatch"} and llm_fit == "uncertain":
        merged["fit"] = fallback_fit
        merged["reason"] = fallback_reason or merged.get("reason") or ""
        merged["confidence"] = fallback.get("confidence", merged.get("confidence", 0.0))
        return merged

    # If heuristics already found a positive domain match, do not let the LLM
    # flip it to mismatch based on execution-phase wording alone.
    if fallback_fit == "match" and llm_fit == "mismatch":
        merged["fit"] = "match"
        merged["reason"] = fallback_reason or merged.get("reason") or ""
        merged["confidence"] = max(float(fallback.get("confidence", 0.6) or 0.6), 0.6)
        return merged

    if fallback_fit == "mismatch" and llm_fit == "match":
        merged["fit"] = "uncertain"
        merged["reason"] = merged.get("reason") or fallback_reason or "Conflicting fit signals"
        merged["confidence"] = min(float(merged.get("confidence", 0.35) or 0.35), 0.5)
        return merged

    return merged


class TaskScenarioProfile(BaseModel):
    task_type: str = "GENERAL"
    business_goal: str = ""
    data_scope: str = "targeted"
    operation_mode: str = "read"
    scenario_tags: list[str] = Field(default_factory=lambda: ["general"])
    expected_capabilities: list[str] = Field(default_factory=lambda: ["General"])
    risk_profile: str = "LOW"
    reason: str = ""


class ScenarioFitProfile(BaseModel):
    fit: str = "uncertain"
    confidence: float = 0.0
    reason: str = ""
    suggested_agent_domains: list[str] = Field(default_factory=list)
    suggested_tool_domains: list[str] = Field(default_factory=list)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _heuristic_task_profile(user_query: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    metadata = metadata or {}
    lowered = str(user_query or "").lower()
    task_type = "GENERAL"
    operation_mode = str(metadata.get("operation_mode", "") or "").lower()
    data_scope = str(metadata.get("data_scope", "") or "")
    tags: list[str] = []
    capabilities: list[str] = []

    if _contains_any(lowered, ("salary", "工资", "薪资")):
        task_type = "HR"
        capabilities.append("HR")
    elif _contains_any(
        lowered,
        ("email", "notify", "notification", "message", "mail", "邮件", "邮箱", "通知", "群发", "发送"),
    ):
        task_type = "COMMUNICATION"
        capabilities.append("Communication")
    elif _contains_any(
        lowered,
        ("salary", "employee", "hr", "leave", "travel", "personnel", "工资", "薪资", "员工", "人事", "请假", "出差"),
    ):
        task_type = "HR"
        capabilities.append("HR")
    elif _contains_any(lowered, ("risk", "credit", "compliance", "风控", "风险", "合规")):
        task_type = "RISK"
        capabilities.append("Risk")
    elif _contains_any(lowered, ("document", "report", "proof", "docx", "文档", "报告", "证明", "生成")):
        task_type = "DOCUMENT"
        capabilities.append("Document")
    elif _contains_any(lowered, ("research", "search", "crawl", "market", "调研", "搜索", "查询", "爬取", "市场")):
        task_type = "RESEARCH"
        capabilities.append("Research")

    if _contains_any(lowered, ("salary", "工资", "薪资")):
        tags.append("salary_query")
    if _contains_any(lowered, ("employee", "person", "员工", "人员")):
        tags.append("employee_info")
    if _contains_any(lowered, ("proof", "certificate", "证明")):
        tags.append("employee_proof")
        if "Document" not in capabilities:
            capabilities.append("Document")
    if _contains_any(lowered, ("email", "mail", "邮件", "发送", "通知")):
        tags.append("notification_send")
    if _contains_any(lowered, ("batch", "mass", "批量", "群发")):
        tags.append("mass_notification")
    if _contains_any(lowered, ("risk", "credit", "风险", "风控")):
        tags.append("risk_analysis")
    if _contains_any(lowered, ("research", "search", "market", "调研", "搜索", "市场")):
        tags.append("market_research")

    if not operation_mode:
        if _contains_any(lowered, ("send", "email", "mail", "通知", "发送", "邮件")):
            operation_mode = "send"
        elif _contains_any(lowered, ("create", "generate", "report", "document", "proof", "生成", "报告", "文档", "证明")):
            operation_mode = "generate"
        elif _contains_any(lowered, ("save", "submit", "write", "update", "保存", "提交", "写入", "更新")):
            operation_mode = "write"
        else:
            operation_mode = "read"

    if not data_scope:
        if _contains_any(lowered, ("all employees", "all staff", "company-wide", "全员", "全公司")):
            data_scope = "company"
        elif _contains_any(lowered, ("department", "team", "本部门", "部门")):
            data_scope = "department"
        elif _contains_any(lowered, ("my", "myself", "本人", "我的")):
            data_scope = "self"
        else:
            data_scope = "targeted"

    return TaskScenarioProfile(
        task_type=task_type,
        business_goal=str(metadata.get("business_goal") or user_query or ""),
        data_scope=data_scope,
        operation_mode=operation_mode,
        scenario_tags=tags or ["general"],
        expected_capabilities=capabilities or ["General"],
        risk_profile=str(metadata.get("risk_profile", "LOW")).upper(),
        reason="heuristic fallback",
    ).model_dump()


def _heuristic_fit(
    task_profile: Dict[str, Any],
    *,
    object_id: str,
    object_attrs: Dict[str, Any],
) -> Dict[str, Any]:
    expected_capabilities = {
        _normalize_token(item) for item in _normalize_list(task_profile.get("expected_capabilities"))
    }
    scenario_tags = {
        _normalize_token(item) for item in _normalize_list(task_profile.get("scenario_tags"))
    }
    object_capabilities = {
        _normalize_token(item) for item in _normalize_list(object_attrs.get("expected_capabilities"))
    }
    object_tags = {
        _normalize_token(item) for item in _normalize_list(object_attrs.get("scenario_tags"))
    }
    task_type = _normalize_token(task_profile.get("task_type"))
    object_domain = _normalize_token(object_attrs.get("capability_domain"))
    object_department = _normalize_token(object_attrs.get("department_domain"))
    sensitivity = _normalize_token(object_attrs.get("sensitivity"))

    cap_overlap = _score_overlap(expected_capabilities, object_capabilities)
    tag_overlap = _score_overlap(scenario_tags, object_tags)
    strong_cap_mismatch = bool(expected_capabilities and object_capabilities and cap_overlap == 0.0)
    strong_tag_mismatch = bool(scenario_tags and object_tags and tag_overlap == 0.0)

    fit = "uncertain"
    reason = "Insufficient scenario information"

    # Strong mismatch only when both structured signals disagree, or one signal
    # disagrees and the target is high sensitivity with a clearly different domain.
    if strong_cap_mismatch and strong_tag_mismatch:
        fit = "mismatch"
        reason = (
            f"Task expects capabilities {sorted(expected_capabilities)} and tags {sorted(scenario_tags)}, "
            f"but target {object_id} provides capabilities {sorted(object_capabilities)} and tags {sorted(object_tags)}"
        )
    elif strong_cap_mismatch and sensitivity in {"high", "critical"} and object_domain and task_type:
        if object_domain != task_type and object_department != task_type:
            fit = "mismatch"
            reason = (
                f"High-sensitivity target {object_id} belongs to domain {object_domain or object_department}, "
                f"while task type is {task_type} with capabilities {sorted(expected_capabilities)}"
            )
    elif cap_overlap > 0.0 or tag_overlap > 0.0:
        fit = "match"
        if cap_overlap >= tag_overlap:
            reason = "Capability domain matches task profile"
        else:
            reason = "Scenario tags match target profile"
    elif expected_capabilities or scenario_tags:
        fit = "uncertain"
        reason = (
            f"Task profile does not provide strong overlap with target {object_id}; "
            "keeping scenario decision conservative"
        )

    return ScenarioFitProfile(
        fit=fit,
        confidence=0.6 if fit == "match" else 0.8 if fit == "mismatch" else 0.35,
        reason=reason,
        suggested_agent_domains=_normalize_list(object_attrs.get("capability_domain")),
        suggested_tool_domains=_normalize_list(object_attrs.get("capability_domain")),
    ).model_dump()


async def analyze_task_context(user_query: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    metadata = metadata or {}
    fallback = _heuristic_task_profile(user_query, metadata)

    if HumanMessage is None or SystemMessage is None:
        return fallback

    try:
        llm = get_llm_by_type("basic")
        structured = llm.with_structured_output(TaskScenarioProfile)
        prompt = (
            "You are the scenario classifier for SuperAgent security. "
            "Return only a structured task scenario profile for downstream S-ABAC evaluation."
        )
        user_msg = (
            f"user_query: {user_query}\n"
            f"known_metadata: {metadata}\n"
            "Requirements:\n"
            "- task_type must be one of GENERAL/HR/COMMUNICATION/RISK/DOCUMENT/RESEARCH\n"
            "- operation_mode should prefer read/generate/write/send/delegate\n"
            "- expected_capabilities should be responsibility-domain labels\n"
        )
        result = await structured.ainvoke(
            [SystemMessage(content=prompt), HumanMessage(content=user_msg)]
        )
        raw = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        return _merge_task_profile(fallback, raw)
    except Exception:
        return fallback


async def analyze_object_fit(
    user_query: str,
    *,
    object_id: str,
    object_type: str,
    object_attrs: Dict[str, Any],
    task_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    task_profile = task_profile or _heuristic_task_profile(user_query, {})
    fallback = _heuristic_fit(task_profile, object_id=object_id, object_attrs=object_attrs)

    if HumanMessage is None or SystemMessage is None:
        return fallback

    try:
        llm = get_llm_by_type("basic")
        structured = llm.with_structured_output(ScenarioFitProfile)
        prompt = (
            "You are the scenario-fit evaluator for SuperAgent security. "
            "Only judge whether the current task scenario matches the target object domain. "
            "Do not make the final authorization decision."
        )
        user_msg = (
            f"user_query: {user_query}\n"
            f"task_profile: {task_profile}\n"
            f"target_id: {object_id}\n"
            f"target_type: {object_type}\n"
            f"target_attributes: {object_attrs}\n"
            "Output fit as match, mismatch, or uncertain. "
            "If the task domain and target responsibility domain are inconsistent, return mismatch."
        )
        result = await structured.ainvoke(
            [SystemMessage(content=prompt), HumanMessage(content=user_msg)]
        )
        raw = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        return _merge_fit_result(fallback, raw)
    except Exception:
        return fallback
