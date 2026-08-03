from __future__ import annotations

import pytest

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


def test_policy_sources_and_not_found_metadata_are_consistent() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "报销",
            "answer": "请提交报销单",
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
        },
        "policy.info@v1",
    )

    assert valid, errors


def test_policy_not_found_metadata_requires_empty_provenance() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "不存在的制度",
            "answer": "未找到相关制度",
            "knowledge_items_count": 0,
            "policy_scope": "unknown",
            "sources": [],
            "matched_items": [],
            "not_found": True,
        },
        "policy.info@v1",
    )

    assert valid, errors


@pytest.mark.parametrize(
    "overrides, expected_error",
    [
        (
            {"sources": [], "matched_items": []},
            "expected 1 entries",
        ),
        (
            {
                "sources": [
                    {
                        "id": "reimbursement_001",
                        "category": "公司制度-费用报销",
                        "source": "演示公司财务报销制度（模拟）",
                        "policy_scope": "company",
                    }
                ],
                "matched_items": ["leave_001"],
            },
            "source ids must match matched_items exactly",
        ),
        (
            {"not_found": True},
            "expected 0 when not_found is true",
        ),
    ],
)
def test_policy_provenance_rejects_internally_inconsistent_results(
    overrides: dict[str, object],
    expected_error: str,
) -> None:
    registry = register_agent_schemas(SchemaRegistry())
    payload = {
        "query": "报销",
        "answer": "请提交报销单",
        "knowledge_items_count": 1,
        "policy_scope": "company",
        "sources": [
            {
                "id": "reimbursement_001",
                "category": "公司制度-费用报销",
                "source": "演示公司财务报销制度（模拟）",
                "policy_scope": "company",
            }
        ],
        "matched_items": ["reimbursement_001"],
        "not_found": False,
    }
    payload.update(overrides)

    valid, errors = registry.validate(payload, "policy.info@v1")

    assert not valid
    assert any(expected_error in error for error in errors)


def test_policy_source_snapshot_date_does_not_require_effective_date() -> None:
    registry = register_agent_schemas(SchemaRegistry())
    valid, errors = registry.validate(
        {
            "query": "养老金",
            "answer": "演示答案",
            "knowledge_items_count": 1,
            "policy_scope": "statutory",
            "sources": [
                {
                    "id": "pension_001",
                    "category": "社保-养老金",
                    "source": "公开办事说明（演示摘录）",
                    "source_updated_at": "2026-01-01",
                    "policy_scope": "statutory",
                    "is_demo": True,
                }
            ],
            "matched_items": ["pension_001"],
            "not_found": False,
        },
        "policy.info@v1",
    )

    assert valid, errors


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
