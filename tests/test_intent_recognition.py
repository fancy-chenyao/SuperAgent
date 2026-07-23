from __future__ import annotations

import asyncio
from typing import Any

from src.orchestrator.intent_recognition import (
    HybridIntentRecognizer,
    IntentFusion,
    RuleIntentRecognizer,
    SemanticProviderError,
    extract_entities,
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


def test_optional_semantic_questions_do_not_block_concrete_compound_task() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "information_research",
            [
                _candidate("information_research", 0.95, text_span="搜索李娜的公开信息"),
                _candidate("report_generation", 0.92, text_span="整理成简短报告"),
            ],
            needs_clarification=True,
            questions=["请确认李娜的具体身份和报告格式。"],
        )
    )

    result = _run(
        HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize(
            "搜索李娜的公开信息，整理成一份简短报告"
        )
    )

    assert result.needs_clarification is False
    assert result.clarification_questions == []
    assert {item.name for item in result.executable_intents} == {
        "information_research",
        "report_generation",
    }


def test_semantic_questions_still_block_generic_single_intent_task() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "report_generation",
            [_candidate("report_generation", 0.93, text_span="生成报告")],
            needs_clarification=True,
            questions=["报告需要基于哪些内容？"],
        )
    )

    result = _run(
        HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize(
            "生成报告"
        )
    )

    assert result.needs_clarification is True
    assert result.clarification_questions == ["报告需要基于哪些内容？"]


def test_compound_task_primary_ranking_difference_does_not_trigger_clarification() -> None:
    provider = FakeSemanticProvider(
        {
            **_payload(
                "meeting_arrangement",
                [
                    _candidate(
                        "schedule_management",
                        0.91,
                        text_span="查询王经理下周的日程",
                    ),
                    _candidate(
                        "meeting_arrangement",
                        0.96,
                        text_span="安排一次和李娜的会议",
                    ),
                    _candidate(
                        "message_or_email_send",
                        0.90,
                        text_span="通知参会人",
                    ),
                ],
                entities={
                    "people": ["王经理", "李娜"],
                    "time": "下周",
                    "recipient": "参会人",
                },
                needs_clarification=True,
                questions=["请提供会议的具体时间和主题。"],
            ),
            "ambiguities": ["会议具体时间未指定", "会议主题未明确"],
        }
    )

    result = _run(
        HybridIntentRecognizer(mode="hybrid", semantic_provider=provider).recognize(
            "查询王经理下周的日程，安排一次和李娜的会议，并通知参会人"
        )
    )

    assert result.needs_clarification is False
    assert result.clarification_questions == []
    assert result.ambiguities == []
    assert result.primary_intent == "meeting_arrangement"
    assert {item.name for item in result.executable_intents} == {
        "schedule_management",
        "meeting_arrangement",
        "message_or_email_send",
    }


def test_schedule_meeting_entities_do_not_treat_quantifier_as_person() -> None:
    query = "查询王经理下周的日程，安排一次和李娜的会议，并通知参会人"

    entities = extract_entities(query)

    assert entities["people"] == ["王经理", "李娜"]
    assert entities["employee_name"] == "王经理"
    assert entities["time"] == "下周"
    assert entities["recipient"] == "参会人"
    assert "一次" not in entities["people"]


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


def test_weather_travel_advice_has_two_tasks_and_asks_for_employee() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "weather_query",
            [
                _candidate("weather_query", 0.95, text_span="查询北京明天天气"),
                _candidate("travel_service", 0.90, text_span="结合出差行程给出提醒"),
            ],
            entities={"location": "北京", "time": "明天"},
        )
    )

    profile = _run(
        profile_task(
            "查询北京明天天气，结合出差行程给出提醒",
            task_id="weather-travel-advice",
            recognition_mode="hybrid",
            semantic_provider=provider,
        )
    )

    assert profile.sub_intents == ["weather_query", "travel_service"]
    assert [item["intent"] for item in profile.subtasks] == [
        "weather_query",
        "travel_service",
    ]
    assert profile.entities["location"] == "北京"
    assert "employee_name" not in profile.entities
    assert "schedule_management" not in profile.sub_intents
    assert profile.needs_clarification is True
    assert profile.missing_fields == ["employee_or_criteria"]
    assert profile.clarification_questions == [
        "请问要结合哪位员工的出差行程？请提供员工姓名或工号。"
    ]


def test_rule_fallback_does_not_treat_weather_location_as_employee() -> None:
    profile = _run(
        profile_task(
            "查询北京明天天气，结合出差行程给出提醒",
            task_id="weather-travel-rule",
            recognition_mode="rule",
        )
    )

    assert profile.sub_intents == ["weather_query", "travel_service"]
    assert profile.entities["location"] == "北京"
    assert "employee_name" not in profile.entities
    assert "people" not in profile.entities
    assert profile.needs_clarification is True


def test_named_travel_query_infers_employee_id_lookup() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "travel_service",
            [_candidate("travel_service", 0.94, text_span="查询王强明天的出差行程")],
            entities={"people": ["王强"], "employee_name": "王强", "time": "明天"},
        )
    )
    profile = _run(
        profile_task(
            "查询王强明天的出差行程",
            task_id="named-travel",
            recognition_mode="hybrid",
            semantic_provider=provider,
        )
    )

    assert profile.sub_intents == ["employee_information_query", "travel_service"]
    assert profile.intent_nodes[0]["provenance"] == "inferred"
    assert profile.subtasks[1]["depends_on"] == ["subtask_1"]
    assert profile.needs_clarification is False


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


def test_policy_summary_and_explanation_document_are_three_distinct_tasks() -> None:
    provider = FakeSemanticProvider(
        _payload(
            "knowledge_lookup",
            [
                _candidate("knowledge_lookup", 0.93, text_span="查询公司年假制度"),
                _candidate("report_generation", 0.91, text_span="整理成摘要"),
            ],
            entities={},
        )
    )

    profile = _run(
        profile_task(
            "查询公司年假制度，整理成摘要，并生成一份说明文档",
            task_id="policy-summary-document",
            recognition_mode="hybrid",
            semantic_provider=provider,
        )
    )

    assert profile.sub_intents == [
        "knowledge_lookup",
        "report_generation",
        "document_generation",
    ]
    assert profile.entities["document_type"] == "explanation_document"
    assert [item["intent"] for item in profile.subtasks] == profile.sub_intents
    assert profile.subtasks[1]["depends_on"] == ["subtask_1"]
    assert profile.subtasks[2]["depends_on"] == ["subtask_2"]
    assert profile.subtasks[2]["segment_id"] == "segment_3"
    assert profile.subtasks[2]["goal"] == "生成一份说明文档"


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
