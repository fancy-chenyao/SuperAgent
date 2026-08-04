from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import mock_remote_tool_skill as tool_server
from mock_remote_tool_skill import _parse_optional_amount, app
from remote_agents.document_generator_agent import RemoteDocumentGeneratorAgent
from remote_agents.email_dispatch_agent import RemoteEmailDispatchAgent
from remote_agents.report_agent import RemoteReportAgent


ROOT = Path(__file__).resolve().parents[1]


def test_document_amount_parser_accepts_business_placeholders() -> None:
    for placeholder in ("待补充", "待确认", "未提供", "暂无", "未知"):
        assert _parse_optional_amount(placeholder) is None


def test_email_outputs_available_content_when_recipient_is_missing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(tool_server, "_EMAIL_CACHE", None)
    monkeypatch.setattr(tool_server, "_email_path", lambda: tmp_path / "emails.json")
    response = TestClient(app).post(
        "/tool",
        json={
            "tool": "remote_email_tool",
            "arguments": {"body": "test body"},
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "success"
    assert result["sent"]["to"] == ""
    assert result["sent"]["body"] == "test body"
    assert result["persisted"] is True
    assert result["external_operation_id"]
    assert "failure_phase" not in result
    assert "safe_to_retry" not in result


def test_travel_query_outputs_available_records_without_employee(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tool_server,
        "_load_travel_applications",
        lambda: [
            {
                "employee_id": "E001",
                "employee_name": "李娜",
                "destination": "上海",
            }
        ],
    )
    response = TestClient(app).post(
        "/tool",
        json={"tool": "query_travel_record", "arguments": {}},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "success"
    assert result["records"][0]["employee_name"] == "李娜"


def test_weather_tool_uses_the_remote_agent_result_contract() -> None:
    response = TestClient(app).post(
        "/tool",
        json={
            "tool": "remote_weather_tool",
            "arguments": {"location": "北京", "date": "明天"},
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "success"
    assert result["location"] == "北京"
    assert result["date"] == "明天"
    assert result["weather"] == "晴"


class FakeParameterExtractor:
    async def extract(self, **_: Any) -> dict[str, Any]:
        # 模拟参数模型选错旧模板；Agent 应以 TaskProfile 契约为准纠正模板。
        return {
            "template_name": "recommendation_letter",
            "data": {},
            "output_filename": "annual_leave_policy",
        }


class EmptyParameterExtractor:
    async def extract(self, **_: Any) -> dict[str, Any]:
        return {}


def test_report_uses_resolved_artifact_when_optional_model_fields_are_empty() -> None:
    agent = RemoteReportAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "success", "markdown": "report"}

    agent.call_tool = fake_call_tool  # type: ignore[method-assign]
    messages = [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(
                {
                    "resolved_inputs": {
                        "upstream_risk": {
                            "status": "success",
                            "records": [{"company_id": "uc-001"}],
                        }
                    }
                },
                ensure_ascii=False,
            ),
        }
    ]

    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_report_builder_tool"}],
            messages=messages,
            context={},
            parameter_extractor=EmptyParameterExtractor(),
        )
    )

    assert result["status"] == "success"
    assert captured["arguments"]["data"] == [{"company_id": "uc-001"}]


def test_email_reuses_profile_recipient_and_resolved_report() -> None:
    agent = RemoteEmailDispatchAgent()
    captured: dict[str, Any] = {}

    async def fake_call_tool(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "success"}

    agent.call_tool = fake_call_tool  # type: ignore[method-assign]
    messages = [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(
                {
                    "task_profile": {
                        "entities": {
                            "recipient": "合规负责人",
                            "document_type": "风险分析报告",
                        }
                    },
                    "resolved_inputs": {
                        "upstream_report": {
                            "status": "success",
                            "markdown": "# 风险分析报告",
                        }
                    },
                },
                ensure_ascii=False,
            ),
        }
    ]

    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_email_tool"}],
            messages=messages,
            context={},
            parameter_extractor=EmptyParameterExtractor(),
        )
    )

    assert result["status"] == "success"
    assert captured["arguments"] == {
        "to": "合规负责人",
        "subject": "风险分析报告",
        "body": "# 风险分析报告",
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
