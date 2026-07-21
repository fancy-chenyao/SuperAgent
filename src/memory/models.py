"""JSON-safe contracts shared by the Agent Memory subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class MemoryRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class LongTermMemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    EXPIRED = "expired"


class LongTermMemoryKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    DECISION = "decision"
    EPISODIC = "episodic"


@dataclass(frozen=True, slots=True)
class MemoryMessage:
    message_id: str
    user_id: str
    session_id: str
    sequence: int
    role: str
    content: str
    created_at: datetime = field(default_factory=utc_now)
    workflow_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from .utils import to_json_safe

        return to_json_safe(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MemoryMessage":
        values = dict(data)
        values["created_at"] = parse_datetime(values.get("created_at")) or utc_now()
        values["metadata"] = dict(values.get("metadata") or {})
        return cls(**values)


@dataclass(frozen=True, slots=True)
class RecoveryAttachments:
    recent_files: tuple[str, ...] = ()
    current_plan: dict[str, Any] | list[Any] | str | None = None
    active_skills: tuple[str, ...] = ()
    async_tasks: tuple[dict[str, Any], ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from .utils import to_json_safe

        return to_json_safe(self)

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any] | None
    ) -> "RecoveryAttachments":
        payload = dict(data or {})
        return cls(
            recent_files=tuple(payload.get("recent_files") or ()),
            current_plan=payload.get("current_plan"),
            active_skills=tuple(payload.get("active_skills") or ()),
            async_tasks=tuple(dict(item) for item in payload.get("async_tasks") or ()),
            extra=dict(payload.get("extra") or {}),
        )


MemoryAttachments = RecoveryAttachments


@dataclass(frozen=True, slots=True)
class CompactionBoundary:
    kind: str
    trigger: str
    token_count_before: int
    token_count_after: int
    last_message_id: str
    last_sequence: int
    created_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        from .utils import to_json_safe

        return to_json_safe(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompactionBoundary":
        values = dict(data)
        values["created_at"] = parse_datetime(values.get("created_at")) or utc_now()
        return cls(**values)


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    compaction_id: str
    user_id: str
    session_id: str
    boundary: CompactionBoundary
    summary: str
    attachments: RecoveryAttachments = field(default_factory=RecoveryAttachments)
    hook_results: tuple[dict[str, Any], ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def last_covered_sequence(self) -> int:
        return self.boundary.last_sequence

    def to_dict(self) -> dict[str, Any]:
        from .utils import to_json_safe

        return to_json_safe(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompactionRecord":
        values = dict(data)
        values["boundary"] = CompactionBoundary.from_dict(values["boundary"])
        values["attachments"] = RecoveryAttachments.from_dict(
            values.get("attachments")
        )
        values["hook_results"] = tuple(
            dict(item) for item in values.get("hook_results") or ()
        )
        values["created_at"] = parse_datetime(values.get("created_at")) or utc_now()
        values["metadata"] = dict(values.get("metadata") or {})
        return cls(**values)


@dataclass(frozen=True, slots=True)
class LongTermMemory:
    memory_id: str
    user_id: str
    content: str
    normalized_content: str
    kind: str
    scope: str
    confidence: float
    provenance: dict[str, Any]
    status: str = LongTermMemoryStatus.ACTIVE.value
    memory_key: str | None = None
    workflow_id: str | None = None
    session_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    superseded_at: datetime | None = None
    superseded_by: str | None = None
    deleted_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from .utils import to_json_safe

        return to_json_safe(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LongTermMemory":
        values = dict(data)
        for key in (
            "created_at",
            "updated_at",
            "expires_at",
            "superseded_at",
            "deleted_at",
        ):
            values[key] = parse_datetime(values.get(key))
        values["created_at"] = values["created_at"] or utc_now()
        values["updated_at"] = values["updated_at"] or values["created_at"]
        values["provenance"] = dict(values.get("provenance") or {})
        values["metadata"] = dict(values.get("metadata") or {})
        status = values.get("status", LongTermMemoryStatus.ACTIVE.value)
        values["status"] = status.value if isinstance(status, StrEnum) else str(status)
        return cls(**values)


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    memory: LongTermMemory
    score: float
    lexical_score: float
    confidence_score: float
    recency_score: float
    matched_terms: tuple[str, ...] = ()
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        from .utils import to_json_safe

        return to_json_safe(self)


@dataclass(frozen=True, slots=True)
class MemoryContextMetadata:
    session_id: str
    token_estimate: int
    compaction_id: str | None = None
    compaction_generation: int = 0
    retrieved_memory_ids: tuple[str, ...] = ()
    attachment_references: tuple[str, ...] = ()
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        from .utils import to_json_safe

        return to_json_safe(self)


@dataclass(frozen=True, slots=True)
class PreparedMemoryContext:
    messages: tuple[dict[str, Any], ...]
    metadata: MemoryContextMetadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [dict(message) for message in self.messages],
            "metadata": self.metadata.to_dict(),
        }


__all__ = [
    "CompactionBoundary",
    "CompactionRecord",
    "LongTermMemory",
    "LongTermMemoryKind",
    "LongTermMemoryStatus",
    "MemoryAttachments",
    "MemoryContextMetadata",
    "MemoryMessage",
    "MemoryRole",
    "PreparedMemoryContext",
    "RecoveryAttachments",
    "RetrievedMemory",
    "parse_datetime",
    "utc_now",
]
