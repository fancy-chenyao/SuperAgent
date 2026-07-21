from __future__ import annotations

import asyncio
from typing import Any

from src.orchestrator.intent_recognition import (
    HybridIntentRecognizer,
    IntentFusion,
    RuleIntentRecognizer,
    SemanticProviderError,
)
from src.orchestrator.task_profiler import profile_task


class FakeSemanticProvider:
    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None):
        self.payload = payload or {}
        self.error = error

    async def recognize(self, user_query: str) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.payload


def _candidate(
    name: str,
    confidence: float,
    *,
    provenance: str = "explicit",
    text_span: str | None = None,
    negated: bool = False,
    condition: str | None = None,
    condition_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "confidence": confidence,
        "source": "semantic",
        "provenance": provenance,
        "text_span": text_span,
        "evidence": [text_span or f"{name} 的业务依赖"],
        "negated": negated,
        "condition": condition,
        "condition_on": condition_on or [],
    }


def _payload(
    primary: str,
    intents: list[dict[str, Any]],
    *,
    entities: dict[str, Any] | None = None,
    needs_clarification: bool = False,
    questions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "primary_intent": primary,
        "intents": intents,
        "entities": entities or {},
        "ambiguities": [],
        "needs_clarification": needs_clarification,
        "clarification_questions": questions or [],
    }


def _run(coro):
    return asyncio.run(coro)


def test_rule_and_semantic_agreement_uses_combined_source() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "employee_information_query",
            [_candidate("employee_information_query", 0.91, text_span="查询李娜的基本信息")],
            entities={"employee_name": "李娜", "people": ["李娜"]},
        )
    )
    result = _run(HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize("查询李娜的基本信息"))
    assert result.intents[0].source == "rule+semantic"
    assert result.intents[0].confidence > 0.91


def test_rule_semantic_conflict_is_not_silently_overwritten() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "meeting_arrangement",
            [_candidate("meeting_arrangement", 0.9, text_span="安排会议")],
        )
    )
    result = _run(HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize("生成文档"))
    assert result.needs_clarification is True
    assert any("冲突" in item for item in result.ambiguities)
    assert {item.name for item in result.intents} >= {"document_generation", "meeting_arrangement"}


def test_semantic_timeout_degrades_to_rule() -> None:
    provider = FakeSemanticProvider(error=SemanticProviderError("semantic_provider_timeout"))
    result = _run(HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize("查询李娜的基本信息"))
    assert result.degraded is True
    assert result.degradation_reason == "semantic_provider_timeout"
    assert result.primary_intent == "employee_information_query"


def test_invalid_semantic_schema_degrades_to_rule() -> None:
    provider = FakeSemanticProvider({"primary_intent": "employee_information_query", "intents": "bad"})
    result = _run(HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize("查询李娜的基本信息"))
    assert result.degraded is True
    assert result.primary_intent == "employee_information_query"


def test_unknown_semantic_label_degrades_to_rule() -> None:
    provider = FakeSemanticProvider(_payload("invented_intent", [_candidate("invented_intent", 0.99)]))
    result = _run(HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize("查询李娜的基本信息"))
    assert result.degraded is True
    assert result.primary_intent == "employee_information_query"


def test_low_confidence_semantic_only_candidate_triggers_clarification() -> None:
    provider = FakeSemanticProvider(
        _payload("document_generation", [_candidate("document_generation", 0.45)])
    )
    result = _run(HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize("替我处理一下"))
    assert result.primary_intent == "general_assistance"
    assert result.needs_clarification is True
    assert not result.executable_intents


def test_high_confidence_semantic_only_candidate_is_accepted() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "document_generation",
            [_candidate("document_generation", 0.91, text_span="制作一版正式材料")],
        )
    )
    result = _run(
        HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize(
            "替我把这事制作一版正式材料"
        )
    )
    assert result.primary_intent == "document_generation"
    assert result.intents[0].source == "semantic"
    assert result.needs_clarification is False


def test_synonym_expression_and_inferred_salary_are_distinguished() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "document_generation",
            [
                _candidate("document_generation", 0.94, text_span="帮李娜开个收入证明"),
                _candidate("salary_query", 0.78, provenance="inferred"),
                _candidate("message_or_email_send", 0.93, text_span="寄给王经理"),
            ],
            entities={
                "people": ["李娜", "王经理"],
                "employee_name": "李娜",
                "recipient": "王经理",
                "document_type": "income_proof",
            },
        )
    )
    profile = _run(
        profile_task(
            "帮李娜开个收入证明寄给王经理",
            task_id="synonym",
            recognition_mode="hybrid",
            semantic_provider=provider,
        )
    )
    assert profile.primary_goal_intent == "document_generation"
    assert profile.sub_intents == [
        "employee_information_query", "salary_query", "document_generation", "message_or_email_send"
    ]
    provenance = {node["name"]: node["provenance"] for node in profile.intent_nodes}
    assert provenance["document_generation"] == "explicit"
    assert provenance["salary_query"] == "inferred"
    assert profile.entities["recipient"] == "王经理"


def test_weak_keyword_schedule_expression() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "schedule_management",
            [_candidate("schedule_management", 0.92, text_span="看看王强明天有没有时间")],
            entities={"people": ["王强"], "employee_name": "王强", "time": "明天"},
        )
    )
    profile = _run(profile_task("看看王强明天有没有时间", task_id="schedule", recognition_mode="hybrid", semantic_provider=provider))
    assert profile.intent == "schedule_management"
    assert profile.entities["time"] == "明天"


def test_negated_send_is_visible_but_not_executable() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "report_generation",
            [
                _candidate("report_generation", 0.95, text_span="生成分析报告"),
                _candidate("message_or_email_send", 0.98, text_span="不要发送", negated=True),
            ],
            entities={"document_type": "analysis_report"},
        )
    )
    profile = _run(profile_task("生成分析报告，但不要发送", task_id="negated", recognition_mode="hybrid", semantic_provider=provider))
    assert profile.sub_intents == ["report_generation"]
    assert [item["intent"] for item in profile.subtasks] == ["report_generation"]
    send_node = next(item for item in profile.intent_nodes if item["name"] == "message_or_email_send")
    assert send_node["negated"] is True


def test_consultation_does_not_execute_document_or_send() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "information_consultation",
            [
                _candidate("information_consultation", 0.95, text_span="了解发送收入证明需要哪些权限"),
                _candidate("document_generation", 0.9, text_span="收入证明", negated=True),
                _candidate("message_or_email_send", 0.9, text_span="发送", negated=True),
            ],
        )
    )
    profile = _run(profile_task("我想了解发送收入证明需要哪些权限", task_id="consult", recognition_mode="hybrid", semantic_provider=provider))
    assert "document_generation" not in profile.sub_intents
    assert "message_or_email_send" not in profile.sub_intents
    assert profile.action == "read"


def test_conditional_task_keeps_condition_separate_from_data_dependency() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "schedule_management",
            [
                _candidate("schedule_management", 0.95, text_span="查询王强明天的日程"),
                _candidate(
                    "meeting_arrangement", 0.93, text_span="安排与张三的会议",
                    condition="王强明天有空", condition_on=["schedule_management"],
                ),
                _candidate("message_or_email_send", 0.9, text_span="发通知"),
            ],
            entities={"people": ["王强", "张三"], "employee_name": "王强", "time": "明天"},
        )
    )
    profile = _run(profile_task(
        "查询王强明天的日程，如果有空就安排与张三的会议并发通知",
        task_id="conditional", recognition_mode="hybrid", semantic_provider=provider,
    ))
    meeting = next(item for item in profile.subtasks if item["intent"] == "meeting_arrangement")
    send = next(item for item in profile.subtasks if item["intent"] == "message_or_email_send")
    assert meeting["execution_policy"] == "conditional"
    assert meeting["condition_on"] == ["subtask_1"]
    assert send["depends_on"] == [meeting["id"]]
    assert send["execution_policy"] == "conditional"


def test_missing_recipient_generates_specific_clarification() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "document_generation",
            [
                _candidate("document_generation", 0.95, text_span="生成李娜的收入证明"),
                _candidate("salary_query", 0.82, provenance="inferred"),
                _candidate("message_or_email_send", 0.92, text_span="发送"),
            ],
            entities={"employee_name": "李娜", "people": ["李娜"], "document_type": "income_proof"},
        )
    )
    profile = _run(profile_task("生成李娜的收入证明然后发送", task_id="missing", recognition_mode="hybrid", semantic_provider=provider))
    assert profile.missing_fields == ["recipient"]
    assert profile.needs_clarification is True
    assert any("收件人" in question for question in profile.clarification_questions)


def test_keyword_negation_has_zero_executable_actions() -> None:
    profile = _run(profile_task(
        "这份材料不涉及工资，也不需要生成收入证明",
        task_id="negative-keyword", recognition_mode="rule",
    ))
    assert profile.subtasks == []
    assert all(node["negated"] for node in profile.intent_nodes)


def test_original_rule_instruction_does_not_regress() -> None:
    profile = _run(profile_task("查询李娜的基本信息", task_id="regression", recognition_mode="rule"))
    assert profile.intent == "employee_information_query"
    assert profile.entities["employee_name"] == "李娜"


def test_employee_profile_leave_records_and_summary_are_three_distinct_tasks() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "employee_information_query",
            [
                _candidate("employee_information_query", 0.95, text_span="李娜的基本信息"),
                _candidate("leave_record_query", 0.94, text_span="请假记录"),
                _candidate("report_generation", 0.93, text_span="生成一份人事情况汇总"),
            ],
            entities={"people": ["李娜"], "employee_name": "李娜"},
        )
    )
    profile = _run(
        profile_task(
            "查询员工李娜的基本信息和请假记录，生成一份人事情况汇总",
            task_id="hr-leave-summary",
            recognition_mode="hybrid",
            semantic_provider=provider,
        )
    )

    assert profile.sub_intents == [
        "employee_information_query",
        "leave_record_query",
        "report_generation",
    ]
    assert [segment["text"] for segment in profile.segments] == [
        "查询员工李娜的基本信息",
        "请假记录",
        "生成一份人事情况汇总",
    ]
    assert [item["depends_on"] for item in profile.subtasks] == [
        [],
        ["subtask_1"],
        ["subtask_1", "subtask_2"],
    ]
