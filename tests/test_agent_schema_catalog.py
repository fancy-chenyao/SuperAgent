from __future__ import annotations

from src.contracts.agent_schema_catalog import register_agent_schemas
from src.orchestration.schema_registry import SchemaRegistry


def test_catalog_registers_and_validates_business_schemas() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(
        {
            "query": "年假",
            "answer": "依据国家法规执行",
            "knowledge_items_count": 2,
            "policy_scope": "statutory",
        },
        "policy.info@v1",
    )

    assert valid
    assert errors == []
    assert registry.has("employee.info@v1")
    assert registry.has("report.sources@v1")
    assert registry.has("report.markdown@v1")


def test_policy_scope_enum_fails_closed() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(
        {
            "query": "年假",
            "answer": "回答",
            "knowledge_items_count": 1,
            "policy_scope": "internal-ish",
        },
        "policy.info@v1",
    )

    assert not valid
    assert "expected one of" in errors[0]


def test_report_source_items_require_logical_name_schema_and_payload() -> None:
    registry = register_agent_schemas(SchemaRegistry())

    valid, errors = registry.validate(
        {
            "sources": [{"logical_name": "employee.info"}],
            "instruction": "汇总",
            "title": "报告",
        },
        "report.sources@v1",
    )

    assert not valid
    assert any("schema_ref" in error for error in errors)
    assert any("payload" in error for error in errors)
