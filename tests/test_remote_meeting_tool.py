from __future__ import annotations

import asyncio
import json

import mock_remote_tool_skill as tool_server


def _call(arguments: dict) -> dict:
    request = tool_server.ToolRequest(
        tool="remote_meeting_scheduling_tool",
        arguments=arguments,
    )
    return asyncio.run(tool_server.tool(request))["result"]


def test_meeting_tool_creates_and_queries_array_backed_records(
    tmp_path,
    monkeypatch,
) -> None:
    meeting_path = tmp_path / "meetings.json"
    meeting_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(tool_server, "_meeting_path", lambda: meeting_path)
    monkeypatch.setattr(tool_server, "_MEETING_CACHE", None)

    created = _call(
        {
            "action": "create",
            "meeting": {
                "title": "王经理与李娜会议",
                "date": "2026-07-27",
                "time": "10:00",
                "participants": ["王经理", "李娜"],
            },
        }
    )

    assert created["status"] == "success"
    stored = json.loads(meeting_path.read_text(encoding="utf-8"))
    assert isinstance(stored, list)
    assert stored[0]["participants"] == ["王经理", "李娜"]

    queried = _call({"action": "query", "date": "2026-07-27"})
    assert queried["status"] == "success"
    assert queried["matched_count"] == 1
