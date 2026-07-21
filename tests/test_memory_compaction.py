import asyncio

import pytest

from src.memory.compaction import (
    CompactionEngine,
    CompactionToolCallError,
    NO_TOOL_WARNING,
    SUMMARY_SECTIONS,
    build_compaction_prompt,
    parse_compaction_response,
    render_compaction_segments,
)
from src.memory.models import MemoryMessage, RecoveryAttachments
from src.memory.store import MemoryStore


def _message(message_id, sequence, role, content):
    return MemoryMessage(
        message_id=message_id,
        user_id="alice",
        session_id="thread",
        sequence=sequence,
        role=role,
        content=content,
    )


def _valid_summary():
    return "\n\n".join(
        f"## {index}. {section}\ncontent {index}"
        for index, section in enumerate(SUMMARY_SECTIONS, 1)
    )


def test_prompt_has_double_no_tool_guard_and_redacts_secrets():
    secret = "sk-test-abcdefghijklmnopqrstuvwxyz"
    prompt = build_compaction_prompt([_message("m1", 1, "user", secret)])

    assert prompt.startswith(NO_TOOL_WARNING)
    assert prompt.endswith(NO_TOOL_WARNING)
    assert secret not in prompt
    assert "[REDACTED]" in prompt


def test_xml_parser_strips_analysis_and_requires_nine_sections():
    candidate = (
        '<memory_compaction version="1"><analysis>discard me</analysis><summary>'
        + _valid_summary()
        + "</summary></memory_compaction>"
    )
    summary = parse_compaction_response(candidate)

    assert "discard me" not in summary
    for section in SUMMARY_SECTIONS:
        assert section in summary


def test_tool_call_invalidates_compaction():
    candidate = {
        "content": "",
        "tool_calls": [{"name": "bad", "args": {}, "id": "1"}],
    }
    with pytest.raises(CompactionToolCallError):
        parse_compaction_response(candidate)


def test_engine_creates_four_part_envelope_and_store_keeps_tail(tmp_path):
    messages = [
        _message("m1", 1, "user", "first request"),
        _message("m2", 2, "assistant", "first result"),
    ]
    engine = CompactionEngine(summarizer=None, trigger_tokens=1, target_tokens=1000)
    record = asyncio.run(
        engine.compact(
            messages,
            attachments=RecoveryAttachments(
                current_plan={"steps": ["one"]},
                active_skills=("planner",),
                async_tasks=({"task_id": "bg-1", "status": "running"},),
            ),
            hook_results=[{"hook": "pre-compact", "status": "ok"}],
        )
    )
    segments = render_compaction_segments(record)

    assert len(segments) == 4
    assert [segment["metadata"]["memory_type"] for segment in segments] == [
        "boundary",
        "summary",
        "attachments",
        "hook_results",
    ]
    assert "bg-1" in segments[2]["content"]

    store = MemoryStore(tmp_path / "memory.sqlite3")
    for message in messages:
        store.append_message(message)
    store.save_compaction(record)
    store.append_message(
        user_id="alice",
        session_id="thread",
        role="user",
        content="new tail",
        message_id="m3",
    )
    latest, tail = store.messages_after_compaction("alice", "thread")
    assert latest.compaction_id == record.compaction_id
    assert [message.message_id for message in tail] == ["m3"]


def test_engine_discards_model_tool_call_instead_of_fallback():
    class BadModel:
        async def ainvoke(self, _prompt):
            return {
                "content": "",
                "tool_calls": [{"name": "bad", "args": {}, "id": "1"}],
            }

    engine = CompactionEngine(summarizer=BadModel(), trigger_tokens=1)
    with pytest.raises(CompactionToolCallError):
        asyncio.run(engine.compact([_message("m1", 1, "user", "hello")]))
