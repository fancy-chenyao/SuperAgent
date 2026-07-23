from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from remote_agents.document_generator_agent import RemoteDocumentGeneratorAgent


ROOT = Path(__file__).resolve().parents[1]


class FakeParameterExtractor:
    async def extract(self, **_: Any) -> dict[str, Any]:
        # 模拟参数模型选错旧模板；Agent 应以 TaskProfile 契约为准纠正模板。
        return {
            "template_name": "recommendation_letter",
            "data": {},
            "output_filename": "annual_leave_policy",
        }


def test_registry_only_advertises_installed_document_templates() -> None:
    registry = json.loads(
        (ROOT / "mock_remote_registry.json").read_text(encoding="utf-8-sig")
    )
    templates = json.loads(
        (ROOT / "assets" / "document_templates.json").read_text(encoding="utf-8")
    )["templates"]
    document_agent = next(
        item
        for item in registry["resources"]
        if item.get("name") == "RemoteDocumentGeneratorAgent"
    )
    tool = document_agent["metadata"]["selected_tools"][0]
    advertised = set(tool["parameters"]["properties"]["template_name"]["enum"])

    assert advertised == set(templates)
    assert "recommendation_letter" not in advertised
    assert "explanation_document" in advertised


def test_explanation_document_uses_profile_contract_and_upstream_report() -> None:
    agent = RemoteDocumentGeneratorAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "success"}

    agent.call_tool = fake_call_tool  # type: ignore[method-assign]
    messages = [
        {
            "role": "assistant",
            "tool": "RemoteReportAgent",
            "content": "# 年假制度摘要\n\n员工年假按工龄分档执行。",
        },
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(
                {
                    "assigned_steps": [{"title": "生成年假制度说明文档"}],
                    "task_profile": {
                        "entities": {"document_type": "explanation_document"}
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_docx_generator_tool"}],
            messages=messages,
            context={},
            parameter_extractor=FakeParameterExtractor(),
        )
    )

    assert result["status"] == "success"
    arguments = captured["arguments"]
    assert arguments["template_name"] == "explanation_document"
    assert arguments["data"]["title"] == "生成年假制度说明文档"
    assert "员工年假按工龄分档执行" in arguments["data"]["content"]
