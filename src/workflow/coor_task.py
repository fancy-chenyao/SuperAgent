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


def _stringify_stream_chunk(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


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


async def _validate_plan_data_flow(steps: list, user_id: str) -> tuple[bool, list[str]]:
    """
    Validate that all data dependencies in the plan are satisfied.

    Returns:
        (is_valid, error_messages): True if valid, False otherwise with error details
    """
    if not steps:
        return True, []

    errors = []

    # Build agent metadata cache
    agent_metadata = {}
    agents = await agent_manager.agent_registry.list()
    for agent in agents:
        if agent.user_id == "share" or agent.user_id == user_id:
            agent_metadata[agent.agent_name] = {
                "requires": getattr(agent, "requires", []),
                "produces": getattr(agent, "produces", []),
            }

    # Track what data is available at each step
    available_outputs = set()

    for step_idx, step in enumerate(steps):
        agent_name = step.get("agent_name")
        if not agent_name:
            errors.append(f"Step {step_idx + 1}: Missing agent_name")
            continue

        metadata = agent_metadata.get(agent_name)
        if not metadata:
            # Agent not found in registry, skip validation
            logger.warning(f"Step {step_idx + 1}: Agent '{agent_name}' not found in registry")
            continue

        required_params = metadata["requires"]
        produced_outputs = metadata["produces"]

        # If agent has no requirements, it's autonomous
        if not required_params:
            # Add this agent's outputs to available data
            for output in produced_outputs:
                available_outputs.add(output)
            continue

        # Check if all required parameters are mapped
        inputs = step.get("inputs", [])
        mapped_params = set()

        for input_mapping in inputs:
            param_name = input_mapping.get("parameter_name")
            source_step = input_mapping.get("source_step")
            source_output = input_mapping.get("source_output")

            if not param_name or not source_step or not source_output:
                errors.append(
                    f"Step {step_idx + 1} ({agent_name}): Incomplete input mapping - "
                    f"parameter_name={param_name}, source_step={source_step}, source_output={source_output}"
                )
                continue

            mapped_params.add(param_name)

            # Verify source_step exists in previous steps
            source_found = False
            for prev_idx in range(step_idx):
                prev_step = steps[prev_idx]
                if prev_step.get("agent_name") == source_step:
                    source_found = True
                    # Verify source_output is in the source agent's produces
                    source_metadata = agent_metadata.get(source_step)
                    if source_metadata and source_output not in source_metadata["produces"]:
                        errors.append(
                            f"Step {step_idx + 1} ({agent_name}): Input mapping references "
                            f"'{source_output}' from '{source_step}', but '{source_step}' "
                            f"does not produce '{source_output}'. Available outputs: {source_metadata['produces']}"
                        )
                    break

            if not source_found:
                errors.append(
                    f"Step {step_idx + 1} ({agent_name}): Input mapping references "
                    f"source_step '{source_step}' which does not exist in previous steps"
                )

        # Check if all required parameters are mapped
        unmapped_params = set(required_params) - mapped_params
        if unmapped_params:
            errors.append(
                f"Step {step_idx + 1} ({agent_name}): Missing input mappings for required parameters: "
                f"{list(unmapped_params)}. Agent requires: {required_params}"
            )

        # Add this agent's outputs to available data
        for output in produced_outputs:
            available_outputs.add(output)

    is_valid = len(errors) == 0
    return is_valid, errors


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

    # Remote agents receive full message history and extract parameters themselves
    # using their own LLM. No local parameter extraction needed.
    messages_to_send = state["messages"]

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
    runtime_event_handler = state.get("runtime_event_handler")

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
            chunk_text = _stringify_stream_chunk(getattr(chunk, "content", ""))
            if chunk_text:
                content += chunk_text
                # Real-time streaming output to user
                print(chunk_text, end="", flush=True)
                if callable(runtime_event_handler):
                    await runtime_event_handler(
                        {
                            "event": "planner_delta",
                            "agent_name": "planner",
                            "data": {
                                "delta": {"content": chunk_text},
                                "full_content": content,
                                "is_final": False,
                            },
                        }
                    )
                chunk_count += 1

        # Add newline after streaming completes
        if chunk_count > 0:
            print()  # Newline after streaming
        if callable(runtime_event_handler):
            await runtime_event_handler(
                {
                    "event": "planner_delta",
                    "agent_name": "planner",
                    "data": {
                        "delta": {"content": ""},
                        "full_content": content,
                        "is_final": True,
                    },
                }
            )

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
            chunk_text = _stringify_stream_chunk(getattr(chunk, "content", ""))
            if chunk_text:
                polish_content += chunk_text
                # Real-time streaming output to user
                print(chunk_text, end="", flush=True)
                if callable(runtime_event_handler):
                    await runtime_event_handler(
                        {
                            "event": "planner_delta",
                            "agent_name": "planner",
                            "data": {
                                "delta": {"content": chunk_text},
                                "full_content": polish_content,
                                "is_final": False,
                            },
                        }
                    )
                chunk_count += 1

        # Add newline after streaming completes
        if chunk_count > 0:
            print()  # Newline after streaming
        if callable(runtime_event_handler):
            await runtime_event_handler(
                {
                    "event": "planner_delta",
                    "agent_name": "planner",
                    "data": {
                        "delta": {"content": ""},
                        "full_content": polish_content,
                        "is_final": True,
                    },
                }
            )

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

        # Validate plan data flow
        if steps is not None and state["workflow_mode"] == "launch":
            validation_start = time.time()
            is_valid, validation_errors = await _validate_plan_data_flow(
                steps, state.get("user_id", "")
            )
            validation_time = time.time() - validation_start
            logger.info("[PERF] Plan validation: %.2fs", validation_time)

            if not is_valid:
                logger.warning("Plan validation failed with errors:")
                for error in validation_errors:
                    logger.warning(f"  - {error}")

                # Try to fix the plan by asking LLM to correct it
                if retry_messages and retry_llm:
                    try:
                        fix_start = time.time()
                        logger.info("[PERF] Attempting to fix plan validation errors...")

                        error_summary = "\n".join(f"- {err}" for err in validation_errors)
                        fix_note = (
                            f"你生成的计划存在数据流错误，请修正：\n\n{error_summary}\n\n"
                            "修正要求：\n"
                            "1. 如果某个Agent需要的参数（在'Requires'字段中）没有来源，你必须在它之前添加一个步骤来获取这些数据\n"
                            "2. 每个有'Requires'字段的Agent都必须在inputs中明确映射所有必需参数\n"
                            "3. 每个InputMapping的source_step必须是之前步骤中存在的agent_name\n"
                            "4. 每个InputMapping的source_output必须在source_step的'Produces'字段中\n"
                            "5. 如果用户只提供了姓名但Agent需要employee_id，你必须先添加RemoteHRAssistantAgent来查询员工信息\n\n"
                            "请输出修正后的完整计划，仅输出JSON格式，不要解释。"
                        )

                        fix_payload = deepcopy(retry_messages)
                        fix_payload.append({"role": "assistant", "content": raw_content})
                        fix_payload.append({"role": "user", "content": fix_note})

                        fix_response = await retry_llm.ainvoke(fix_payload)
                        fix_content = clean_response_tags(getattr(fix_response, "content", ""))

                        fix_time = time.time() - fix_start
                        logger.info("[PERF] Plan fix LLM call: %.2fs", fix_time)

                        if fix_content:
                            fixed_steps = _extract_plan_steps(fix_content)
                            if fixed_steps is not None:
                                # Validate the fixed plan
                                is_fixed_valid, fixed_errors = await _validate_plan_data_flow(
                                    fixed_steps, state.get("user_id", "")
                                )
                                if is_fixed_valid:
                                    steps = fixed_steps
                                    raw_content = fix_content
                                    logger.info("[PERF] Plan fix succeeded")
                                else:
                                    logger.warning(
                                        f"[PERF] Plan fix failed: still has {len(fixed_errors)} errors"
                                    )
                                    for error in fixed_errors:
                                        logger.warning(f"  - {error}")
                            else:
                                logger.warning("[PERF] Plan fix failed: cannot parse JSON")
                    except Exception as exc:
                        logger.warning(f"[PERF] Plan fix exception: {exc}")

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
