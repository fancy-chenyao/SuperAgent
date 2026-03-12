import logging
import json
from copy import deepcopy
from typing import Literal
try:
    from langgraph.types import Command
except Exception:  # pragma: no cover - optional dependency in lightweight test env
    class Command:  # type: ignore
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, update=None, goto=None):
            self.update = update or {}
            self.goto = goto
from src.interface.agent import COORDINATOR, PLANNER, PUBLISHER, AGENT_FACTORY
from src.llm.agents import AGENT_LLM_MAP
from src.interface.agent import State, Router
from src.manager import agent_manager
from src.workflow.graph import AgentWorkflow
from src.workflow.cache import workflow_cache as cache
from src.utils.content_process import clean_response_tags
from src.interface.serializer import AgentBuilder
from src.manager.executor.base import ExecutionContext
from src.manager.executor.factory import execute_agent
from src.manager.registry import ToolRegistry
from src.manager.resource import get_resource_registry
from src.manager.executor.remote_tool_proxy import RemoteToolProxy

try:
    from src.llm.llm import get_llm_by_type
except Exception:  # pragma: no cover - optional dependency in lightweight test env
    def get_llm_by_type(*args, **kwargs):  # type: ignore
        raise RuntimeError("LLM dependencies are not installed")

try:
    from src.prompts.template import apply_prompt_template
except Exception:  # pragma: no cover - optional dependency in lightweight test env
    def apply_prompt_template(*args, **kwargs):  # type: ignore
        return []

try:
    from src.tools.search import tavily_tool
except Exception:  # pragma: no cover - optional dependency in lightweight test env
    class _NoopTavilyTool:  # type: ignore
        def invoke(self, *args, **kwargs):
            return []

        async def ainvoke(self, *args, **kwargs):
            return []

    tavily_tool = _NoopTavilyTool()


logger = logging.getLogger(__name__)


async def _resolve_tools_by_names(tool_names: list[str]) -> list:
    registry = await ToolRegistry.get_instance()
    resource_registry = await get_resource_registry()
    global_tools = await registry.list_global_tools()
    tool_map = {
        getattr(meta.tool, "name", ""): meta.tool
        for meta in global_tools
        if getattr(meta.tool, "name", "")
    }

    resolved = []
    for name in tool_names:
        if name in tool_map:
            resolved.append(tool_map[name])
            continue

        specs = await resource_registry.list(type="tool")
        matched = next((spec for spec in specs if spec.name == name), None)
        if matched and matched.server_id != "local":
            resolved.append(RemoteToolProxy(matched, registry))
            continue

        logger.warning("Tool (%s) is not available", name)
    return resolved


async def agent_factory_node(state: State) -> Command[Literal["publisher", "__end__"]]:
    """
    代理工厂节点（agent_factory）：
    - 职责：在“launch”模式下，根据大模型返回的 AgentBuilder 结构，创建新的执行代理，并将其加入可用代理与团队成员列表。
    - 输入：State（包含 workflow_id、user_id、messages、workflow_mode 等上下文）
    - 输出：Command，goto 固定为 "publisher"；update 会写入 messages、新创建的代理名称等。
    - 关键点：
      1) 仅在 launch 模式执行实际的“创建代理”逻辑；其他模式当前留空（保留扩展）。
      2) 使用 with_structured_output(AgentBuilder) 约束模型返回结构化字段，便于后续解析。
      3) 从 agent_spec['selected_tools'] 中映射工具，写入到新代理配置中。
    """
    logger.info("Agent Factory Start to work in %s workmode", state["workflow_mode"])

    goto = "publisher"
    await agent_manager.ensure_initialized()

    if state["workflow_mode"] == "launch":
        # 恢复系统节点：将 agent_factory 作为系统节点写回 workflow 缓存（用于后续图可视化/还原）
        cache.restore_system_node(state["workflow_id"], AGENT_FACTORY, state["user_id"])
        # 基于模板与上下文拼接提示词
        messages = apply_prompt_template("agent_factory", state)
        # 请求大模型生成 AgentBuilder（包含代理名、描述、所需工具、提示词等）
        agent_spec = await (
            get_llm_by_type(AGENT_LLM_MAP["agent_factory"])
            .with_structured_output(AgentBuilder)
            .ainvoke(messages)
        )

        selected_tool_names = [tool["name"] for tool in agent_spec["selected_tools"]]
        tools = await _resolve_tools_by_names(selected_tool_names)
                
        # 创建代理并注册到系统
        await agent_manager._create_agent_by_prebuilt(
            user_id=state["user_id"],
            name=agent_spec["agent_name"],
            nick_name=agent_spec["agent_name"],
            llm_type=agent_spec["llm_type"],
            tools=tools,
            prompt=agent_spec["prompt"],
            description=agent_spec["agent_description"],
        )
        # 将新代理加入团队成员列表，用于后续路由/展示
        state["TEAM_MEMBERS"].append(agent_spec["agent_name"])

    elif state["workflow_mode"] == "polish":
        # this will be support soon
        pass

    return Command(
        update={
            "messages": [
                {
                    "content": f"New agent {agent_spec['agent_name']} created. \n",
                    "tool": "agent_factory",
                    "role": "assistant",
                }
            ],
            "new_agent_name": agent_spec["agent_name"],
            "agent_name": "agent_factory",
        },
        goto=goto,
    )


async def publisher_node(
    state: State,
) -> Command[Literal["agent_proxy", "agent_factory", "__end__"]]:
    """
    发布者节点（publisher）：
    - 职责：根据规划（steps）与当前进度，决定“下一步执行的代理”并进行路由。
    - 输入：State（包含 full_plan/steps、上一轮 next 等）
    - 输出：Command，其中：
      - 当判定结束时，goto="__end__"
      - 否则返回 "agent_proxy" 或 "agent_factory"
    - 关键点：
      1) 在 launch 模式下，使用 with_structured_output(Router) 要求模型仅返回 {"next": "..."}。
      2) 响应中的 "next" 可能为 "FINISH"、"agent_factory" 或具体代理名。
      3) 在 production/polish 模式下，改为从缓存中读取下一节点（get_next_node）。
    """
    logger.info("publisher evaluating next action in %s mode ", state["workflow_mode"])

    if state["workflow_mode"] == "launch":
        # 将 publisher 标记为系统节点，便于在工作流图中复原
        cache.restore_system_node(state["workflow_id"], PUBLISHER, state["user_id"])
        # 组装 publisher 提示词，输入包含 steps 等关键上下文
        messages = apply_prompt_template("publisher", state)
        # 结构化输出，仅返回 {"next": "..."}，类型为 Router
        response = await (
            get_llm_by_type(AGENT_LLM_MAP["publisher"])
            .with_structured_output(Router)
            .ainvoke(messages)
        )

        # 期望 response 是 dict-like（Router）；若异常将导致 TypeError
        try:
            agent = response["next"]
        except Exception as e:
            # 当结构化解析失败时，尽可能序列化并打印/记录原始 response，随后重新抛出异常
            try:
                preview = response.model_dump() if hasattr(response, "model_dump") else response
                try:
                    preview_str = json.dumps(preview, ensure_ascii=False)
                except Exception:
                    preview_str = str(preview)
                logger.error(f"publisher response parse error: {e}; response={preview_str}")
            except Exception as inner:
                logger.error(f"publisher response parse error and printing failed: {inner}")
            raise

        if agent == "FINISH":
            goto = "__end__"
            logger.info("Workflow completed \n")
            # 记录结束节点到图结构（launch 模式）
            cache.restore_node(
                state["workflow_id"], goto, state["initialized"], state["user_id"]
            )
            return Command(goto=goto, update={"next": goto})
        elif agent != "agent_factory":
            # 目标为普通执行代理，由 agent_proxy 代理实际执行
            cache.restore_system_node(state["workflow_id"], agent, state["user_id"])
            goto = "agent_proxy"
        else:
            # 目标为 agent_factory，继续走创建流程
            cache.restore_system_node(
                state["workflow_id"], "agent_factory", state["user_id"]
            )
            goto = "agent_factory"

        logger.info("publisher delegating to: %s ", agent)

        # 在工作流图中补全边（当前节点 -> 下一节点）
        cache.restore_node(
            state["workflow_id"], agent, state["initialized"], state["user_id"]
        )

    elif state["workflow_mode"] in ["production", "polish"]:
        # todo add polish history
        # 生产/打磨模式：根据已保存的工作流图（queue）进行顺序恢复
        agent = cache.get_next_node(state["workflow_id"])
        if agent == "FINISH":
            goto = "__end__"
            logger.info("Workflow completed \n")
            return Command(goto=goto, update={"next": goto})
        else:
            goto = "agent_proxy"
    logger.info("publisher delegating to: %s", agent)

    return Command(
        goto=goto,
        update={
            "messages": [
                {
                    "content": f"Next step is delegating to: {agent}\n",
                    "tool": "publisher",
                    "role": "assistant",
                }
            ],
            "next": agent,
        },
    )


async def agent_proxy_node(state: State) -> Command[Literal["publisher", "__end__"]]:
    """
    代理执行节点（agent_proxy）：
    - 职责：根据 state['next'] 找到对应的执行代理，构建 REACT Agent 执行具体任务。
    - 输入：State（包含 next、TEAM_MEMBERS、messages 等）
    - 输出：Command，goto 固定回到 "publisher"，以便继续路由。
    - 关键点：
      1) 使用 create_react_agent 结合代理的工具与 prompt，执行 ainvoke。
      2) 将结果消息写入 state['messages']，并记录 processing_agent_name 等信息。
      3) 在 production 模式下，会更新工作流队列（update_stack）。
    """
    logger.info(
        "Agent Proxy Start to work in %s workmode, %s agent is going to work",
        state["workflow_mode"],
        state["next"],
    )

    await agent_manager.ensure_initialized()
    _agent = await agent_manager.agent_registry.get(state["next"])
    if _agent is None:
        raise KeyError(f"Agent not found in registry: {state['next']}")
    state["initialized"] = True

    context = ExecutionContext(
        user_id=state.get("user_id"),
        workflow_id=state.get("workflow_id"),
        workflow_mode=state.get("workflow_mode"),
        deep_thinking_mode=state.get("deep_thinking_mode", False),
    )
    execute_result = await execute_agent(_agent, state["messages"], context)
    response_content = execute_result.result if execute_result.is_success else execute_result.error

    if state["workflow_mode"] == "launch":
        # 在工作流图中记录本次执行的代理节点
        cache.restore_node(
            state["workflow_id"], _agent, state["initialized"], state["user_id"]
        )
    elif state["workflow_mode"] == "production":
        # 恢复执行时出栈
        cache.update_stack(state["workflow_id"], state["user_id"])

    return Command(
        update={
            "messages": [
                {
                    "content": response_content,
                    "tool": state["next"],
                    "role": "assistant",
                }
            ],
            "processing_agent_name": _agent.agent_name,
            "agent_name": _agent.agent_name,
        },
        goto="publisher",
    )


async def planner_node(state: State) -> Command[Literal["publisher", "__end__"]]:
    """
    规划器节点（planner）：
    - 职责：生成完整计划（steps），供 publisher 做路由决策。
    - 输入：State（包含 messages、deep_thinking_mode、search_before_planning 等）
    - 输出：Command，goto 通常为 "publisher"；update 会写入 full_plan 与消息。
    - 关键点：
      1) launch 模式下，流式获取模型输出并合并，最后做简单清洗（clean_response_tags）。
      2) 若启用 search_before_planning，会先用 tavily_tool 检索，将结果拼入提示词上下文。
      3) 解析 content 为 JSON，从中取 "steps" 并写入缓存；解析失败则告警并直接结束。
      4) production 模式下，从缓存读取历史 planning_steps 作为 content。
    """
    logger.info("Planner generating full plan in %s mode", state["workflow_mode"])

    content = ""
    goto = "publisher"

    if state["workflow_mode"] == "launch":
        # 组装 planner 提示词
        messages = apply_prompt_template("planner", state)
        llm = get_llm_by_type(AGENT_LLM_MAP["planner"])
        if state.get("deep_thinking_mode"):
            llm = get_llm_by_type("reasoning")
        if state.get("search_before_planning"):
            # 搜索增强：将搜索结果拼接到提示词末尾
            config = {"configurable": {"user_id": state.get("user_id")}}
            searched_content = tavily_tool.invoke(
                {
                    "query": [
                        "".join(message["content"])
                        for message in state["messages"]
                        if message["role"] == "user"
                    ][0]
                },
                config=config,
            )
            messages = deepcopy(messages)
            messages[-1]["content"] += (
                f"\n\n# Relative Search Results\n\n{json.dumps([{'titile': elem['title'], 'content': elem['content']} for elem in searched_content], ensure_ascii=False)}"
            )
        # 标记系统节点，便于复原
        cache.restore_system_node(state["workflow_id"], PLANNER, state["user_id"])
        # 流式获取规划内容
        response = llm.stream(messages)
        for chunk in response:
            if chunk.content:
                content += chunk.content  # type: ignore
        content = clean_response_tags(content)
    elif state["workflow_mode"] == "production":
        # watch out the json style
        # 从缓存恢复历史规划步骤（production 场景）
        content = json.dumps(
            cache.get_planning_steps(state["workflow_id"]), indent=4, ensure_ascii=False
        )

    elif state["workflow_mode"] == "polish" and state["polish_target"] == "planner":
        # this will be support soon
        # 打磨模式：读取历史规划与当前打磨指令，重新生成
        state["historical_plan"] = cache.get_planning_steps(state["workflow_id"])
        state["adjustment_instruction"] = state["polish_instruction"]

        messages = apply_prompt_template("planner_polishment", state)
        llm = get_llm_by_type(AGENT_LLM_MAP["planner"])
        if state.get("deep_thinking_mode"):
            llm = get_llm_by_type("reasoning")
        if state.get("search_before_planning"):
            config = {"configurable": {"user_id": state.get("user_id")}}
            searched_content = tavily_tool.invoke(
                {
                    "query": [
                        "".join(message["content"])
                        for message in state["messages"]
                        if message["role"] == "user"
                    ][0]
                },
                config=config,
            )
            messages = deepcopy(messages)
            messages[-1]["content"] += (
                f"\n\n# Relative Search Results\n\n{json.dumps([{'titile': elem['title'], 'content': elem['content']} for elem in searched_content], ensure_ascii=False)}"
            )

        response = await llm.ainvoke(messages)
        content = clean_response_tags(response.content)  # type: ignore
    # steps need to be stored in cache
    if state["workflow_mode"] in ["launch", "polish"]:
        try:
            # 将模型输出解析为 JSON，并持久化 steps
            steps_obj = json.loads(content)
            steps = steps_obj.get("steps", [])
            cache.restore_planning_steps(state["workflow_id"], steps, state["user_id"])
        except json.JSONDecodeError:
            logger.warning("Planner response is not a valid JSON \n")
            goto = "__end__"
        # 在图中接好“下一跳”占位（由 publisher 决策）
        cache.restore_system_node(state["workflow_id"], goto, state["user_id"])
    return Command(
        update={
            "messages": [{"content": content, "tool": "planner", "role": "assistant"}],
            "agent_name": "planner",
            "full_plan": content,
        },
        goto=goto,
    )


async def coordinator_node(state: State) -> Command[Literal["planner", "__end__"]]:
    """
    协调器节点（coordinator）：
    - 职责：与用户对话，决定是否将控制权交给 planner。
    - 输入：State（包含 messages 等）
    - 输出：Command，goto="planner" 或 "__end__"
    - 关键点：
      1) 使用模板生成系统消息，调用 basic LLM 获取内容。
      2) 当返回文本包含 "handover_to_planner" 时，转入 planner。
      3) 在 launch 模式下将 coordinator 与 planner 写入系统节点，便于图复原。
    """
    logger.info("Coordinator talking. \n")

    goto = "__end__"
    content = ""

    messages = apply_prompt_template("coordinator", state)
    response = await get_llm_by_type(AGENT_LLM_MAP["coordinator"]).ainvoke(messages)
    if state["workflow_mode"] == "launch":
        cache.restore_system_node(state["workflow_id"], COORDINATOR, state["user_id"]) # 写入cache

    content = clean_response_tags(response.content)  # type: ignore
    if "handover_to_planner" in content:
        goto = "planner"
    if state["workflow_mode"] == "launch":
        cache.restore_system_node(state["workflow_id"], "planner", state["user_id"]) # 写入cache
    return Command(
        update={
            "messages": [
                {"content": content, "tool": "coordinator", "role": "assistant"}
            ],
            "agent_name": "coordinator",
        },
        goto=goto,
    )


def build_graph():
    """
    构建工作流图（逻辑编排）：
    - 依次注册节点：coordinator → planner → publisher → agent_factory → agent_proxy
    - 起始节点：coordinator
    - 返回编译后的 CompiledWorkflow，可被流程引擎调度（见 graph.py）
    """
    workflow = AgentWorkflow()
    workflow.add_node("coordinator", coordinator_node)  # type: ignore
    workflow.add_node("planner", planner_node)  # type: ignore
    workflow.add_node("publisher", publisher_node)  # type: ignore
    workflow.add_node("agent_factory", agent_factory_node)  # type: ignore
    workflow.add_node("agent_proxy", agent_proxy_node)  # type: ignore

    workflow.set_start("coordinator")
    return workflow.compile()
