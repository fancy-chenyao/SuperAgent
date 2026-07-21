"""Deterministic, dependency-free helpers for the memory subsystem."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_TOKEN_UNIT_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|"
    r"[A-Za-z0-9_]+|[^\s]",
)
_LEXICAL_RE = re.compile(
    r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+",
    re.IGNORECASE,
)
_UNSAFE_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9._:@-]+")

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----[\s\S]*?"
            r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}", re.IGNORECASE),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "api_key",
        re.compile(
            r"\b(?:sk|pk|rk|api|key|token|secret)[-_][A-Za-z0-9._-]{12,}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_assignment",
        re.compile(
            r"\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|password|passwd|"
            r"client[_ -]?secret|secret[_ -]?key)\b\s*[:=]\s*"
            r"(?P<quote>['\"]?)(?P<value>[^\s,'\";]{6,})(?P=quote)",
            re.IGNORECASE,
        ),
    ),
    (
        "connection_string",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://"
            r"[^\s:/]+:[^\s/@]+@[^\s]+",
            re.IGNORECASE,
        ),
    ),
)


def to_json_safe(value: Any) -> Any:
    """Recursively convert common Python objects into JSON-native values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_json_safe(value.value)
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_json_safe(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "model_dump"):
        return to_json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_safe(item) for item in value]
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")


def json_dumps(value: Any) -> str:
    return json.dumps(to_json_safe(value), ensure_ascii=False, separators=(",", ":"))


def estimate_tokens(value: str | Mapping[str, Any] | list[Any] | tuple[Any, ...]) -> int:
    """Conservatively estimate tokens without a provider-specific tokenizer.

    Every CJK character counts as one token. Latin/digit runs use roughly four
    characters per token, and punctuation counts individually.
    """

    text = value if isinstance(value, str) else json_dumps(value)
    if not text:
        return 0
    count = 0
    for unit in _TOKEN_UNIT_RE.findall(text):
        if _CJK_RE.fullmatch(unit):
            count += 1
        elif unit.isalnum() or "_" in unit:
            count += max(1, (len(unit) + 3) // 4)
        else:
            count += 1
    return max(1, count)


def normalize_content(content: str) -> str:
    """Normalize content for deterministic duplicate/conflict checks."""

    return " ".join(content.strip().casefold().split())


def lexical_terms(text: str) -> set[str]:
    """Return explainable lexical terms for Latin and CJK text."""

    terms: set[str] = set()
    for chunk in _LEXICAL_RE.findall(text.casefold()):
        if _CJK_RE.search(chunk):
            terms.update(chunk)
            if len(chunk) > 1:
                terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
        elif len(chunk) > 1 or chunk.isdigit():
            terms.add(chunk)
            for suffix, minimum in (("ing", 6), ("ed", 5), ("es", 5), ("s", 4)):
                if chunk.endswith(suffix) and len(chunk) >= minimum:
                    terms.add(chunk[: -len(suffix)])
                    break
    return terms


def find_secrets(text: str) -> tuple[dict[str, str], ...]:
    """Return secret classifications and matched text for policy decisions."""

    findings: list[dict[str, str]] = []
    seen: set[tuple[int, int]] = set()
    for kind, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if span in seen:
                continue
            seen.add(span)
            findings.append({"kind": kind, "match": match.group(0)})
    return tuple(findings)


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))


def redact_secrets(text: str, replacement: str = "[REDACTED]") -> str:
    """Redact credential-like substrings while retaining surrounding context."""

    redacted = text
    for _kind, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def safe_identifier(value: str, *, prefix: str = "id", max_length: int = 128) -> str:
    """Create a stable log/file-safe identifier without losing collision safety."""

    raw = str(value).strip()
    if not raw:
        raise ValueError("identifier must not be empty")
    sanitized = _UNSAFE_IDENTIFIER_RE.sub("_", raw).strip("._-")
    if not sanitized:
        sanitized = prefix
    if sanitized == raw and len(sanitized) <= max_length:
        return sanitized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    available = max(1, max_length - len(digest) - 1)
    return f"{sanitized[:available]}-{digest}"


def derive_session_id(
    user_id: str,
    *,
    session_id: str | None = None,
    workflow_id: str | None = None,
) -> str:
    """Resolve the stable session identity used by legacy and new callers."""

    if session_id and session_id.strip():
        return safe_identifier(session_id, prefix="session")
    if workflow_id and workflow_id.strip():
        return safe_identifier(workflow_id, prefix="workflow")
    return safe_identifier(f"user:{user_id}", prefix="user")


def build_provenance(
    source: str,
    *,
    message_id: str | None = None,
    workflow_id: str | None = None,
    session_id: str | None = None,
    actor: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe provenance record for an explicit memory write."""

    if not source.strip():
        raise ValueError("provenance source must not be empty")
    result: dict[str, Any] = {
        "source": source.strip(),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    optional = {
        "message_id": message_id,
        "workflow_id": workflow_id,
        "session_id": session_id,
        "actor": actor,
    }
    result.update({key: value for key, value in optional.items() if value is not None})
    if metadata:
        result["metadata"] = to_json_safe(metadata)
    return result
