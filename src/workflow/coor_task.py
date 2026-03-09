import logging
import json
from copy import deepcopy
from langgraph.types import Command
from typing import Literal
from src.interface.agent import COORDINATOR, PLANNER, PUBLISHER, AGENT_FACTORY
from src.llm.llm import get_llm_by_type
from src.llm.agents import AGENT_LLM_MAP
from src.prompts.template import apply_prompt_template
from src.tools.search import tavily_tool
from src.interface.agent import State, Router
from src.manager import agent_manager
from src.prompts.template import apply_prompt
from langgraph.prebuilt import create_react_agent
from src.workflow.graph import AgentWorkflow
from src.service.env import MAX_STEPS
from src.workflow.cache import workflow_cache as cache
from src.utils.content_process import clean_response_tags
from src.interface.serializer import AgentBuilder


logger = logging.getLogger(__name__)


async def agent_factory_node(state: State) -> Command[Literal["publisher", "__end__"]]:
    """Node for the create agent agent that creates a new agent."""
    logger.info("Agent Factory Start to work in %s workmode", state["workflow_mode"])

    goto = "publisher"
    tools = []

    if state["workflow_mode"] == "launch":
        cache.restore_system_node(state["workflow_id"], AGENT_FACTORY, state["user_id"])
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
    """publisher node that decides which agent should act next."""
    logger.info("publisher evaluating next action in %s mode ", state["workflow_mode"])

    if state["workflow_mode"] == "launch":
        cache.restore_system_node(state["workflow_id"], PUBLISHER, state["user_id"])
        messages = apply_prompt_template("publisher", state)
        response = await (
            get_llm_by_type(AGENT_LLM_MAP["publisher"])
            .with_structured_output(Router)
            .ainvoke(messages)
        )
        agent = response["next"]

        if agent == "FINISH":
            goto = "__end__"
            logger.info("Workflow completed \n")
            cache.restore_node(
                state["workflow_id"], goto, state["initialized"], state["user_id"]
            )
            return Command(goto=goto, update={"next": goto})
        elif agent != "agent_factory":
            cache.restore_system_node(state["workflow_id"], agent, state["user_id"])
            goto = "agent_proxy"
        else:
            cache.restore_system_node(
                state["workflow_id"], "agent_factory", state["user_id"]
            )
            goto = "agent_factory"

        logger.info("publisher delegating to: %s ", agent)

        cache.restore_node(
            state["workflow_id"], agent, state["initialized"], state["user_id"]
        )

    elif state["workflow_mode"] in ["production", "polish"]:
        # todo add polish history
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
    """Proxy node that acts as a proxy for the agent."""
    logger.info(
        "Agent Proxy Start to work in %s workmode, %s agent is going to work",
        state["workflow_mode"],
        state["next"],
    )

    _agent = agent_manager.available_agents[state["next"]]
    state["initialized"] = True

    agent = create_react_agent(
        get_llm_by_type(_agent.llm_type),
        tools=[
            agent_manager.available_tools[tool.name] for tool in _agent.selected_tools
        ],
        prompt=apply_prompt(state, _agent.prompt),
    )

    # Create config with user_id for tool notifications
    config = {
        "configurable": {"user_id": state.get("user_id")},
        "recursion_limit": int(MAX_STEPS),
    }

    response = await agent.ainvoke(state, config=config)

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
                    "content": response["messages"][-1].content,
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
    """Planner node that generate the full plan."""
    logger.info("Planner generating full plan in %s mode", state["workflow_mode"])

    content = ""
    goto = "publisher"

    if state["workflow_mode"] == "launch":
        messages = apply_prompt_template("planner", state)
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
    elif state["workflow_mode"] == "production":
        # watch out the json style
        content = json.dumps(
            cache.get_planning_steps(state["workflow_id"]), indent=4, ensure_ascii=False
        )

    elif state["workflow_mode"] == "polish" and state["polish_target"] == "planner":
        # this will be support soon
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
            steps_obj = json.loads(content)
            steps = steps_obj.get("steps", [])
            cache.restore_planning_steps(state["workflow_id"], steps, state["user_id"])
        except json.JSONDecodeError:
            logger.warning("Planner response is not a valid JSON \n")
            goto = "__end__"
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
    """Coordinator node that communicate with customers."""
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
    workflow.add_node("agent_factory", agent_factory_node)  # type: ignore
    workflow.add_node("agent_proxy", agent_proxy_node)  # type: ignore

    workflow.set_start("coordinator")
    return workflow.compile()
