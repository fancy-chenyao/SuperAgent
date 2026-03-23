import logging
import json
import time
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
from src.workflow.parameter_extractor import extract_parameters_for_step

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
# Ensure planner performance logs are visible
if not logger.handlers:
    logger.setLevel(logging.INFO)

def _sanitize_messages(messages):
    if not isinstance(messages, list):
        return messages
    sanitized = []
    for msg in messages:
        if isinstance(msg, dict) and "content" in msg:
            content = msg.get("content")
            if not isinstance(content, (str, list)):
                try:
                    msg = dict(msg)
                    msg["content"] = json.dumps(content, ensure_ascii=False)
                except Exception:
                    msg = dict(msg)
                    msg["content"] = str(content)
        sanitized.append(msg)
    return sanitized

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
        messages = _sanitize_messages(apply_prompt_template("publisher", state))
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

    # Try to extract parameters if agent has requirements and plan has input mappings
    agent_requires = getattr(_agent, "requires", [])
    messages_to_send = state["messages"]

    if False:
        logger.info("=" * 80)
        logger.info(f"[PARAMETER EXTRACTION] Agent: {_agent.agent_name}")
        logger.info(f"[PARAMETER EXTRACTION] Agent requires: {agent_requires}")
        logger.info(f"[PARAMETER EXTRACTION] Agent source: {_agent.source.value}")
        logger.info(f"[PARAMETER EXTRACTION] Original message count: {len(state['messages'])}")

    if agent_requires and _agent.source.value == "remote":
        # Find current step in planning_steps
        planning_steps = state.get("planning_steps", [])
        if False:
            logger.info(f"[PARAMETER EXTRACTION] Total planning steps: {len(planning_steps)}")

        current_step = None
        for step in planning_steps:
            if step.get("agent_name") == state["next"]:
                current_step = step
                break

        if current_step:
            if False:
                logger.info(f"[PARAMETER EXTRACTION] Found current step: {current_step.get('title')}")
                logger.info(f"[PARAMETER EXTRACTION] Step has inputs: {bool(current_step.get('inputs'))}")
                if current_step.get("inputs"):
                    logger.info(f"[PARAMETER EXTRACTION] Input mappings: {json.dumps(current_step.get('inputs'), ensure_ascii=False, indent=2)}")
        else:
            if False:
                logger.warning(f"[PARAMETER EXTRACTION] Could not find step for agent: {state['next']}")

        # Try to extract parameters if:
        # 1. Step has explicit input mappings, OR
        # 2. Agent requires parameters but step has no inputs (fallback to context extraction)
        should_extract = current_step and (
            current_step.get("inputs") or
            (agent_requires and not current_step.get("inputs"))
        )

        if should_extract:
            try:
                if False:
                    logger.info("[PARAMETER EXTRACTION] Starting parameter extraction...")

                # If step has explicit input mappings, use them
                if current_step.get("inputs"):
                    # Extract parameters based on input mappings
                    parameters = extract_parameters_for_step(
                        current_step,
                        agent_requires,
                        state["messages"]
                    )
                else:
                    # Fallback: Agent requires parameters but no input mappings
                    # Try to extract from user instruction or context
                    if False:
                        logger.info("[PARAMETER EXTRACTION] No input mappings, attempting context extraction...")
                    parameters = {}

                    # Get user instruction from state
                    user_instruction = state.get("messages", [{}])[0].get("content", "")

                    # Simple heuristic extraction for common patterns
                    for param in agent_requires:
                        if "person.query" in param:
                            # Extract person query from instruction (e.g., "行长秘书")
                            if "行长秘书" in user_instruction or "秘书" in user_instruction:
                                parameters[param] = "行长秘书"
                                if False:
                                    logger.info(f"[PARAMETER EXTRACTION] Extracted {param} = '行长秘书' from user instruction")
                        elif "unicorn.query" in param:
                            # Extract unicorn query from instruction
                            if "独角兽" in user_instruction:
                                parameters[param] = "独角兽企业"
                                if False:
                                    logger.info(f"[PARAMETER EXTRACTION] Extracted {param} = '独角兽企业' from user instruction")
                        # Add more patterns as needed

                    if not parameters:
                        if False:
                            logger.warning(f"[PARAMETER EXTRACTION] Could not extract required parameters {agent_requires} from context")

                if parameters:
                    if False:
                        logger.info(f"[PARAMETER EXTRACTION] ✓ Successfully extracted parameters:")
                        logger.info(f"[PARAMETER EXTRACTION]   {json.dumps(parameters, ensure_ascii=False, indent=2)}")

                    # Transform parameter names using agent's parameter_mapping
                    # This ensures compatibility with tools that expect specific field names
                    transformed_params = {}
                    parameter_mapping = getattr(_agent, "parameter_mapping", None) or {}

                    if parameter_mapping:
                        if False:
                            logger.info(f"[PARAMETER EXTRACTION] Using parameter_mapping: {json.dumps(parameter_mapping, ensure_ascii=False)}")
                        for param_name, param_value in parameters.items():
                            # Use mapping if available, otherwise keep original name
                            mapped_name = parameter_mapping.get(param_name, param_name)
                            transformed_params[mapped_name] = param_value
                    else:
                        if False:
                            logger.info("[PARAMETER EXTRACTION] No parameter_mapping found, using fallback strategy")
                        # Fallback: remove prefix (e.g., "email.to" -> "to")
                        for param_name, param_value in parameters.items():
                            simple_name = param_name.split(".")[-1] if "." in param_name else param_name
                            transformed_params[simple_name] = param_value

                    if False:
                        logger.info(f"[PARAMETER EXTRACTION] ✓ Transformed parameters:")
                        logger.info(f"[PARAMETER EXTRACTION]   {json.dumps(transformed_params, ensure_ascii=False, indent=2)}")

                    # Create a clean message with just the transformed parameters
                    messages_to_send = [
                        {
                            "role": "user",
                            "content": json.dumps(transformed_params, ensure_ascii=False)
                        }
                    ]
                    if False:
                        logger.info(f"[PARAMETER EXTRACTION] ✓ Created clean message with {len(messages_to_send)} message(s)")
                else:
                    if False:
                        logger.warning("[PARAMETER EXTRACTION] ✗ No parameters extracted")
            except Exception as e:
                if False:
                    logger.error(f"[PARAMETER EXTRACTION] ✗ Failed to extract parameters: {e}")
                    logger.error(f"[PARAMETER EXTRACTION] Exception details:", exc_info=True)
                    logger.warning("[PARAMETER EXTRACTION] Falling back to sending all messages")
        else:
            if not current_step:
                if False:
                    logger.info("[PARAMETER EXTRACTION] Skipping extraction: current step not found")
            elif not current_step.get("inputs"):
                if False:
                    logger.info("[PARAMETER EXTRACTION] Skipping extraction: no input mappings in step")
    else:
        if not agent_requires:
            if False:
                logger.info("[PARAMETER EXTRACTION] Skipping extraction: agent has no requirements")
        elif _agent.source.value != "remote":
            if False:
                logger.info(f"[PARAMETER EXTRACTION] Skipping extraction: agent is not remote (source={_agent.source.value})")

    if False:
        logger.info(f"[PARAMETER EXTRACTION] Final message count to send: {len(messages_to_send)}")
        logger.info("=" * 80)

    execute_result = await execute_agent(_agent, messages_to_send, context)
    response_content = execute_result.result if execute_result.is_success else execute_result.error
    if response_content is None:
        response_content = ""
    raw_payload = response_content
    if not isinstance(response_content, str):
        try:
            response_content = json.dumps(response_content, ensure_ascii=False)
        except Exception:
            response_content = str(response_content)

    # Build a structured payload to preserve tool results across publisher hops.
    structured_result: Dict[str, Any] = {"tool": state["next"]}
    parsed_json: Optional[Any] = None
    if isinstance(response_content, str):
        stripped = response_content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed_json = json.loads(stripped)
            except Exception:
                parsed_json = None
    if parsed_json is not None:
        structured_result["result"] = parsed_json
    else:
        structured_result["result"] = raw_payload

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
                ,
                {
                    "content": structured_result,
                    "tool": "agent_proxy",
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
    start_time = time.time()
    logger.info("=" * 60)
    logger.info("PLANNER PERFORMANCE TRACKING START")
    logger.info("Mode: %s", state["workflow_mode"])
    logger.info("=" * 60)

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
        messages = _sanitize_messages(apply_prompt_template("planner", prompt_state))
        llm = get_llm_by_type(AGENT_LLM_MAP["planner"])
        if state.get("deep_thinking_mode"):
            llm = get_llm_by_type("reasoning")

        # Log LLM preparation time
        prep_time = time.time()
        prep_duration = prep_time - start_time
        logger.info("[PERF] Prompt preparation: %.2fs", prep_duration)

        if state.get("search_before_planning"):
            search_start = time.time()
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
            search_time = time.time() - search_start
            logger.info("[PERF] Web search: %.2fs", search_time)

            messages = deepcopy(messages)
            messages[-1]["content"] += (
                f"\n\n# Relative Search Results\n\n{json.dumps([{'titile': elem['title'], 'content': elem['content']} for elem in searched_content], ensure_ascii=False)}"
            )

        cache.restore_system_node(state["workflow_id"], PLANNER, state["user_id"])

        # Log LLM call start
        llm_start = time.time()
        model_type = "reasoning" if state.get("deep_thinking_mode") else AGENT_LLM_MAP["planner"]
        logger.info("[PERF] Starting LLM call (model: %s)...", model_type)

        # Use async streaming with real-time display
        response = llm.astream(messages)
        chunk_count = 0
        async for chunk in response:
            if chunk.content:
                content += chunk.content  # type: ignore
                # Real-time streaming output to user
                print(chunk.content, end="", flush=True)
                chunk_count += 1

        # Add newline after streaming completes
        if chunk_count > 0:
            print()  # Newline after streaming

        llm_time = time.time() - llm_start
        logger.info("[PERF] LLM call completed: %.2fs", llm_time)

        content = clean_response_tags(content)
        retry_messages = messages
        retry_llm = llm
    elif state["workflow_mode"] == "production":
        content = json.dumps(
            cache.get_planning_steps(state["workflow_id"]), indent=4, ensure_ascii=False
        )

    elif state["workflow_mode"] == "polish" and state.get("polish_target") == "planner":
        polish_start = time.time()
        state["historical_plan"] = cache.get_planning_steps(state["workflow_id"])
        state["adjustment_instruction"] = state.get("polish_instruction")

        messages = _sanitize_messages(apply_prompt_template("planner_polishment", state))
        llm = get_llm_by_type(AGENT_LLM_MAP["planner"])
        if state.get("deep_thinking_mode"):
            llm = get_llm_by_type("reasoning")

        prep_time = time.time()
        logger.info("[PERF] Polish prompt preparation: %.2fs", prep_time - polish_start)

        if state.get("search_before_planning"):
            search_start = time.time()
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
            search_time = time.time() - search_start
            logger.info("[PERF] Polish web search: %.2fs", search_time)

            messages = deepcopy(messages)
            messages[-1]["content"] += (
                f"\n\n# Relative Search Results\n\n{json.dumps([{'titile': elem['title'], 'content': elem['content']} for elem in searched_content], ensure_ascii=False)}"
            )

        llm_start = time.time()
        model_type = "reasoning" if state.get("deep_thinking_mode") else AGENT_LLM_MAP["planner"]
        logger.info("[PERF] Polish starting LLM call (model: %s)...", model_type)

        # Use async streaming with real-time display for polish mode
        response = llm.astream(messages)
        polish_content = ""
        chunk_count = 0
        async for chunk in response:
            if chunk.content:
                polish_content += chunk.content  # type: ignore
                # Real-time streaming output to user
                print(chunk.content, end="", flush=True)
                chunk_count += 1

        # Add newline after streaming completes
        if chunk_count > 0:
            print()  # Newline after streaming

        llm_time = time.time() - llm_start
        logger.info("[PERF] Polish LLM call completed: %.2fs", llm_time)

        content = clean_response_tags(polish_content)

    raw_content = content
    message_content = content

    if state["workflow_mode"] in ["launch", "polish"]:
        parse_start = time.time()
        steps = _extract_plan_steps(raw_content)
        parse_time = time.time() - parse_start
        logger.info("[PERF] JSON parsing: %.2fs", parse_time)

        if steps is None and state["workflow_mode"] == "launch" and retry_messages and retry_llm:
            try:
                retry_start = time.time()
                logger.warning("[PERF] JSON parsing failed, retrying...")

                retry_note = (
                    "仅输出JSON格式的计划，不要解释或补充文字。"
                    "必须使用 {\"steps\": [...]} 结构。"
                )
                retry_payload = deepcopy(retry_messages)
                retry_payload.append({"role": "user", "content": retry_note})
                retry_response = await retry_llm.ainvoke(retry_payload)
                retry_content = clean_response_tags(getattr(retry_response, "content", ""))

                retry_time = time.time() - retry_start
                logger.info("[PERF] Retry LLM call: %.2fs", retry_time)

                if retry_content:
                    steps = _extract_plan_steps(retry_content)
                    if steps is not None:
                        raw_content = retry_content
                        logger.info("[PERF] Retry succeeded")
                    else:
                        logger.warning("[PERF] Retry failed: still cannot parse JSON")
            except Exception as exc:
                logger.warning("[PERF] Retry exception: %s", exc)
        if steps is not None:
            cache.restore_planning_steps(state["workflow_id"], steps, state["user_id"])
            message_content = json.dumps({"steps": steps}, indent=2, ensure_ascii=False)
            if state.get("stop_after_planner") and state["workflow_mode"] == "launch":
                goto = "__end__"
        else:
            logger.warning("Planner response is not a valid JSON \n")
            goto = "__end__"
        cache.restore_system_node(state["workflow_id"], goto, state["user_id"])

    total_time = time.time() - start_time
    logger.info("=" * 60)
    logger.info("[PERF] PLANNER TOTAL TIME: %.2fs (mode: %s)", total_time, state["workflow_mode"])
    logger.info("=" * 60)

    return Command(
        update={
            "messages": [{"content": message_content, "tool": "planner", "role": "assistant"}],
            "agent_name": "planner",
            "full_plan": raw_content,
            "planning_steps": steps if steps is not None else [],
        },
        goto=goto,
    )


async def coordinator_node(state: State) -> Command[Literal["planner", "__end__"]]:
    """Coordinator node."""
    logger.info("Coordinator talking. \n")

    goto = "__end__"
    content = ""

    if state.get("workflow_mode") == "production":
        goto = "publisher"
        content = "handover_to_publisher"
        return Command(
            update={
                "messages": [
                    {"content": content, "tool": "coordinator", "role": "assistant"}
                ],
                "agent_name": "coordinator",
            },
            goto=goto,
        )

    messages = _sanitize_messages(apply_prompt_template("coordinator", state))
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
