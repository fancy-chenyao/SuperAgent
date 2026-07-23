from __future__ import annotations

import asyncio
from typing import Any

from src.orchestrator.task_profiler import profile_task


class FakeSemanticProvider:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    async def recognize(self, _user_query: str) -> dict[str, Any]:
        return self.payload


def _intent(name: str, confidence: float, text_span: str) -> dict[str, Any]:
    return {
        "name": name,
        "confidence": confidence,
        "source": "semantic",
        "provenance": "explicit",
        "text_span": text_span,
        "evidence": [text_span],
        "negated": False,
        "condition": None,
        "condition_on": [],
    }


def _profile(query: str, payload: dict[str, Any]):
    return asyncio.run(
        profile_task(
            query,
            task_id="clarification-test",
            recognition_mode="hybrid",
            semantic_provider=FakeSemanticProvider(payload),
        )
    )


def test_policy_summary_document_does_not_clarify_for_primary_label_difference() -> None:
    query = "查询公司年假制度，整理成摘要，并生成一份说明文档"
    profile = _profile(
        query,
        {
            "primary_intent": "information_consultation",
            "intents": [
                _intent("information_consultation", 0.90, "查询公司年假制度"),
                _intent("report_generation", 0.85, "整理成摘要并生成说明文档"),
            ],
            "entities": {},
            "ambiguities": [],
            "needs_clarification": False,
            "clarification_questions": [],
        },
    )

    assert profile.needs_clarification is False
    assert profile.missing_fields == []
    assert profile.clarification_questions == []
    assert profile.sub_intents == [
        "knowledge_lookup",
        "report_generation",
        "document_generation",
    ]
    assert profile.subtasks[1]["depends_on"] == [profile.subtasks[0]["id"]]
    assert profile.subtasks[2]["depends_on"] == [profile.subtasks[1]["id"]]


def test_generic_report_asks_only_for_missing_source() -> None:
    profile = _profile(
        "生成报告",
        {
            "primary_intent": "report_generation",
            "intents": [_intent("report_generation", 0.93, "生成报告")],
            "entities": {},
            "ambiguities": [],
            "needs_clarification": False,
            "clarification_questions": [],
        },
    )

    assert profile.needs_clarification is True
    assert profile.missing_fields == ["document.source"]
    assert profile.clarification_questions == [
        "请说明要基于哪些内容生成报告，或提供报告的主题。"
    ]


def test_schedule_dependency_satisfies_meeting_time_requirement() -> None:
    query = "查询王经理下周的日程，安排一次和李娜的会议，并通知参会人"
    profile = _profile(
        query,
        {
            "primary_intent": "meeting_arrangement",
            "intents": [
                _intent("schedule_management", 0.91, "查询王经理下周的日程"),
                _intent("meeting_arrangement", 0.96, "安排一次和李娜的会议"),
                _intent("message_or_email_send", 0.90, "通知参会人"),
            ],
            "entities": {
                "people": ["王经理", "李娜"],
                "employee_name": "王经理",
                "time": "下周",
                "recipient": "参会人",
            },
            "ambiguities": ["会议具体时间未指定"],
            "needs_clarification": True,
            "clarification_questions": ["请提供具体会议时间。"],
        },
    )

    assert profile.needs_clarification is False
    assert profile.clarification_questions == []


def test_meeting_without_participants_or_time_asks_two_specific_questions() -> None:
    profile = _profile(
        "安排一次会议",
        {
            "primary_intent": "meeting_arrangement",
            "intents": [_intent("meeting_arrangement", 0.94, "安排一次会议")],
            "entities": {},
            "ambiguities": [],
            "needs_clarification": False,
            "clarification_questions": [],
        },
    )

    assert profile.missing_fields == ["meeting.participants", "meeting.time"]
    assert profile.clarification_questions == [
        "请提供需要参加会议的人员。",
        "请提供会议日期、时间或可选择的时间范围。",
    ]
