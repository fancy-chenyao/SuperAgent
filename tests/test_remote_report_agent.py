from __future__ import annotations

import asyncio

from remote_agents.report_agent import RemoteReportAgent


class FakeExtractor:
    async def extract(self, **kwargs):
        return {"title": "人事情况汇总", "data": [{"employee": "李娜"}]}


def test_report_agent_uses_separate_inner_and_outer_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_REPORT_LLM_TIMEOUT", "80")
    monkeypatch.setenv("REMOTE_REPORT_TOOL_TIMEOUT", "100")
    captured = {}
    agent = RemoteReportAgent()

    async def fake_call_tool(*, tool_name, arguments, timeout):
        captured.update(
            {"tool_name": tool_name, "arguments": arguments, "timeout": timeout}
        )
        return {"status": "success", "markdown": "# 人事情况汇总"}

    monkeypatch.setattr(agent, "call_tool", fake_call_tool)
    result = asyncio.run(
        agent.execute(
            tools=[{"name": "remote_report_builder_tool"}],
            messages=[],
            context={},
            parameter_extractor=FakeExtractor(),
        )
    )

    assert result["status"] == "success"
    assert captured["arguments"]["llm_timeout_sec"] == 80
    assert captured["timeout"] == 100
