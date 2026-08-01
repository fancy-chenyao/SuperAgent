from __future__ import annotations

import asyncio

from remote_agents.hr_assistant_agent import RemoteHRAssistantAgent
from remote_agents.knowledge_agent import RemoteKnowledgeAgent
from remote_agents.report_agent import RemoteReportAgent
from src.contracts import validate_agent_result
from src.contracts.agent_schema_catalog import register_agent_schemas
from src.orchestration.schema_registry import SchemaRegistry


class Extractor:
    def __init__(self, result):
        self.result = result

    async def extract(self, **_kwargs):
        return dict(self.result)


def _validate(result, agent) -> None:
    validation = validate_agent_result(
        result,
        agent.contract,
        register_agent_schemas(SchemaRegistry()),
    )
    assert validation.valid, validation.errors


def test_hr_timeout_returns_standard_retryable_error() -> None:
    agent = RemoteHRAssistantAgent()

    async def timeout(**_kwargs):
        raise TimeoutError("remote_person_info_tool timed out")

    agent.call_tool = timeout
    result = asyncio.run(
        agent.execute(
            [{"name": "remote_person_info_tool"}],
            [],
            {},
            Extractor({"keyword": "王强"}),
        )
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "REMOTE_TOOL_TIMEOUT"
    assert result["error"]["retryable"] is True
    _validate(result, agent)


def test_knowledge_marks_demo_law_content_as_statutory() -> None:
    agent = RemoteKnowledgeAgent()

    async def success(**_kwargs):
        return {
            "query": "年假",
            "answer": "依据《职工带薪年休假条例》",
            "knowledge_items_count": 2,
        }

    agent.call_tool = success
    result = asyncio.run(
        agent.execute(
            [{"name": "knowledge_search_tool"}],
            [],
            {},
            Extractor({"query": "公司的年假制度是什么"}),
        )
    )

    policy = result["outputs"]["policy.info"]
    assert policy["policy_scope"] == "statutory"
    _validate(result, agent)


def test_knowledge_preserves_explicit_company_policy_scope() -> None:
    agent = RemoteKnowledgeAgent()

    async def success(**_kwargs):
        return {
            "query": "报销",
            "answer": "公司报销流程",
            "knowledge_items_count": 1,
            "policy_scope": "company",
        }

    agent.call_tool = success
    result = asyncio.run(
        agent.execute(
            [{"name": "knowledge_search_tool"}],
            [],
            {},
            Extractor({"query": "报销"}),
        )
    )

    assert result["outputs"]["policy.info"]["policy_scope"] == "company"
    _validate(result, agent)


def test_knowledge_preserves_sources_and_not_found_metadata() -> None:
    agent = RemoteKnowledgeAgent()

    async def success(**_kwargs):
        return {
            "query": "报销",
            "answer": "请提交报销单和发票",
            "knowledge_items_count": 1,
            "policy_scope": "company",
            "sources": [
                {
                    "id": "reimbursement_001",
                    "category": "公司制度-费用报销",
                    "source": "演示公司财务报销制度（模拟）",
                    "effective_date": "2026-01-01",
                    "policy_scope": "company",
                }
            ],
            "matched_items": ["reimbursement_001"],
            "not_found": False,
        }

    agent.call_tool = success
    result = asyncio.run(
        agent.execute(
            [{"name": "knowledge_search_tool"}],
            [],
            {},
            Extractor({"query": "报销"}),
        )
    )

    policy = result["outputs"]["policy.info"]
    assert policy["matched_items"] == ["reimbursement_001"]
    assert policy["sources"][0]["source"] == "演示公司财务报销制度（模拟）"
    assert policy["not_found"] is False
    _validate(result, agent)


def test_report_returns_generic_markdown_output() -> None:
    agent = RemoteReportAgent()

    async def success(**_kwargs):
        return {"markdown": "# 汇总"}

    agent.call_tool = success
    result = asyncio.run(
        agent.execute(
            [{"name": "remote_report_builder_tool"}],
            [],
            {},
            Extractor(
                {
                    "title": "汇总",
                    "instruction": "汇总来源",
                    "sources": [
                        {
                            "logical_name": "employee.info",
                            "schema_ref": "employee.info@v1",
                            "payload": {"records": []},
                        },
                        {
                            "logical_name": "policy.info",
                            "schema_ref": "policy.info@v1",
                            "payload": {},
                        },
                    ],
                }
            ),
        )
    )

    output = result["outputs"]["report.markdown"]
    assert output == {"title": "汇总", "markdown": "# 汇总", "source_count": 2}
    _validate(result, agent)
