from __future__ import annotations

from typing import Any

from src.contracts import TaskProfile
from src.security.scenario_analyzer import analyze_task_context


DOMAIN_RULES = (
    {
        "task_type": "HR",
        "intent": "salary_query",
        "keywords": ("薪资", "工资", "薪酬", "salary", "payroll"),
        "capabilities": ("HR",),
        "tags": ("salary_query", "hr_service"),
        "scope": ("employee.salary",),
    },
    {
        "task_type": "HR",
        "intent": "employee_information_query",
        "keywords": ("员工", "人员", "人事", "花名册", "employee", "personnel", "hr"),
        "capabilities": ("HR",),
        "tags": ("employee_info", "hr_service"),
        "scope": ("employee.basic_profile",),
    },
    {
        "task_type": "LEARNING",
        "intent": "programming_learning",
        "keywords": ("学习java", "学习 java", "学java", "学 java", "学习python", "编程学习", "技术学习", "java", "python教程"),
        "capabilities": ("Engineering", "Learning"),
        "tags": ("programming_learning", "technology_support"),
        "scope": ("learning.public_content",),
    },
    {
        "task_type": "COMMUNICATION",
        "intent": "message_or_email_send",
        "keywords": ("发邮件", "发送邮件", "通知", "群发", "站内信", "email", "mail", "message"),
        "capabilities": ("Communication",),
        "tags": ("notification_send",),
        "scope": ("communication.recipient", "communication.content"),
    },
    {
        "task_type": "MEETING",
        "intent": "meeting_arrangement",
        "keywords": ("会议", "会议室", "参会人", "meeting"),
        "capabilities": ("Meeting", "Office"),
        "tags": ("meeting_management",),
        "scope": ("calendar.meeting",),
    },
    {
        "task_type": "OFFICE",
        "intent": "schedule_management",
        "keywords": ("日程", "待办", "提醒", "calendar", "schedule", "todo"),
        "capabilities": ("Office",),
        "tags": ("office_assistance",),
        "scope": ("calendar.personal",),
    },
    {
        "task_type": "TRAVEL",
        "intent": "travel_service",
        "keywords": ("出差", "差旅", "行程", "travel", "trip"),
        "capabilities": ("Travel", "Office"),
        "tags": ("travel_service",),
        "scope": ("travel.request",),
    },
    {
        "task_type": "RISK",
        "intent": "risk_analysis",
        "keywords": ("风险", "风控", "合规", "授信", "risk", "compliance", "credit"),
        "capabilities": ("Risk",),
        "tags": ("risk_analysis",),
        "scope": ("risk.business",),
    },
    {
        "task_type": "KNOWLEDGE",
        "intent": "knowledge_lookup",
        "keywords": ("制度", "知识库", "规定", "政策", "knowledge", "policy"),
        "capabilities": ("Knowledge",),
        "tags": ("knowledge_lookup",),
        "scope": ("knowledge.internal",),
    },
    {
        "task_type": "DOCUMENT",
        "intent": "document_generation",
        "keywords": ("生成文档", "word", "docx", "证明", "公文", "document"),
        "capabilities": ("Document",),
        "tags": ("document_generation",),
        "scope": ("document.generated",),
    },
    {
        "task_type": "DOCUMENT",
        "intent": "report_generation",
        "keywords": ("报告", "总结", "汇报材料", "report", "summary"),
        "capabilities": ("Document",),
        "tags": ("reporting", "analysis_summary"),
        "scope": ("document.generated",),
    },
    {
        "task_type": "RESEARCH",
        "intent": "information_research",
        "keywords": ("调研", "研究", "搜索", "检索", "市场分析", "research", "search", "crawl"),
        "capabilities": ("Research",),
        "tags": ("market_research", "knowledge_lookup"),
        "scope": ("internet.public",),
    },
    {
        "task_type": "WEATHER",
        "intent": "weather_query",
        "keywords": ("天气", "气温", "weather", "temperature"),
        "capabilities": ("Weather",),
        "tags": ("weather_query",),
        "scope": ("weather.public",),
    },
)


def _contains(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _infer_action(text: str) -> tuple[str, bool]:
    if _contains(text, ("发送", "群发", "发邮件", "send", "submit")):
        return "send", True
    if _contains(text, ("删除", "取消", "delete", "cancel")):
        return "delete", True
    if _contains(text, ("创建", "新增", "修改", "更新", "保存", "create", "update", "write")):
        return "write", False
    if _contains(text, ("生成", "写一份", "整理成", "generate", "draft")):
        return "generate", False
    return "read", False


async def profile_task(
    user_query: str,
    *,
    task_id: str,
    metadata: dict[str, Any] | None = None,
) -> TaskProfile:
    """规则优先形成画像；低置信度时复用现有 LLM 场景分析作为补充。"""
    metadata = metadata or {}
    normalized = str(user_query or "").strip().lower()
    matched = [rule for rule in DOMAIN_RULES if _contains(normalized, rule["keywords"])]
    action, irreversible = _infer_action(normalized)

    capabilities: list[str] = []
    tags: list[str] = []
    scopes: list[str] = []
    for rule in matched:
        capabilities.extend(rule["capabilities"])
        tags.extend(rule["tags"])
        scopes.extend(rule["scope"])

    primary = matched[0] if matched else None
    confidence = 0.92 if primary else 0.45
    intent = primary["intent"] if primary else "general_assistance"
    task_type = primary["task_type"] if primary else "GENERAL"
    reason = "rule_match" if primary else "no_strong_rule_match"

    if not primary:
        llm_profile = await analyze_task_context(user_query, metadata)
        llm_type = str(llm_profile.get("task_type") or "GENERAL").upper()
        if llm_type != "GENERAL":
            task_type = llm_type
            intent = f"{llm_type.lower()}_task"
            capabilities = list(llm_profile.get("expected_capabilities") or [llm_type.title()])
            tags = list(llm_profile.get("scenario_tags") or [llm_type.lower()])
            scopes = [str(llm_profile.get("data_scope") or "targeted")]
            action = str(llm_profile.get("operation_mode") or action)
            confidence = 0.65
            reason = "llm_enriched_after_rule_miss"

    missing_fields = []
    if action == "send" and not _contains(normalized, ("给", "向", "收件人", "recipient", "@")):
        missing_fields.append("recipient")

    risk_level = "HIGH" if irreversible else "MEDIUM" if action in {"write", "generate"} else "LOW"
    return TaskProfile(
        task_id=task_id,
        intent=intent,
        task_type=task_type,
        business_goal=user_query,
        action=action,
        entities={},
        data_scope=_unique(scopes) or ["general"],
        scenario_tags=_unique(tags) or ["general"],
        expected_capabilities=_unique(capabilities) or ["General"],
        risk_level=risk_level,
        irreversible=irreversible,
        constraints=list(metadata.get("constraints") or []),
        missing_fields=missing_fields,
        confidence=confidence,
        reason=reason,
    )

