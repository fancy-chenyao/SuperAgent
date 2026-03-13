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

from src.interface.agent import COORDINATOR, PLANNER, PUBLISHER
from src.llm.agents import AGENT_LLM_MAP
from src.interface.agent import State, Router
from src.manager import agent_manager
from src.workflow.graph import AgentWorkflow
from src.workflow.cache import workflow_cache as cache
from src.utils.content_process import clean_response_tags
from src.manager.executor.base import ExecutionContext
from src.manager.executor.factory import execute_agent

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

def _extract_plan_steps(content: str) -> list | None:
    if not content:
        return None

    def _try_parse(value: str):
        try:
            return json.loads(value)
        except Exception:
            return None

    text = content.strip()
    candidates = [text]

    first_obj = text.find("{")
    last_obj = text.rfind("}")
    if first_obj >= 0 and last_obj > first_obj:
        candidates.append(text[first_obj : last_obj + 1])

    first_arr = text.find("[")
    last_arr = text.rfind("]")
    if first_arr >= 0 and last_arr > first_arr:
        candidates.append(text[first_arr : last_arr + 1])

    for candidate in candidates:
        parsed = _try_parse(candidate)
        if parsed is None:
            continue
        if isinstance(parsed, dict):
            steps = parsed.get("steps") or parsed.get("planning_steps")
            if isinstance(steps, list):
                return steps
        if isinstance(parsed, list):
            return parsed
    return None


async def publisher_node(
    state: State,
) -> Command[Literal["agent_proxy", "__end__"]]:
    """Publisher node."""
    logger.info("publisher evaluating next action in %s mode ", state["workflow_mode"])

    if state["workflow_mode"] == "launch":
        cache.restore_system_node(state["workflow_id"], PUBLISHER, state["user_id"])
        messages = apply_prompt_template("publisher", state)
        response = await (
            get_llm_by_type(AGENT_LLM_MAP["publisher"])
            .with_structured_output(Router)
            .ainvoke(messages)
        )

        try:
            agent = response["next"]
        except Exception as e:
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
            cache.restore_node(
                state["workflow_id"], goto, state["initialized"], state["user_id"]
            )
            return Command(goto=goto, update={"next": goto})

        cache.restore_system_node(state["workflow_id"], agent, state["user_id"])
        goto = "agent_proxy"

        logger.info("publisher delegating to: %s ", agent)
        cache.restore_node(
            state["workflow_id"], agent, state["initialized"], state["user_id"]
        )

    elif state["workflow_mode"] in ["production", "polish"]:
        agent = cache.get_next_node(state["workflow_id"])
        if agent == "FINISH":
            goto = "__end__"
            logger.info("Workflow completed \n")
            return Command(goto=goto, update={"next": goto})
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
    """Proxy node that executes the selected agent."""
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
        cache.restore_node(
            state["workflow_id"], _agent, state["initialized"], state["user_id"]
        )
    elif state["workflow_mode"] == "production":
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
    """Planner node that generates the plan."""
    logger.info("Planner generating full plan in %s mode", state["workflow_mode"])

    content = ""
    goto = "publisher"
    retry_messages = None
    retry_llm = None

    if state["workflow_mode"] == "launch":
        prompt_state = dict(state)
        history = prompt_state.get("instruction_history") or []
        if not isinstance(history, list):
            history = [str(history)]
        if history:
            history_text = "\n".join(f"{idx + 1}. {item}" for idx, item in enumerate(history))
        else:
            history_text = "None"

        current_plan = cache.get_planning_steps(state["workflow_id"])
        if isinstance(current_plan, str):
            try:
                current_plan = json.loads(current_plan)
            except Exception:
                current_plan = []
        if not isinstance(current_plan, list):
            current_plan = []
        current_plan_text = (
            json.dumps({"steps": current_plan}, indent=2, ensure_ascii=False)
            if current_plan
            else "[]"
        )

        prompt_state["INSTRUCTION_HISTORY_TEXT"] = history_text
        prompt_state["CURRENT_PLAN_TEXT"] = current_plan_text
        messages = apply_prompt_template("planner", prompt_state)
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
        cache.restore_system_node(state["workflow_id"], PLANNER, state["user_id"])
        response = llm.stream(messages)
        for chunk in response:
            if chunk.content:
                content += chunk.content  # type: ignore
        content = clean_response_tags(content)
        retry_messages = messages
        retry_llm = llm
    elif state["workflow_mode"] == "production":
        content = json.dumps(
            cache.get_planning_steps(state["workflow_id"]), indent=4, ensure_ascii=False
        )

    elif state["workflow_mode"] == "polish" and state.get("polish_target") == "planner":
        state["historical_plan"] = cache.get_planning_steps(state["workflow_id"])
        state["adjustment_instruction"] = state.get("polish_instruction")

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

    raw_content = content
    message_content = content

    if state["workflow_mode"] in ["launch", "polish"]:
        steps = _extract_plan_steps(raw_content)
        if steps is None and state["workflow_mode"] == "launch" and retry_messages and retry_llm:
            try:
                retry_note = (
                    "仅输出JSON格式的计划，不要解释或补充文字。"
                    "必须使用 {\"steps\": [...]} 结构。"
                )
                retry_payload = deepcopy(retry_messages)
                retry_payload.append({"role": "user", "content": retry_note})
                retry_response = await retry_llm.ainvoke(retry_payload)
                retry_content = clean_response_tags(getattr(retry_response, "content", ""))
                if retry_content:
                    steps = _extract_plan_steps(retry_content)
                    if steps is not None:
                        raw_content = retry_content
            except Exception as exc:
                logger.warning("Planner retry failed: %s", exc)
        if steps is not None:
            cache.restore_planning_steps(state["workflow_id"], steps, state["user_id"])
            message_content = json.dumps({"steps": steps}, indent=2, ensure_ascii=False)
            if state.get("stop_after_planner") and state["workflow_mode"] == "launch":
                goto = "__end__"
        else:
            logger.warning("Planner response is not a valid JSON \n")
            goto = "__end__"
        cache.restore_system_node(state["workflow_id"], goto, state["user_id"])
    return Command(
        update={
            "messages": [{"content": message_content, "tool": "planner", "role": "assistant"}],
            "agent_name": "planner",
            "full_plan": raw_content,
        },
        goto=goto,
    )


async def coordinator_node(state: State) -> Command[Literal["planner", "__end__"]]:
    """Coordinator node."""
    logger.info("Coordinator talking. \n")

    goto = "__end__"
    content = ""

    messages = apply_prompt_template("coordinator", state)
    response = await get_llm_by_type(AGENT_LLM_MAP["coordinator"]).ainvoke(messages)
    if state["workflow_mode"] == "launch":
        cache.restore_system_node(state["workflow_id"], COORDINATOR, state["user_id"])

    content = clean_response_tags(response.content)  # type: ignore
    if "handover_to_planner" in content:
        goto = "planner"
    if state["workflow_mode"] == "launch":
        cache.restore_system_node(state["workflow_id"], "planner", state["user_id"])
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
    """Build and return the agent workflow graph."""
    workflow = AgentWorkflow()
    workflow.add_node("coordinator", coordinator_node)  # type: ignore
    workflow.add_node("planner", planner_node)  # type: ignore
    workflow.add_node("publisher", publisher_node)  # type: ignore
    workflow.add_node("agent_proxy", agent_proxy_node)  # type: ignore

    workflow.set_start("coordinator")
    return workflow.compile()
