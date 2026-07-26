"""Minimal schema registry (Plan §7, Phase 1).

Provides ``register`` / ``validate`` with a deliberately small validation model
(required fields + basic type checks) and no third-party dependency. The
interface is shaped so it can later be swapped for ``jsonschema`` without callers
changing.

Schema format (minimal subset)::

    {
        "required": ["name", "age"],
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "additional_properties": True,  # optional, default True
    }
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# Accepted "type" tokens -> python types. ``number`` accepts int or float.
_TYPE_MAP: Dict[str, tuple] = {
    "string": (str,),
    "str": (str,),
    "integer": (int,),
    "int": (int,),
    "number": (int, float),
    "float": (float, int),
    "boolean": (bool,),
    "bool": (bool,),
    "array": (list,),
    "list": (list,),
    "object": (dict,),
    "dict": (dict,),
    "null": (type(None),),
}


def _validate_value(
    value: Any,
    spec: Dict[str, Any],
    path: str,
    errors: List[str],
) -> None:
    expected = spec.get("type")
    if expected:
        allowed = _TYPE_MAP.get(str(expected).lower())
        if allowed is None:
            errors.append(f"{path}: unknown type {expected!r} in schema")
            return
        if isinstance(value, bool) and bool not in allowed:
            errors.append(f"{path}: expected {expected}, got bool")
            return
        if not isinstance(value, allowed):
            errors.append(
                f"{path}: expected {expected}, got {type(value).__name__}"
            )
            return

    enum = spec.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"{path}: expected one of {enum!r}, got {value!r}")

    if isinstance(value, dict):
        required = spec.get("required", []) or []
        for field in required:
            if field not in value:
                errors.append(f"{path}: missing required field: {field!r}")
        properties: Dict[str, Any] = spec.get("properties", {}) or {}
        for field, field_spec in properties.items():
            if field in value:
                _validate_value(
                    value[field],
                    field_spec or {},
                    f"{path}.{field}",
                    errors,
                )
        if not spec.get("additional_properties", True):
            for field in value:
                if field not in properties:
                    errors.append(f"{path}: unexpected field: {field!r}")

    if isinstance(value, list) and spec.get("items"):
        item_spec = spec["items"] or {}
        for index, item in enumerate(value):
            _validate_value(item, item_spec, f"{path}[{index}]", errors)


class SchemaRegistry:
    """In-memory registry of named schemas with minimal validation."""

    def __init__(self) -> None:
        self._schemas: Dict[str, Dict[str, Any]] = {}

    def register(self, schema_ref: str, schema: Dict[str, Any]) -> None:
        """Register (or overwrite) a schema under ``schema_ref``."""
        if not isinstance(schema_ref, str) or not schema_ref:
            raise ValueError("schema_ref must be a non-empty string")
        if not isinstance(schema, dict):
            raise TypeError("schema must be a dict")
        self._schemas[schema_ref] = schema

    def has(self, schema_ref: str) -> bool:
        return schema_ref in self._schemas

    def get(self, schema_ref: str) -> Dict[str, Any] | None:
        return self._schemas.get(schema_ref)

    def validate(self, payload: Any, schema_ref: str) -> Tuple[bool, List[str]]:
        """Validate ``payload`` against a registered schema.

        Returns ``(is_valid, errors)``. An unknown ``schema_ref`` is reported as
        invalid so callers never silently pass unchecked data.
        """
        schema = self._schemas.get(schema_ref)
        if schema is None:
            return False, [f"unknown schema_ref: {schema_ref!r}"]

        errors: List[str] = []

        if not isinstance(payload, dict):
            return False, [
                f"payload must be an object for schema {schema_ref!r}, "
                f"got {type(payload).__name__}"
            ]

        _validate_value(payload, schema, "payload", errors)

        return (len(errors) == 0), errors


# Process-wide default registry (optional convenience for non-test callers).
_DEFAULT_REGISTRY = SchemaRegistry()


def get_schema_registry() -> SchemaRegistry:
    """Return the process-wide default :class:`SchemaRegistry`."""
    return _DEFAULT_REGISTRY
