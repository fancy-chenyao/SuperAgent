from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from src.memory.models import LongTermMemoryStatus
from src.memory.retrieval import LexicalMemoryRetriever
from src.memory.store import MemoryStore, SecretDetectedError


def test_transcript_persists_and_isolates_users(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    stored = store.append_message(
        user_id="alice",
        session_id="thread",
        role="user",
        content="hello",
        message_id="m1",
    )

    reloaded = MemoryStore(path)
    assert reloaded.list_messages("alice", "thread") == [stored]
    assert reloaded.list_messages("bob", "thread") == []


def test_concurrent_appends_keep_all_messages(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    def append(index: int):
        return store.append_message(
            user_id="alice",
            session_id="thread",
            role="user",
            content=f"message {index}",
            message_id=f"m{index}",
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(append, range(20)))

    messages = store.list_messages("alice", "thread")
    assert len(messages) == 20
    assert [message.sequence for message in messages] == list(range(1, 21))
    assert {message.message_id for message in messages} == {
        f"m{index}" for index in range(20)
    }


def test_short_term_secrets_are_redacted_before_disk_write(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    secret = "sk-test-abcdefghijklmnopqrstuvwxyz"
    message = store.append_message(
        user_id="alice",
        session_id="thread",
        role="user",
        content=f"key={secret}",
        message_id="secret-message",
    )

    assert secret not in message.content
    assert "[REDACTED]" in message.content
    assert secret.encode() not in path.read_bytes()


def test_long_term_lifecycle_and_secret_rejection(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    first = store.remember(
        user_id="alice",
        content="I prefer concise reports",
        kind="preference",
        memory_key="report-style",
        provenance={"source": "user"},
    )
    second = store.remember(
        user_id="alice",
        content="I prefer detailed reports",
        kind="preference",
        memory_key="report-style",
        provenance={"source": "user"},
    )

    active = store.list_long_term("alice")
    assert [record.memory_id for record in active] == [second.memory_id]
    superseded = store.get_long_term("alice", first.memory_id)
    assert superseded.status == LongTermMemoryStatus.SUPERSEDED.value
    assert superseded.superseded_by == second.memory_id

    assert store.delete_long_term("alice", second.memory_id) is True
    assert store.list_long_term("alice") == []

    with pytest.raises(SecretDetectedError):
        store.remember(
            user_id="alice",
            content="remember sk-test-abcdefghijklmnopqrstuvwxyz",
            provenance={"source": "user"},
        )
    with pytest.raises(ValueError, match="unsupported memory kind"):
        store.remember(
            user_id="alice",
            content="invalid kind",
            kind="unknown",
            provenance={"source": "user"},
        )


def test_expired_memory_is_not_active(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    record = store.remember(
        user_id="alice",
        content="temporary constraint",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        provenance={"source": "test"},
    )

    assert store.list_long_term("alice") == []
    assert store.get_long_term("alice", record.memory_id).status == "expired"


def test_lexical_retrieval_is_relevant_and_user_scoped(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    weather = store.remember(
        user_id="alice",
        content="北京天气报告使用摄氏温度",
        kind="preference",
        confidence=0.9,
        provenance={"source": "user"},
    )
    store.remember(
        user_id="alice",
        content="代码示例使用 Python",
        kind="preference",
        provenance={"source": "user"},
    )
    store.remember(
        user_id="bob",
        content="北京天气报告使用华氏温度",
        kind="preference",
        provenance={"source": "user"},
    )

    results = LexicalMemoryRetriever().retrieve(
        "北京天气温度",
        [*store.list_long_term("alice"), *store.list_long_term("bob")],
        user_id="alice",
        top_k=5,
    )
    assert results[0].memory.memory_id == weather.memory_id
    assert all(result.memory.user_id == "alice" for result in results)
