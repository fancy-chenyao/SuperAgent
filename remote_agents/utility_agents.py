#!/usr/bin/env python
"""Tool-driven remote agents advertised by the mock resource registry."""

from __future__ import annotations

from typing import Any, Dict, List

from .base_agent import BaseRemoteAgent


class _ToolDrivenRemoteAgent(BaseRemoteAgent):
    """Execute one of the tools explicitly assigned to a registered remote Agent."""

    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any,
    ) -> Dict[str, Any]:
        if not tools:
            raise ValueError(f"No tools configured for {self.name}")

        prompt = str(context.get("agent_prompt") or self.prompt)
        if len(tools) == 1:
            selected_tool = tools[0]
            arguments = await parameter_extractor.extract(
                agent_name=self.name,
                agent_prompt=prompt,
                tool=selected_tool,
                messages=messages,
            )
        else:
            selected_tool, arguments = await parameter_extractor.select_tool_and_extract(
                agent_name=self.name,
                agent_prompt=prompt,
                tools=tools,
                messages=messages,
            )

        return await self.call_tool(
            tool_name=str(selected_tool["name"]),
            arguments=arguments,
            timeout=20,
        )


class RemoteWeatherAgent(_ToolDrivenRemoteAgent):
    def __init__(self) -> None:
        super().__init__(
            name="RemoteWeatherAgent",
            prompt="查询用户指定地点的天气，只调用已配置的天气查询工具。",
        )


class RemoteScheduleAgent(_ToolDrivenRemoteAgent):
    def __init__(self) -> None:
        super().__init__(
            name="RemoteScheduleAgent",
            prompt="创建、更新或删除用户明确要求的日程。",
        )


class RemoteTodoAgent(_ToolDrivenRemoteAgent):
    def __init__(self) -> None:
        super().__init__(
            name="RemoteTodoAgent",
            prompt="查询或管理用户的待办事项，并准确提取日期范围和状态。",
        )


class RemoteHRCalendarAgent(_ToolDrivenRemoteAgent):
    def __init__(self) -> None:
        super().__init__(
            name="RemoteHRCalendarAgent",
            prompt=(
                "查询或创建员工日程。查询时把相对时间换算成 YYYY-MM-DD 日期范围；"
                "创建时提取事项、日期、时间和备注。当前日期：<<CURRENT_DATE>>。"
            ),
        )
