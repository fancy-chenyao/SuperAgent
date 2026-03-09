import logging
import json
from copy import deepcopy
from typing import Literal
from langgraph.types import Command

from src.llm.llm import get_llm_by_type
from src.llm.agents import AGENT_LLM_MAP
from src.prompts.template import apply_prompt_template
from src.tools.search import tavily_tool
from src.interface.agent import State, Router
from src.interface.serializer import AgentBuilder
from src.manager import agent_manager
from src.workflow.graph import AgentWorkflow
from src.utils.content_process import clean_response_tags

logger = logging.getLogger(__name__)

RESPONSE_FORMAT = (
    "Response from {}:\n\n<response>\n{}\n</response>\n\n"
    "*Please execute the next step.*"
)


async def agent_factory_node(state: State) -> Command[Literal["publisher", "__end__"]]:
    """Node for the create agent agent that creates a new agent."""
    logger.info("Agent Factory Start to work")

    tools = []
    messages = apply_prompt_template("agent_factory", state)
    agent_spec = await (
        get_llm_by_type(AGENT_LLM_MAP["agent_factory"])
        .with_structured_output(AgentBuilder)
        .ainvoke(messages)
    )
  
    for tool in agent_spec["selected_tools"]:
        if agent_manager.available_tools.get(tool["name"]):
            tools.append(agent_manager.available_tools[tool["name"]])
        else:
            logger.warning("Tool (%s) is not available", tool["name"])

    await agent_manager._create_agent_by_prebuilt(
        user_id=state["user_id"],
        name=agent_spec["agent_name"],
        nick_name=agent_spec["agent_name"],
        llm_type=agent_spec["llm_type"],
        tools=tools,
        prompt=agent_spec["prompt"],
        description=agent_spec["agent_description"],
    )

    state["TEAM_MEMBERS"].append(agent_spec["agent_name"])

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
        goto="__end__",
    )


async def publisher_node(state: State) -> Command[Literal["agent_factory", "__end__"]]:
    """publisher node that decides which agent should act next."""
    logger.info("publisher evaluating next action")

    goto = "__end__"
    messages = apply_prompt_template("publisher", state)
    response = await (
        get_llm_by_type(AGENT_LLM_MAP["publisher"])
        .with_structured_output(Router)
        .ainvoke(messages)
    )
    agent = response["next"]
    if agent == "FINISH":
        logger.info("Workflow completed \n")
        return Command(goto=goto, update={"next": goto})
    elif agent != "agent_factory":
        logger.warning("Agent Factory task restricted: cannot be executed by %s", agent)
        return Command(goto=goto, update={"next": "FINISH"})
    else:
        goto = "agent_factory"
        logger.info("publisher delegating to: %s", agent)
        return Command(goto=goto, update={"next": agent})


async def planner_node(state: State) -> Command[Literal["publisher", "__end__"]]:
    """Planner node that generate the full plan."""
    logger.info("Planner generating full plan \n")

    goto = "publisher"
    content = ""

    messages = apply_prompt_template("agent_factory_planner", state)
    llm = get_llm_by_type(AGENT_LLM_MAP["planner"])
    if state.get("deep_thinking_mode"):
        llm = get_llm_by_type("reasoning")
    if state.get("search_before_planning"):
        searched_content = await tavily_tool.ainvoke(
            {"query": state["messages"][-1]["content"]}
        )
        messages = deepcopy(messages)
        messages[-1]["content"] += (
            f"\n\n# Relative Search Results\n\n{json.dumps([{'titile': elem['title'], 'content': elem['content']} for elem in searched_content], ensure_ascii=False)}"
        )

    try:
        response = await llm.ainvoke(messages)
        content = clean_response_tags(response.content)  # type: ignore
        json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Planner response is not a valid JSON")
        goto = "__end__"
    except Exception as e:
        logger.error("Error in planner node: %s", e)
        content = f"Error in planner node: {e}"
        goto = "__end__"

    return Command(
        update={
            "messages": [{"content": content, "tool": "planner", "role": "assistant"}],
            "agent_name": "planner",
            "full_plan": content,
        },
        goto=goto,
    )


async def coordinator_node(state: State) -> Command[Literal["planner", "__end__"]]:
    """Coordinator node that communicate with customers."""
    logger.info("Coordinator talking. \n")

    goto = "__end__"
    messages = apply_prompt_template("coordinator", state)
    response = await get_llm_by_type(AGENT_LLM_MAP["coordinator"]).ainvoke(messages)

    content = clean_response_tags(response.content)  # type: ignore
    if "handover_to_planner" in content:
        goto = "planner"

    return Command(
        update={
            "messages": [
                {"content": content, "tool": "coordinator", "role": "assistant"}
            ],
            "agent_name": "coordinator",
        },
        goto=goto,
    )


def agent_factory_graph():
    workflow = AgentWorkflow()
    workflow.add_node("coordinator", coordinator_node)  # type: ignore
    workflow.add_node("planner", planner_node)  # type: ignore
    workflow.add_node("publisher", publisher_node)  # type: ignore
    workflow.add_node("agent_factory", agent_factory_node)  # type: ignore

    workflow.set_start("coordinator")
    return workflow.compile()
