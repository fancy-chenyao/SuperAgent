from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


class MissingRequirement(BaseModel):
    field: str
    intent: str
    reason: str
    question: str


class ClarificationDecision(BaseModel):
    needs_clarification: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    requirements: list[MissingRequirement] = Field(default_factory=list)


class ClarificationAnalyzer:
    """根据可执行子任务的数据契约决定是否追问，不参与意图识别和排序。"""

    _GENERIC_RECIPIENTS = {"参会人", "所有参会人", "全体参会人", "与会人员", "相关人员"}
    _VAGUE_PHRASES = (
        "处理一下",
        "搞一下",
        "弄一下",
        "看着办",
        "随便",
        "帮我看看这个",
        "handle it",
        "do it",
    )

    @staticmethod
    def _has_people(entities: dict[str, Any]) -> bool:
        return bool(entities.get("employee_name") or entities.get("people"))

    @staticmethod
    def _has_specific_text(subtask: dict[str, Any], generic_terms: tuple[str, ...]) -> bool:
        text = str(subtask.get("text_span") or subtask.get("goal") or "").strip().lower()
        if not text:
            return False
        remainder = text
        for term in generic_terms:
            remainder = remainder.replace(term.lower(), "")
        remainder = re.sub(r"[\s，,。；;：:、的一份一个请帮我]+", "", remainder)
        return len(remainder) >= 2

    @staticmethod
    def _requirement(
        field: str,
        intent: str,
        reason: str,
        question: str,
    ) -> MissingRequirement:
        return MissingRequirement(
            field=field,
            intent=intent,
            reason=reason,
            question=question,
        )

    def analyze(
        self,
        *,
        user_query: str,
        recognition: Any,
        entities: dict[str, Any],
        subtasks: list[dict[str, Any]],
    ) -> ClarificationDecision:
        requirements: list[MissingRequirement] = []
        executable = [item for item in recognition.intents if not item.negated]

        if not executable or not subtasks:
            requirements.append(
                self._requirement(
                    "task_goal",
                    "general_assistance",
                    "没有识别到可执行任务",
                    "请说明您希望完成什么任务，以及要处理的对象和期望结果。",
                )
            )
        elif any(phrase in user_query.lower() for phrase in self._VAGUE_PHRASES):
            if not self._has_people(entities):
                requirements.append(
                    self._requirement(
                        "target",
                        str(executable[0].name),
                        "任务表达模糊且没有可识别的处理对象",
                        "请说明要处理的具体对象，例如员工姓名、客户名称或业务事项。",
                    )
                )
            if len(executable) == 1 and not self._has_specific_text(
                subtasks[0],
                ("处理", "查询", "生成", "发送", "任务", "事情", "员工"),
            ):
                requirements.append(
                    self._requirement(
                        "expected_result",
                        str(executable[0].name),
                        "未说明需要查询、生成或发送的具体结果",
                        "请说明期望结果，例如查询哪些信息、生成什么材料或执行什么操作。",
                    )
                )

        for subtask in subtasks:
            intent = str(subtask.get("intent") or "")
            dependencies = list(subtask.get("depends_on") or [])
            if intent == "weather_query":
                if not entities.get("location"):
                    requirements.append(
                        self._requirement(
                            "location",
                            intent,
                            "天气查询缺少地点",
                            "请问需要查询哪个城市或地区的天气？",
                        )
                    )

            elif intent == "message_or_email_send":
                recipient = str(entities.get("recipient") or "").strip()
                recipient_ready = bool(recipient) and (
                    recipient not in self._GENERIC_RECIPIENTS or self._has_people(entities)
                )
                if not recipient_ready:
                    requirements.append(
                        self._requirement(
                            "recipient",
                            intent,
                            "发送任务缺少可解析的收件人",
                            "请问要发送给谁？请提供收件人姓名、角色或邮箱。",
                        )
                    )
                if not dependencies and not self._has_specific_text(
                    subtask,
                    ("发送", "发给", "通知", "邮件", "消息"),
                ):
                    requirements.append(
                        self._requirement(
                            "communication.content",
                            intent,
                            "发送任务没有前置产物，也没有明确正文",
                            "请提供要发送的内容，或说明应基于哪个结果生成通知。",
                        )
                    )

            elif intent == "meeting_arrangement":
                if not self._has_people(entities):
                    requirements.append(
                        self._requirement(
                            "meeting.participants",
                            intent,
                            "会议任务缺少参会人",
                            "请提供需要参加会议的人员。",
                        )
                    )
                has_schedule_dependency = any(
                    prior.get("id") in dependencies
                    and prior.get("intent") == "schedule_management"
                    and prior.get("action") == "read"
                    for prior in subtasks
                )
                if not entities.get("time") and not has_schedule_dependency:
                    requirements.append(
                        self._requirement(
                            "meeting.time",
                            intent,
                            "会议任务没有时间范围，也没有前置日程查询",
                            "请提供会议日期、时间或可选择的时间范围。",
                        )
                    )

            elif intent in {"document_generation", "report_generation"}:
                has_document_definition = bool(entities.get("document_type")) and intent == "document_generation"
                if not dependencies and not has_document_definition and not self._has_specific_text(
                    subtask,
                    ("生成", "整理", "报告", "文档", "材料", "摘要", "说明"),
                ):
                    label = "报告" if intent == "report_generation" else "文档"
                    requirements.append(
                        self._requirement(
                            "document.source",
                            intent,
                            f"{label}任务没有内容来源或具体主题",
                            f"请说明要基于哪些内容生成{label}，或提供{label}的主题。",
                        )
                    )

            elif intent in {"employee_information_query", "salary_query", "leave_record_query"}:
                if not self._has_people(entities) and not self._has_specific_text(
                    subtask,
                    ("查询", "员工", "人员", "信息", "工资", "薪资", "请假记录"),
                ):
                    requirements.append(
                        self._requirement(
                            "employee_or_criteria",
                            intent,
                            "员工类查询没有姓名或筛选条件",
                            "请提供员工姓名，或说明用于查询员工的岗位、机构等条件。",
                        )
                    )

        unique: dict[str, MissingRequirement] = {}
        for item in requirements:
            unique.setdefault(item.field, item)
        normalized = list(unique.values())
        return ClarificationDecision(
            needs_clarification=bool(normalized),
            missing_fields=[item.field for item in normalized],
            questions=[item.question for item in normalized],
            requirements=normalized,
        )
