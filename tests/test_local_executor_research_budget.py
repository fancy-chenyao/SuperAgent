import pytest
from langchain_core.messages import AIMessage, ToolMessage

from src.manager.executor.local import LocalExecutor


class _FakeResearchGraph:
    async def astream(self, state, config, stream_mode):
        first_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "tavily_tool",
                    "args": {"query": "李娜公开信息"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        )
        first_result = ToolMessage(
            content="第一批搜索结果",
            tool_call_id="call-1",
        )
        yield {"messages": [*state["messages"], first_call, first_result]}

        repeated_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "tavily_tool",
                    "args": {"query": "李娜公开信息"},
                    "id": "call-2",
                    "type": "tool_call",
                }
            ],
        )
        repeated_result = ToolMessage(
            content="第一批搜索结果",
            tool_call_id="call-2",
        )
        yield {
            "messages": [
                *state["messages"],
                first_call,
                first_result,
                repeated_call,
                repeated_result,
            ]
        }

        raise AssertionError("重复工具调用后应当停止，不应继续遍历")


class _FakeFinalizer:
    def __init__(self):
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        return AIMessage(content="已根据现有搜索结果生成简短报告")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_research_loop_stops_on_repeated_identical_tool_call():
    executor = LocalExecutor()
    finalizer = _FakeFinalizer()

    result = await executor._invoke_bounded_research_agent(
        react_agent=_FakeResearchGraph(),
        llm=finalizer,
        prompt="你是研究助手",
        state={"messages": []},
        config={"recursion_limit": 25},
    )

    assert result["messages"][-1].content == "已根据现有搜索结果生成简短报告"
    assert "不要再调用任何工具" in finalizer.messages[-1].content


class _ChangingResultGraph:
    def __init__(self):
        self.completed = False

    async def astream(self, state, config, stream_mode):
        first_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "tavily_tool",
                    "args": {"query": "李娜公开信息"},
                    "id": "changing-1",
                    "type": "tool_call",
                }
            ],
        )
        first_result = ToolMessage(
            content="第一批结果",
            tool_call_id="changing-1",
        )
        yield {"messages": [*state["messages"], first_call, first_result]}

        second_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "tavily_tool",
                    "args": {"query": "李娜公开信息"},
                    "id": "changing-2",
                    "type": "tool_call",
                }
            ],
        )
        second_result = ToolMessage(
            content="第二批新增结果",
            tool_call_id="changing-2",
        )
        yield {
            "messages": [
                *state["messages"],
                first_call,
                first_result,
                second_call,
                second_result,
            ]
        }
        self.completed = True
        yield {
            "messages": [
                *state["messages"],
                first_call,
                first_result,
                second_call,
                second_result,
                AIMessage(content="正常完成研究"),
            ]
        }


@pytest.mark.anyio
async def test_same_tool_and_arguments_continue_when_result_changes():
    executor = LocalExecutor()
    graph = _ChangingResultGraph()
    finalizer = _FakeFinalizer()

    result = await executor._invoke_bounded_research_agent(
        react_agent=graph,
        llm=finalizer,
        prompt="你是研究助手",
        state={"messages": []},
        config={"recursion_limit": 25},
    )

    assert graph.completed is True
    assert result["messages"][-1].content == "正常完成研究"
    assert finalizer.messages is None


def test_only_search_agents_use_bounded_research_loop():
    class _Tool:
        def __init__(self, name):
            self.name = name

    assert LocalExecutor._uses_research_tool_loop(
        [_Tool("tavily_tool"), _Tool("crawl_tool")]
    )
    assert not LocalExecutor._uses_research_tool_loop([_Tool("python_repl_tool")])
