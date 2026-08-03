from __future__ import annotations

from typing import Any

from src.orchestration.schema_registry import SchemaRegistry, get_schema_registry


_POLICY_SCOPES = {"company", "statutory", "mixed", "unknown"}


def _policy_scope_from_sources(sources: list[dict[str, Any]]) -> str | None:
    source_scopes = [source.get("policy_scope") for source in sources]
    if not all(
        isinstance(scope, str) and scope in _POLICY_SCOPES
        for scope in source_scopes
    ):
        return None
    scopes = set(source_scopes)
    if not scopes:
        return "unknown"
    if len(scopes) == 1:
        return next(iter(scopes))
    return "mixed"


def _validate_policy_info_provenance(value: Any, path: str) -> list[str]:
    if not isinstance(value, dict):
        return []

    provenance_fields = ("sources", "matched_items", "not_found")
    present_fields = [field for field in provenance_fields if field in value]
    if not present_fields:
        return []

    errors = [
        (
            f"{path}: provenance field {field!r} is required "
            "when provenance metadata is present"
        )
        for field in provenance_fields
        if field not in value
    ]
    if errors:
        return errors

    count = value.get("knowledge_items_count")
    sources = value.get("sources")
    matched_items = value.get("matched_items")
    not_found = value.get("not_found")
    if (
        type(count) is not int
        or not isinstance(sources, list)
        or not isinstance(matched_items, list)
        or not isinstance(not_found, bool)
    ):
        return []

    if not_found:
        if count != 0:
            errors.append(
                f"{path}.knowledge_items_count: expected 0 when "
                f"not_found is true, got {count}"
            )
        if sources:
            errors.append(
                f"{path}.sources: expected an empty array when not_found is true"
            )
        if matched_items:
            errors.append(
                f"{path}.matched_items: expected an empty array when not_found is true"
            )
        if value.get("policy_scope") != "unknown":
            errors.append(
                f"{path}.policy_scope: expected 'unknown' when not_found is true, "
                f"got {value.get('policy_scope')!r}"
            )
        return errors

    if count <= 0:
        errors.append(
            f"{path}.knowledge_items_count: expected a positive value when "
            f"not_found is false, got {count}"
        )
    if len(sources) != count:
        errors.append(
            f"{path}.sources: expected {count} entries, got {len(sources)}"
        )
    if len(matched_items) != count:
        errors.append(
            f"{path}.matched_items: expected {count} entries, got {len(matched_items)}"
        )

    if all(isinstance(source, dict) for source in sources) and all(
        isinstance(item, str) for item in matched_items
    ):
        source_ids = [source.get("id") for source in sources]
        if all(isinstance(source_id, str) for source_id in source_ids):
            if any(not source_id.strip() for source_id in source_ids):
                errors.append(f"{path}.sources: source ids must be non-empty")
            if any(not item.strip() for item in matched_items):
                errors.append(f"{path}.matched_items: item ids must be non-empty")
            if len(set(source_ids)) != len(source_ids):
                errors.append(f"{path}.sources: source ids must be unique")
            if len(set(matched_items)) != len(matched_items):
                errors.append(f"{path}.matched_items: item ids must be unique")
            if set(source_ids) != set(matched_items):
                errors.append(
                    f"{path}: source ids must match matched_items exactly"
                )

        source_names = [source.get("source") for source in sources]
        if all(isinstance(source_name, str) for source_name in source_names) and any(
            not source_name.strip() for source_name in source_names
        ):
            errors.append(f"{path}.sources: source names must be non-empty")

        expected_scope = _policy_scope_from_sources(sources)
        actual_scope = value.get("policy_scope")
        if expected_scope is not None and actual_scope != expected_scope:
            errors.append(
                f"{path}.policy_scope: expected {expected_scope!r} derived from "
                f"sources, got {actual_scope!r}"
            )
    return errors


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
        "semantic_validator": _validate_policy_info_provenance,
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
                        "policy_scope",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string"},
                        "source": {"type": "string"},
                        "effective_date": {"type": "string"},
                        "source_updated_at": {"type": "string"},
                        "is_demo": {"type": "boolean"},
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
