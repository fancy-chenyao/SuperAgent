from __future__ import annotations

from typing import Any


# 意图标签、路由元数据、规则词和语义提示样例只维护在这一处。
# SemanticIntentRecognizer 会根据本目录动态构造标签白名单和提示词。
INTENT_CATALOG: dict[str, dict[str, Any]] = {
    "salary_query": {
        "label": "查询薪资信息",
        "description": "查询员工工资、薪酬或收入数据",
        "task_type": "HR",
        "keywords": ("薪资", "工资", "薪酬", "salary", "payroll", "收入证明"),
        "examples": ("查询本月工资", "读取员工薪资数据"),
        "counter_examples": ("不涉及工资", "收入证明需要哪些权限"),
        "capabilities": ("HR",),
        "tags": ("salary_query", "hr_service"),
        "scope": ("employee.salary",),
        "default_action": "read",
    },
    "employee_information_query": {
        "label": "查询员工基础信息",
        "description": "查询员工身份、部门、岗位等基础档案",
        "task_type": "HR",
        "keywords": ("员工", "人员", "人事", "花名册", "基本信息", "个人信息", "employee", "personnel"),
        "examples": ("查询员工基本信息", "查一下人员档案"),
        "counter_examples": ("员工请假制度", "员工培训政策"),
        "capabilities": ("HR",),
        "tags": ("employee_info", "hr_service"),
        "scope": ("employee.basic_profile",),
        "default_action": "read",
    },
    "leave_record_query": {
        "label": "查询员工请假记录",
        "description": "查询员工的请假、休假申请和考勤请假记录",
        "task_type": "HR",
        "keywords": (
            "请假记录",
            "休假记录",
            "请假申请记录",
            "休假申请记录",
            "考勤记录",
            "leave record",
            "leave records",
        ),
        "patterns": (
            r"(?:有没有|是否|有无)"
            r"(?!.{0,8}(?:打算|计划|准备|需要|想|要不要|应该|可以|能否))"
            r".{0,8}(?:请假|休假)",
            r"(?:请假|休假).{0,8}(?:情况|状态|记录)",
        ),
        "context_exclusions": (
            r"(?:(?:请假|休假)[^，,。；;]*?(?:制度|政策|规定|规则|流程|权限)|"
            r"(?:制度|政策|规定|规则|流程|权限)[^，,。；;]*?(?:请假|休假))",
            r"(?:有没有|是否|有无).{0,8}"
            r"(?:打算|计划|准备|需要|想|要不要|应该|可以|能否).{0,8}"
            r"(?:请假|休假)",
        ),
        "context_preserve_patterns": (
            r"(?:制度|政策|规定|规则|流程|权限)[^，,。；;]*?"
            r"(?:查询|查一下|查看|看看|确认)[^，,。；;]*?"
            r"(?:请假|休假|考勤)(?:申请)?记录",
        ),
        "examples": ("查询李娜的请假记录", "查看员工休假申请记录"),
        "counter_examples": ("查询请假制度", "生成请假申请书"),
        "capabilities": ("HR", "Office"),
        "tags": ("leave_record_query", "hr_service", "leave_request"),
        "scope": ("employee.leave_records",),
        "default_action": "read",
    },
    "programming_learning": {
        "label": "编程学习与技术支持",
        "description": "学习编程语言或获得技术教学帮助",
        "task_type": "LEARNING",
        "keywords": ("学习java", "学习 java", "学java", "学 java", "学习python", "编程学习", "技术学习", "java", "python教程"),
        "examples": ("学习 Java 基础", "给我 Python 教程"),
        "counter_examples": (),
        "capabilities": ("Engineering", "Learning"),
        "tags": ("programming_learning", "technology_support"),
        "scope": ("learning.public_content",),
        "default_action": "read",
    },
    "message_or_email_send": {
        "label": "发送消息或邮件",
        "description": "向明确收件人发送邮件、消息、通知或材料",
        "task_type": "COMMUNICATION",
        "keywords": ("发给", "发送", "发邮件", "发送邮件", "邮件", "邮箱", "通知", "群发", "站内信", "寄给", "交给", "转给", "抄送", "email", "mail", "message"),
        "examples": ("发给王经理", "把材料寄给负责人"),
        "counter_examples": ("不要发送", "了解发送需要什么权限"),
        "capabilities": ("Communication",),
        "tags": ("notification_send",),
        "scope": ("communication.recipient", "communication.content"),
        "default_action": "send",
        "high_risk": True,
    },
    "meeting_arrangement": {
        "label": "安排会议",
        "description": "创建、调整或取消会议",
        "task_type": "MEETING",
        "keywords": ("会议", "会议室", "参会人", "预约会议", "开会", "meeting"),
        "examples": ("安排双方开会", "预约一个会议室"),
        "counter_examples": ("查询会议制度",),
        "capabilities": ("Meeting", "Office"),
        "tags": ("meeting_management",),
        "scope": ("calendar.meeting",),
        "default_action": "write",
    },
    "schedule_management": {
        "label": "处理日程或待办",
        "description": "查询或修改日程、待办和提醒",
        "task_type": "OFFICE",
        "keywords": (
            "日程", "待办", "设置提醒", "创建提醒", "添加提醒", "提醒我",
            "安排", "有没有时间", "有空", "calendar", "schedule", "todo",
        ),
        "examples": ("看看明天有没有时间", "查询个人日程"),
        "counter_examples": (),
        "capabilities": ("Office",),
        "tags": ("office_assistance",),
        "scope": ("calendar.personal",),
        "default_action": "read",
    },
    "travel_service": {
        "label": "处理差旅事项",
        "description": "查询或办理出差、差旅和行程事项",
        "task_type": "TRAVEL",
        "keywords": ("出差", "差旅", "行程", "travel", "trip"),
        "examples": ("查询出差行程",),
        "counter_examples": (),
        "capabilities": ("Travel", "Office"),
        "tags": ("travel_service",),
        "scope": ("travel.request",),
        "default_action": "read",
    },
    "risk_analysis": {
        "label": "风险分析",
        "description": "分析授信、经营、合规等风险",
        "task_type": "RISK",
        "keywords": ("风险", "风控", "合规", "授信", "risk", "compliance", "credit"),
        "examples": ("分析经营风险", "检查授信风险"),
        "counter_examples": (),
        "capabilities": ("Risk",),
        "tags": ("risk_analysis",),
        "scope": ("risk.business",),
        "default_action": "read",
    },
    "knowledge_lookup": {
        "label": "查询知识库或制度",
        "description": "查询内部制度、规定、流程或政策",
        "task_type": "KNOWLEDGE",
        "keywords": ("制度", "知识库", "规定", "政策", "权限", "流程", "knowledge", "policy"),
        "examples": ("查询请假制度", "了解审批权限"),
        "counter_examples": ("生成请假申请书",),
        "capabilities": ("Knowledge",),
        "tags": ("knowledge_lookup",),
        "scope": ("knowledge.internal",),
        "default_action": "read",
    },
    "document_generation": {
        "label": "生成文档",
        "description": "生成证明、申请书、公文或其他业务文档",
        "task_type": "DOCUMENT",
        "keywords": ("生成文档", "写一份", "起草", "收入证明", "在职证明", "证明", "公文", "申请书", "请假书", "请假申请", "请假申请书", "请假条", "请假单", "请假材料", "休假申请", "休假材料", "word", "docx", "document"),
        "examples": ("开个收入证明", "写一份请假书"),
        "counter_examples": ("不需要生成收入证明", "了解收入证明权限"),
        "capabilities": ("Document",),
        "tags": ("document_generation",),
        "scope": ("document.generated",),
        "default_action": "generate",
    },
    "report_generation": {
        "label": "生成报告或总结",
        "description": "生成分析报告、总结或汇报材料",
        "task_type": "DOCUMENT",
        "keywords": ("报告", "总结", "汇总", "汇报材料", "整理成", "report", "summary"),
        "examples": ("生成分析报告", "整理成汇报材料"),
        "counter_examples": ("不要生成报告",),
        "capabilities": ("Document",),
        "tags": ("reporting", "analysis_summary"),
        "scope": ("document.generated",),
        "default_action": "generate",
    },
    "information_research": {
        "label": "检索公开信息",
        "description": "调研、研究、搜索或检索公开资料",
        "task_type": "RESEARCH",
        "keywords": ("调研", "研究", "搜索", "检索", "市场分析", "公开信息", "资料", "research", "search", "crawl"),
        "examples": ("调研三家公司", "检索公开资料"),
        "counter_examples": (),
        "capabilities": ("Research",),
        "tags": ("market_research", "knowledge_lookup"),
        "scope": ("internet.public",),
        "default_action": "read",
    },
    "weather_query": {
        "label": "查询天气",
        "description": "查询天气、气温或温度",
        "task_type": "WEATHER",
        "keywords": ("天气", "气温", "温度", "weather", "temperature"),
        "examples": ("查询明天天气",),
        "counter_examples": (),
        "capabilities": ("Weather",),
        "tags": ("weather_query",),
        "scope": ("weather.public",),
        "default_action": "read",
    },
    "information_consultation": {
        "label": "流程或权限咨询",
        "description": "只了解如何操作、需要哪些权限或流程，不要求实际执行",
        "task_type": "KNOWLEDGE",
        "keywords": (),
        "examples": ("我只想了解发送材料需要什么权限",),
        "counter_examples": ("请直接发送材料",),
        "capabilities": ("Knowledge",),
        "tags": ("information_consultation",),
        "scope": ("knowledge.internal",),
        "default_action": "read",
    },
}


INTENT_LABELS = {name: str(item["label"]) for name, item in INTENT_CATALOG.items()}
SUPPORTED_INTENTS = frozenset(INTENT_CATALOG)


def intent_definition(name: str) -> dict[str, Any] | None:
    item = INTENT_CATALOG.get(name)
    return dict(item) if item else None


def intent_prompt_catalog() -> str:
    lines: list[str] = []
    for name, item in INTENT_CATALOG.items():
        examples = "；".join(item.get("examples") or ()) or "无"
        counter_examples = "；".join(item.get("counter_examples") or ()) or "无"
        lines.append(
            f"- {name}：{item['description']}；正例：{examples}；易混淆反例：{counter_examples}"
        )
    return "\n".join(lines)
