from __future__ import annotations

import asyncio
import json
from pathlib import Path

import mock_remote_tool_skill as tool_skill


class _FakeResponse:
    content = "已根据命中条目生成演示答案。"


class _FakeLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> _FakeResponse:
        self.prompts.append(prompt)
        return _FakeResponse()


def _knowledge_items() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "assets" / "knowledge_base.json"
    return json.loads(path.read_text(encoding="utf-8"))["knowledge_items"]


def test_knowledge_search_ranks_curated_keywords() -> None:
    ranked = tool_skill._rank_knowledge_items(_knowledge_items(), "工作十二年休几天")

    assert [item[0]["id"] for item in ranked] == ["annual_leave_001"]
    assert "十二年" in ranked[0][1]


def test_knowledge_search_limits_llm_context_and_returns_sources(monkeypatch) -> None:
    fake_llm = _FakeLLM()
    monkeypatch.setattr(tool_skill, "_KNOWLEDGE_CACHE", {"knowledge_items": _knowledge_items()})
    monkeypatch.setattr(tool_skill, "get_llm_by_type", lambda _name: fake_llm)

    response = asyncio.run(
        tool_skill.tool(
            tool_skill.ToolRequest(
                tool="knowledge_search_tool",
                arguments={"query": "费用报销需要什么材料"},
            )
        )
    )
    result = response["result"]

    assert result["status"] == "success"
    assert result["knowledge_items_count"] == 1
    assert result["matched_items"] == ["reimbursement_001"]
    assert result["sources"][0]["source"] == "演示公司财务报销制度（模拟）"
    assert result["sources"][0]["policy_scope"] == "company"
    assert result["not_found"] is False
    assert len(fake_llm.prompts) == 1
    assert "reimbursement_001" in fake_llm.prompts[0]
    assert "annual_leave_001" not in fake_llm.prompts[0]


def test_knowledge_search_returns_structured_not_found_without_llm(monkeypatch) -> None:
    monkeypatch.setattr(tool_skill, "_KNOWLEDGE_CACHE", {"knowledge_items": _knowledge_items()})

    def fail_if_called(_name):
        raise AssertionError("LLM must not be called for an unmatched query")

    monkeypatch.setattr(tool_skill, "get_llm_by_type", fail_if_called)
    response = asyncio.run(
        tool_skill.tool(
            tool_skill.ToolRequest(
                tool="knowledge_search_tool",
                arguments={"query": "火星基地如何申请"},
            )
        )
    )
    result = response["result"]

    assert result["status"] == "success"
    assert result["knowledge_items_count"] == 0
    assert result["policy_scope"] == "unknown"
    assert result["sources"] == []
    assert result["matched_items"] == []
    assert result["not_found"] is True
    assert "暂未收录" in result["answer"]
