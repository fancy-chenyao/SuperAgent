"""Transactional SQLite persistence for short- and long-term Agent Memory."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from .models import (
    CompactionRecord,
    LongTermMemory,
    LongTermMemoryStatus,
    MemoryMessage,
    parse_datetime,
    utc_now,
)
from .utils import contains_secret, normalize_content, redact_secrets, to_json_safe


class MemoryStoreError(RuntimeError):
    pass


class MemoryScopeError(MemoryStoreError):
    pass


class MessageIdConflictError(MemoryStoreError):
    pass


class SecretDetectedError(MemoryStoreError, ValueError):
    pass


def _iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(to_json_safe(value), ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class MemoryStore:
    """SQLite repository with WAL and short write transactions.

    A new connection is opened per operation so the same store object is safe to
    use from FastAPI workers and ``asyncio.to_thread`` calls.
    """

    def __init__(self, path: str | Path) -> None:
        requested = Path(path).expanduser()
        if requested.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            requested = requested / "memory.sqlite3"
        self.path = requested.resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_messages (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    workflow_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (user_id, session_id, message_id),
                    UNIQUE (user_id, session_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_messages_scope_sequence
                ON memory_messages(user_id, session_id, sequence);

                CREATE TABLE IF NOT EXISTS memory_compactions (
                    compaction_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    last_message_id TEXT NOT NULL,
                    boundary_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '{}',
                    hook_results_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_compactions_scope_sequence
                ON memory_compactions(user_id, session_id, last_sequence DESC);

                CREATE TABLE IF NOT EXISTS memory_long_term (
                    memory_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    memory_key TEXT,
                    workflow_id TEXT,
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    superseded_at TEXT,
                    superseded_by TEXT,
                    deleted_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_memory_long_term_user_status
                ON memory_long_term(user_id, status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_memory_long_term_user_key
                ON memory_long_term(user_id, memory_key, status);
                """
            )

    @staticmethod
    def _validate_scope(user_id: str, session_id: str | None = None) -> None:
        if not str(user_id).strip():
            raise MemoryScopeError("user_id is required")
        if session_id is not None and not str(session_id).strip():
            raise MemoryScopeError("session_id is required")

    def append_message(
        self,
        message: MemoryMessage | Mapping[str, Any] | None = None,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        role: str | None = None,
        content: str | None = None,
        message_id: str | None = None,
        workflow_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryMessage:
        if message is None:
            if user_id is None or session_id is None or role is None or content is None:
                raise ValueError("message or user_id/session_id/role/content is required")
            message = MemoryMessage(
                message_id=message_id or uuid4().hex,
                user_id=user_id,
                session_id=session_id,
                sequence=0,
                role=role,
                content=content,
                workflow_id=workflow_id,
                metadata=dict(metadata or {}),
            )
        elif isinstance(message, Mapping):
            message = MemoryMessage.from_dict(message)
        self._validate_scope(message.user_id, message.session_id)

        sanitized = redact_secrets(message.content)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT * FROM memory_messages
                    WHERE user_id=? AND session_id=? AND message_id=?
                    """,
                    (message.user_id, message.session_id, message.message_id),
                ).fetchone()
                if existing is not None:
                    stored = self._row_to_message(existing)
                    if stored.role != message.role or stored.content != sanitized:
                        raise MessageIdConflictError(
                            f"message_id {message.message_id!r} has different content"
                        )
                    connection.execute("COMMIT")
                    return stored

                next_sequence = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM memory_messages WHERE user_id=? AND session_id=?
                    """,
                    (message.user_id, message.session_id),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO memory_messages (
                        user_id, session_id, sequence, message_id, role, content,
                        created_at, workflow_id, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.user_id,
                        message.session_id,
                        next_sequence,
                        message.message_id,
                        message.role,
                        sanitized,
                        _iso(message.created_at),
                        message.workflow_id,
                        _json(message.metadata),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return MemoryMessage(
            message_id=message.message_id,
            user_id=message.user_id,
            session_id=message.session_id,
            sequence=int(next_sequence),
            role=message.role,
            content=sanitized,
            created_at=message.created_at,
            workflow_id=message.workflow_id,
            metadata=dict(message.metadata),
        )

    def append_messages(
        self, messages: Iterable[MemoryMessage | Mapping[str, Any]]
    ) -> list[MemoryMessage]:
        return [self.append_message(message) for message in messages]

    def list_messages(
        self,
        user_id: str,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> list[MemoryMessage]:
        self._validate_scope(user_id, session_id)
        sql = (
            "SELECT * FROM memory_messages "
            "WHERE user_id=? AND session_id=? AND sequence>? "
            "ORDER BY sequence ASC"
        )
        parameters: list[Any] = [user_id, session_id, after_sequence]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(max(0, int(limit)))
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._row_to_message(row) for row in rows]

    get_messages = list_messages

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MemoryMessage:
        return MemoryMessage(
            message_id=row["message_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            sequence=int(row["sequence"]),
            role=row["role"],
            content=row["content"],
            created_at=parse_datetime(row["created_at"]) or utc_now(),
            workflow_id=row["workflow_id"],
            metadata=dict(_loads(row["metadata_json"], {})),
        )

    def save_compaction(self, record: CompactionRecord) -> CompactionRecord:
        self._validate_scope(record.user_id, record.session_id)
        boundary = record.boundary
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM memory_compactions WHERE compaction_id=?",
                    (record.compaction_id,),
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return self._row_to_compaction(existing)
                covered = connection.execute(
                    """
                    SELECT message_id FROM memory_messages
                    WHERE user_id=? AND session_id=? AND sequence=?
                    """,
                    (record.user_id, record.session_id, boundary.last_sequence),
                ).fetchone()
                if covered is None or covered["message_id"] != boundary.last_message_id:
                    raise MemoryStoreError("compaction boundary does not match transcript")
                connection.execute(
                    """
                    INSERT INTO memory_compactions (
                        compaction_id, user_id, session_id, last_sequence,
                        last_message_id, boundary_json, summary, attachments_json,
                        hook_results_json, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.compaction_id,
                        record.user_id,
                        record.session_id,
                        boundary.last_sequence,
                        boundary.last_message_id,
                        _json(boundary),
                        record.summary,
                        _json(record.attachments),
                        _json(record.hook_results),
                        _json(record.metadata),
                        _iso(record.created_at),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return record

    commit_compaction = save_compaction

    def latest_compaction(
        self, user_id: str, session_id: str
    ) -> CompactionRecord | None:
        self._validate_scope(user_id, session_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_compactions
                WHERE user_id=? AND session_id=?
                ORDER BY last_sequence DESC, created_at DESC LIMIT 1
                """,
                (user_id, session_id),
            ).fetchone()
        return self._row_to_compaction(row) if row is not None else None

    get_latest_compaction = latest_compaction

    def list_compactions(
        self, user_id: str, session_id: str
    ) -> list[CompactionRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_compactions
                WHERE user_id=? AND session_id=?
                ORDER BY last_sequence ASC, created_at ASC
                """,
                (user_id, session_id),
            ).fetchall()
        return [self._row_to_compaction(row) for row in rows]

    @staticmethod
    def _row_to_compaction(row: sqlite3.Row) -> CompactionRecord:
        return CompactionRecord.from_dict(
            {
                "compaction_id": row["compaction_id"],
                "user_id": row["user_id"],
                "session_id": row["session_id"],
                "boundary": _loads(row["boundary_json"], {}),
                "summary": row["summary"],
                "attachments": _loads(row["attachments_json"], {}),
                "hook_results": _loads(row["hook_results_json"], []),
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
        )

    def messages_after_compaction(
        self, user_id: str, session_id: str
    ) -> tuple[CompactionRecord | None, list[MemoryMessage]]:
        record = self.latest_compaction(user_id, session_id)
        after = record.boundary.last_sequence if record else 0
        return record, self.list_messages(user_id, session_id, after_sequence=after)

    def remember(
        self,
        *,
        user_id: str,
        content: str,
        kind: str = "fact",
        memory_key: str | None = None,
        scope: str = "user",
        confidence: float = 1.0,
        provenance: Mapping[str, Any] | None = None,
        workflow_id: str | None = None,
        session_id: str | None = None,
        expires_at: datetime | str | None = None,
        metadata: Mapping[str, Any] | None = None,
        memory_id: str | None = None,
    ) -> LongTermMemory:
        self._validate_scope(user_id)
        clean = str(content).strip()
        if not clean:
            raise ValueError("memory content is required")
        if contains_secret(clean):
            raise SecretDetectedError("secret-looking content cannot be remembered")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        allowed_kinds = {"fact", "preference", "constraint", "decision", "episodic"}
        if kind not in allowed_kinds:
            raise ValueError(f"unsupported memory kind: {kind}")
        if not str(scope).strip():
            raise ValueError("memory scope is required")
        normalized = normalize_content(clean)
        now = utc_now()
        identifier = memory_id or uuid4().hex
        expires = parse_datetime(expires_at)

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if memory_key:
                    existing = connection.execute(
                        """
                        SELECT * FROM memory_long_term
                        WHERE user_id=? AND memory_key=? AND status='active'
                        ORDER BY updated_at DESC LIMIT 1
                        """,
                        (user_id, memory_key),
                    ).fetchone()
                else:
                    existing = connection.execute(
                        """
                        SELECT * FROM memory_long_term
                        WHERE user_id=? AND normalized_content=? AND status='active'
                        ORDER BY updated_at DESC LIMIT 1
                        """,
                        (user_id, normalized),
                    ).fetchone()
                if existing is not None and not memory_key:
                    connection.execute("COMMIT")
                    return self._row_to_long_term(existing)
                if existing is not None:
                    connection.execute(
                        """
                        UPDATE memory_long_term
                        SET status='superseded', superseded_at=?, superseded_by=?,
                            updated_at=? WHERE memory_id=? AND user_id=?
                        """,
                        (_iso(now), identifier, _iso(now), existing["memory_id"], user_id),
                    )
                connection.execute(
                    """
                    INSERT INTO memory_long_term (
                        memory_id, user_id, content, normalized_content, kind,
                        scope, confidence, provenance_json, status, memory_key,
                        workflow_id, session_id, created_at, updated_at,
                        expires_at, superseded_at, superseded_by, deleted_at,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                    """,
                    (
                        identifier,
                        user_id,
                        clean,
                        normalized,
                        kind,
                        scope,
                        float(confidence),
                        _json(dict(provenance or {})),
                        memory_key,
                        workflow_id,
                        session_id,
                        _iso(now),
                        _iso(now),
                        _iso(expires) if expires else None,
                        _json(dict(metadata or {})),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM memory_long_term WHERE memory_id=?", (identifier,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return self._row_to_long_term(row)

    def _expire_records(self, connection: sqlite3.Connection, user_id: str) -> None:
        now = _iso()
        connection.execute(
            """
            UPDATE memory_long_term SET status='expired', updated_at=?
            WHERE user_id=? AND status='active' AND expires_at IS NOT NULL
              AND expires_at<=?
            """,
            (now, user_id, now),
        )

    def list_long_term(
        self,
        user_id: str,
        *,
        statuses: Sequence[str] = ("active",),
    ) -> list[LongTermMemory]:
        self._validate_scope(user_id)
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_records(connection, user_id)
                rows = connection.execute(
                    f"""
                    SELECT * FROM memory_long_term
                    WHERE user_id=? AND status IN ({placeholders})
                    ORDER BY updated_at DESC, memory_id ASC
                    """,
                    [user_id, *statuses],
                ).fetchall()
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return [self._row_to_long_term(row) for row in rows]

    def get_long_term(self, user_id: str, memory_id: str) -> LongTermMemory | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM memory_long_term WHERE user_id=? AND memory_id=?",
                (user_id, memory_id),
            ).fetchone()
        return self._row_to_long_term(row) if row is not None else None

    def delete_long_term(self, user_id: str, memory_id: str) -> bool:
        self._validate_scope(user_id)
        now = _iso()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE memory_long_term SET status='deleted', deleted_at=?, updated_at=?
                WHERE user_id=? AND memory_id=? AND status!='deleted'
                """,
                (now, now, user_id, memory_id),
            )
        return cursor.rowcount > 0

    forget = delete_long_term

    @staticmethod
    def _row_to_long_term(row: sqlite3.Row) -> LongTermMemory:
        return LongTermMemory.from_dict(
            {
                "memory_id": row["memory_id"],
                "user_id": row["user_id"],
                "content": row["content"],
                "normalized_content": row["normalized_content"],
                "kind": row["kind"],
                "scope": row["scope"],
                "confidence": float(row["confidence"]),
                "provenance": _loads(row["provenance_json"], {}),
                "status": row["status"],
                "memory_key": row["memory_key"],
                "workflow_id": row["workflow_id"],
                "session_id": row["session_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "expires_at": row["expires_at"],
                "superseded_at": row["superseded_at"],
                "superseded_by": row["superseded_by"],
                "deleted_at": row["deleted_at"],
                "metadata": _loads(row["metadata_json"], {}),
            }
        )


__all__ = [
    "MemoryScopeError",
    "MemoryStore",
    "MemoryStoreError",
    "MessageIdConflictError",
    "SecretDetectedError",
]
