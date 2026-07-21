import asyncio

from src.memory import MemoryManager, MemorySettings, MemoryStore
from src.memory.models import PreparedMemoryContext
from src.memory.manager import set_memory_manager


def _manager(tmp_path, **overrides):
    defaults = dict(
        enabled=True,
        long_term_enabled=True,
        auto_compact_enabled=True,
        llm_compaction_enabled=False,
        max_context_tokens=2000,
        reserved_output_tokens=200,
        trigger_tokens=120,
        target_tokens=80,
        store_path=tmp_path / "memory.sqlite3",
    )
    defaults.update(overrides)
    settings = MemorySettings(**defaults)
    store = MemoryStore(settings.store_path)
    return MemoryManager(settings=settings, store=store)


def test_manager_persists_context_and_explicit_long_term_memory(tmp_path):
    manager = _manager(tmp_path, trigger_tokens=10000)

    first = asyncio.run(
        manager.prepare_context(
            user_id="alice",
            session_id="thread",
            incoming_messages=[
                {"role": "user", "content": "Remember that I prefer concise reports"}
            ],
        )
    )
    restarted = _manager(tmp_path, trigger_tokens=10000)
    second = asyncio.run(
        restarted.prepare_context(
            user_id="alice",
            session_id="thread",
            incoming_messages=[{"role": "user", "content": "Write the next report"}],
        )
    )
    memories = asyncio.run(restarted.list_long_term("alice"))

    assert first.metadata.session_id == "thread"
    assert any("concise reports" in item.content for item in memories)
    assert any("untrusted_long_term_memory" in msg["content"] for msg in second.messages)
    assert len(asyncio.run(restarted.list_session_messages("alice", "thread"))) == 2


def test_manager_compacts_full_history_and_keeps_active_request(tmp_path):
    manager = _manager(tmp_path, trigger_tokens=20, target_tokens=200)
    asyncio.run(
        manager.prepare_context(
            user_id="alice",
            session_id="thread",
            incoming_messages=[
                {"role": "user", "content": "first long request " * 10}
            ],
        )
    )
    context = asyncio.run(
        manager.prepare_context(
            user_id="alice",
            session_id="thread",
            incoming_messages=[
                {"role": "user", "content": "ACTIVE REQUEST MUST REMAIN"}
            ],
        )
    )

    types = [message.get("metadata", {}).get("memory_type") for message in context.messages]
    assert context.metadata.compaction_id is not None
    assert types[:4] == ["boundary", "summary", "attachments", "hook_results"]
    assert context.messages[-1]["content"] == "ACTIVE REQUEST MUST REMAIN"
    assert len(asyncio.run(manager.list_session_messages("alice", "thread"))) == 2


def test_memory_failure_returns_original_sanitized_messages(tmp_path):
    manager = _manager(tmp_path)

    def broken(*_args, **_kwargs):
        raise OSError("disk unavailable")

    manager.store.append_message = broken
    context = asyncio.run(
        manager.prepare_context(
            user_id="alice",
            incoming_messages=[{"role": "user", "content": "hello"}],
        )
    )

    assert isinstance(context, PreparedMemoryContext)
    assert context.messages == ({"role": "user", "content": "hello"},)
    assert context.metadata.warning.startswith("memory_soft_failure:")


def test_simple_greeting_does_not_retrieve_long_term_memory(tmp_path):
    manager = _manager(tmp_path)
    asyncio.run(
        manager.remember(
            user_id="alice",
            content="hello messages should use a formal tone",
            provenance={"source": "test"},
        )
    )
    context = asyncio.run(
        manager.prepare_context(
            user_id="alice",
            incoming_messages=[{"role": "user", "content": "hello"}],
        )
    )

    assert not any(
        "untrusted_long_term_memory" in message["content"]
        for message in context.messages
    )


def test_memory_web_crud_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from src.service.web_app import app

    manager = _manager(tmp_path)
    set_memory_manager(manager)
    client = TestClient(app)
    try:
        created = client.post(
            "/api/memory/long-term",
            json={
                "user_id": "alice",
                "content": "Reports use markdown",
                "kind": "preference",
            },
        )
        assert created.status_code == 200
        memory_id = created.json()["memory_id"]

        listed = client.get("/api/memory/long-term", params={"user_id": "alice"})
        assert listed.status_code == 200
        assert listed.json()[0]["memory_id"] == memory_id

        deleted = client.delete(
            f"/api/memory/long-term/{memory_id}", params={"user_id": "alice"}
        )
        assert deleted.status_code == 200
    finally:
        set_memory_manager(None)
