from __future__ import annotations

from typing import Any

from src.orchestration.schema_registry import SchemaRegistry, get_schema_registry


AGENT_SCHEMA_CATALOG: dict[str, dict[str, Any]] = {
    "employee.info@v1": {
        "required": ["records"],
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "name": {"type": "string"},
                        "department": {"type": "string"},
                        "position": {"type": "string"},
                    },
                },
            },
            "query": {"type": "string"},
            "matched_count": {"type": "integer"},
        },
    },
    "employee.salary@v1": {
        "required": ["records"],
        "properties": {
            "records": {"type": "array"},
            "matched_count": {"type": "integer"},
        },
    },
    "policy.info@v1": {
        "required": ["query", "answer", "knowledge_items_count", "policy_scope"],
        "properties": {
            "query": {"type": "string"},
            "answer": {"type": "string"},
            "knowledge_items_count": {"type": "integer"},
            "policy_scope": {
                "type": "string",
                "enum": ["company", "statutory", "mixed", "unknown"],
            },
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "id",
                        "category",
                        "source",
                        "effective_date",
                        "policy_scope",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string"},
                        "source": {"type": "string"},
                        "effective_date": {"type": "string"},
                        "policy_scope": {
                            "type": "string",
                            "enum": ["company", "statutory", "mixed", "unknown"],
                        },
                    },
                },
            },
            "matched_items": {
                "type": "array",
                "items": {"type": "string"},
            },
            "not_found": {"type": "boolean"},
        },
    },
    "report.sources@v1": {
        "required": ["sources", "instruction", "title"],
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["logical_name", "schema_ref", "payload"],
                    "properties": {
                        "logical_name": {"type": "string"},
                        "schema_ref": {"type": "string"},
                        "payload": {"type": "object"},
                    },
                },
            },
            "instruction": {"type": "string"},
            "title": {"type": "string"},
        },
    },
    "report.markdown@v1": {
        "required": ["title", "markdown", "source_count"],
        "properties": {
            "title": {"type": "string"},
            "markdown": {"type": "string"},
            "source_count": {"type": "integer"},
        },
    },
}


def register_agent_schemas(
    registry: SchemaRegistry | None = None,
) -> SchemaRegistry:
    target = registry or get_schema_registry()
    for schema_ref, schema in AGENT_SCHEMA_CATALOG.items():
        target.register(schema_ref, schema)
    return target
