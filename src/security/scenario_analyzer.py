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
        item.lower() for item in _normalize_list(task_profile.get("expected_capabilities"))
    }
    scenario_tags = {item.lower() for item in _normalize_list(task_profile.get("scenario_tags"))}
    object_capabilities = {
        item.lower() for item in _normalize_list(object_attrs.get("expected_capabilities"))
    }
    object_tags = {item.lower() for item in _normalize_list(object_attrs.get("scenario_tags"))}

    fit = "uncertain"
    reason = "Insufficient scenario information"
    if expected_capabilities and object_capabilities:
        if expected_capabilities.isdisjoint(object_capabilities):
            fit = "mismatch"
            reason = (
                f"Task expects capabilities {sorted(expected_capabilities)}, "
                f"but target {object_id} provides {sorted(object_capabilities)}"
            )
        else:
            fit = "match"
            reason = "Capability domain matches task profile"

    if fit != "mismatch" and scenario_tags and object_tags:
        if scenario_tags.isdisjoint(object_tags):
            fit = "mismatch"
            reason = (
                f"Task tags {sorted(scenario_tags)} do not align with target tags "
                f"{sorted(object_tags)}"
            )
        else:
            fit = "match"
            reason = "Scenario tags match target profile"

    return ScenarioFitProfile(
        fit=fit,
        confidence=0.55 if fit == "match" else 0.85 if fit == "mismatch" else 0.25,
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
        merged = fallback.copy()
        merged.update(result.model_dump() if hasattr(result, "model_dump") else dict(result))
        return merged
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
        merged = fallback.copy()
        merged.update(result.model_dump() if hasattr(result, "model_dump") else dict(result))
        return merged
    except Exception:
        return fallback
