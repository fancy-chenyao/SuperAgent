import logging
import hashlib
import asyncio
import json
from typing import Any
from collections import deque
from collections.abc import AsyncGenerator
from src.workflow import build_graph
from src.manager import agent_manager
from rich.console import Console
from src.interface.agent import State
from src.service.env import USE_BROWSER, AUTO_RECOVERY_ENABLED, DISABLE_DEFAULT_AGENTS
from src.workflow.cache import workflow_cache as cache
from src.workflow.graph import CompiledWorkflow
from src.interface.agent import WorkMode
from src.manager.registry import ToolRegistry
from src.robust.checkpoint import CheckpointManager
from src.robust.task_logger import TaskLogger
from src.llm.llm import get_llm_by_type
from src.manager.resource import get_resource_registry

# Hook system imports
from src.robust.hooks import (
    HookEngine,
    HookContext,
    HookPoint,
    initialize_hook_system,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

console = Console()


def enable_debug_logging():
    """Enable debug level logging for more detailed execution information."""
    logging.getLogger("src").setLevel(logging.DEBUG)


logger = logging.getLogger(__name__)


def _normalize_planning_steps(raw: Any) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        steps = raw.get("steps") or raw.get("planning_steps")
        return steps if isinstance(steps, list) else []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            return []
        return _normalize_planning_steps(parsed)
    return []


async def _execute_node_with_runtime_events(
    state: State, node_func, enable_runtime_events: bool
):
    if not enable_runtime_events:
        yield await node_func(state)
        return

    runtime_event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _emit_runtime_event(event: dict[str, Any]) -> None:
        await runtime_event_queue.put(event)

    state["runtime_event_handler"] = _emit_runtime_event
    command_task = asyncio.create_task(node_func(state))

    try:
        while True:
            if command_task.done():
                while not runtime_event_queue.empty():
                    yield await runtime_event_queue.get()
                break
            try:
                event = await asyncio.wait_for(runtime_event_queue.get(), timeout=0.05)
                yield event
            except asyncio.TimeoutError:
                continue

        command = await command_task
        yield command
    finally:
        state.pop("runtime_event_handler", None)
        if not command_task.done():
            command_task.cancel()


async def _prepare_execution_graph(workflow_id: str, user_id: str, resume_step: int = None) -> None:
    """Prepare execution graph and queue for production mode.

    Args:
        workflow_id: Workflow ID
        user_id: User ID
        resume_step: If provided, fast-forward the queue to start from this step.
                     resume_step=5 means the first 4 steps are done, start from step 5.
    """
    workflow = cache.cache.get(workflow_id)
    if not workflow:
        cache._load_workflow(user_id)
        workflow = cache.cache.get(workflow_id)
    if not workflow:
        raise ValueError("workflow not found for execution")

    steps = _normalize_planning_steps(cache.get_planning_steps(workflow_id))
    if not steps:
        raise ValueError("no planning steps found for execution")

    await agent_manager.ensure_initialized()
    nodes = workflow.get("nodes") if isinstance(workflow.get("nodes"), dict) else {}
    graph = workflow.get("graph") if isinstance(workflow.get("graph"), list) else []
    system_graph = [
        node
        for node in graph
        if isinstance(node, dict) and (node.get("config") or {}).get("node_type") == "system_agent"
    ]

    exec_graph = []
    missing = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            missing.append(f"step_{idx + 1}")
            continue
        agent_name = step.get("agent_name")
        if not agent_name:
            missing.append(f"step_{idx + 1}")
            continue
        agent = await agent_manager.agent_registry.get(agent_name)
        if agent is None:
            missing.append(agent_name)
            continue

        tools = []
        for tool in agent.selected_tools:
            tools.append(
                {
                    "component_type": "function",
                    "label": tool.name,
                    "name": tool.name,
                    "config": {
                        "name": tool.name,
                        "description": tool.description,
                    },
                }
            )

        nodes[agent_name] = {
            "component_type": "agent",
            "label": agent.agent_name,
            "name": agent.agent_name,
            "config": {
                "type": "execution_agent",
                "name": agent.agent_name,
                "description": agent.description,
                "tools": tools,
                "prompt": agent.prompt,
                "llm_type": agent.llm_type,
            },
        }

        exec_graph.append(
            {
                "component_type": "agent",
                "label": agent.agent_name,
                "name": agent.agent_name,
                "config": {
                    "node_name": agent.agent_name,
                    "node_type": "execution_agent",
                    "next_to": [],
                    "condition": "supervised",
                },
            }
        )

    if missing:
        raise ValueError(f"missing agents for execution: {', '.join(missing)}")

    for i, node in enumerate(exec_graph):
        if i + 1 < len(exec_graph):
            node["config"]["next_to"] = [exec_graph[i + 1]["config"]["node_name"]]
        else:
            node["config"]["next_to"] = []

    workflow["planning_steps"] = steps
    workflow["nodes"] = nodes
    workflow["graph"] = system_graph + exec_graph
    cache.cache[workflow_id] = workflow
    cache.save_workflow(workflow)

    cache.queue[workflow_id] = deque(exec_graph)
    if exec_graph:
        begin_node = {
            "component_type": "agent",
            "label": "begin_node",
            "name": "begin_node",
            "config": {
                "node_name": "begin_node",
                "node_type": "execution_agent",
                "next_to": [exec_graph[0]["config"]["node_name"]],
                "condition": "supervised",
            },
        }
        cache.queue[workflow_id].appendleft(begin_node)

    # Fast-forward queue for resume
    if resume_step is not None and resume_step >= 1:
        # Execution sequence analysis:
        # - Steps 0,1: coordinator, planner (system nodes, no queue update)
        # - Step 2: publisher (system node, no queue update)
        # - Step 3: agent_proxy->agent1 (update_stack pops begin_node)
        # - Step 4: publisher (no update)
        # - Step 5: agent_proxy->agent2 (update_stack pops agent1)
        # - Step 6: publisher (no update)
        # - ...
        #
        # Pattern: agent_proxy runs at odd steps (3, 5, 7, ...), each pops queue[0]
        #          publisher runs at even steps (2, 4, 6, ...), no pop
        #
        # Checkpoint is saved AFTER node execution and update_stack
        # So for resume_step=M (checkpoint step=M-1):
        # - Queue state reflects all update_stack calls from steps < M
        # - We need to replay those pops
        #
        # Count agent_proxy steps that completed before resume_step:
        # - Agent_proxy steps are: 3, 5, 7, ... (odd steps >= 3)
        # - Count odd numbers in range [3, resume_step)
        agent_proxy_steps_completed = sum(1 for s in range(3, resume_step) if s % 2 == 1)
        
        # Log initial queue state
        initial_queue = list(cache.queue[workflow_id])
        logger.info(f"Queue BEFORE fast-forward (resume_step={resume_step}): {[n['name'] for n in initial_queue]}")
        logger.info(f"Agent_proxy steps completed: {agent_proxy_steps_completed}")
        
        # Pop that many elements from queue
        for i in range(agent_proxy_steps_completed):
            if cache.queue[workflow_id]:
                popped = cache.queue[workflow_id].popleft()
                logger.info(f"Fast-forward pop {i+1}: removed '{popped.get('name')}' from queue")
            else:
                logger.warning(f"Queue empty at iteration {i+1}, stopping fast-forward")
                break
        
        final_queue = list(cache.queue[workflow_id])
        logger.info(f"Queue AFTER fast-forward: {[n['name'] for n in final_queue]}")

if USE_BROWSER and not DISABLE_DEFAULT_AGENTS:
    DEFAULT_TEAM_MEMBERS_DESCRIPTION = """
        - **`coder`**: Executes Python or Bash commands, performs mathematical calculations, and outputs a Markdown report. Must be used for all mathematical computations.
        - **`browser`**: Directly interacts with web pages, performing complex operations and interactions. You can also leverage `browser` to perform in-domain search, like Facebook, Instagram, Github, etc.
        - **`reporter`**: Write a professional report based on the result of each step.
        
        """
elif not DISABLE_DEFAULT_AGENTS:
    DEFAULT_TEAM_MEMBERS_DESCRIPTION = """
        - **`researcher`**: Uses search engines and web crawlers to gather information from the internet. Outputs a Markdown report summarizing findings. Researcher can not do math or programming.
        - **`coder`**: Executes Python or Bash commands, performs mathematical calculations, and outputs a Markdown report. Must be used for all mathematical computations.
        - **`reporter`**: Write a professional report based on the result of each step.
        
        """
else:
    DEFAULT_TEAM_MEMBERS_DESCRIPTION = ""

TEAM_MEMBERS_DESCRIPTION_TEMPLATE = """
- **`{agent_name}`**: {agent_description}
  - Requires: {requires}
  - Produces: {produces}
"""
TOOLS_DESCRIPTION_TEMPLATE = """
- **`{tool_name}`**: {tool_description}
"""
# Cache for coordinator messages
coordinator_cache = []
MAX_CACHE_SIZE = 2


async def _build_team_members(
    user_id: str,
    coor_agents: list[str] | None,
) -> tuple[list[str], str]:
    coor_agents = coor_agents or []
    member_desc = DEFAULT_TEAM_MEMBERS_DESCRIPTION
    members = []

    agents = await agent_manager.agent_registry.list()
    for agent in agents:
        should_include = (
            agent.user_id == "share"
            or agent.user_id == user_id
            or agent.agent_name in coor_agents
        )
        if should_include and agent.agent_name not in members:
            members.append(agent.agent_name)

        if agent.user_id != "share" or getattr(agent, "source", None) == "remote":
            # Get requires and produces, format as comma-separated strings
            requires = getattr(agent, "requires", [])
            produces = getattr(agent, "produces", [])
            requires_str = ", ".join(requires) if requires else "None"
            produces_str = ", ".join(produces) if produces else "None"

            member_desc += "\n" + TEAM_MEMBERS_DESCRIPTION_TEMPLATE.format(
                agent_name=agent.agent_name,
                agent_description=agent.description,
                requires=requires_str,
                produces=produces_str,
            )

    return members, member_desc


async def _build_tools_description() -> str:
    registry = await ToolRegistry.get_instance()
    tools = await registry.list_global_tools()
    resource_registry = await get_resource_registry()
    resource_tools = await resource_registry.list(type="tool")
    descriptions = []

    for meta in tools:
        tool_name = getattr(meta.tool, "name", "")
        if not tool_name:
            continue
        tool_desc = meta.description or getattr(meta.tool, "description", "")
        descriptions.append(
            TOOLS_DESCRIPTION_TEMPLATE.format(
                tool_name=tool_name,
                tool_description=tool_desc,
            )
        )

    for spec in resource_tools:
        if spec.server_id == "local":
            continue
        tool_desc = (spec.metadata or {}).get("description", "")
        suffix = f"(remote/{spec.protocol or 'http'} from {spec.server_id})"
        descriptions.append(
            TOOLS_DESCRIPTION_TEMPLATE.format(
                tool_name=spec.name,
                tool_description=f"{tool_desc} {suffix}".strip(),
            )
        )
    return "".join(descriptions)


async def _build_resource_catalog() -> str:
    registry = await get_resource_registry()
    specs = await registry.list()
    if not specs:
        return ""

    lines = []
    for spec in sorted(specs, key=lambda s: (s.type, s.server_id, s.name)):
        desc = (spec.metadata or {}).get("description", "")
        proto = spec.protocol or "local"
        location = "local" if spec.server_id == "local" else f"remote/{spec.server_id}"
        lines.append(
            f"- [{spec.type}] {spec.name} ({location}, protocol={proto}) {desc}".strip()
        )
    return "\n".join(lines)


async def run_agent_workflow(
    user_id: str,
    user_input_messages: list,
    debug: bool = False,
    deep_thinking_mode: bool = False,
    search_before_planning: bool = False,
    coor_agents: list[str] | None = None,
    polish_id: str = None,
    lap: int = 0,
    workmode: WorkMode = "launch",
    workflow_id: str = None,
    polish_instruction: str = None,
    resume_step: int = None,
    task_id: str = None,
    stop_after_planner: bool = False,
    instruction: str | None = None,
    instruction_history: list[str] | None = None,
):
    """Run the agent workflow with the given user input.

    Args:
        user_input_messages: The user request messages
        debug: If True, enables debug level logging

    Returns:
        The final state after the workflow completes
    """
    if not workflow_id:
        if not polish_id:
            if workmode == "launch":
                msg = f"{user_id}_{user_input_messages}_{deep_thinking_mode}_{search_before_planning}_{coor_agents}"
                polish_id = hashlib.md5(msg.encode("utf-8")).hexdigest()
            else:
                polish_id = cache.get_latest_polish_id(user_id)

        workflow_id = f"{user_id}:{polish_id}"

    await agent_manager.ensure_initialized()
    lap = cache.get_lap(workflow_id) if workmode != "launch" else 0

    if workmode != "production":
        lap = lap + 1

    cache.init_cache(
        user_id=user_id,
        mode=workmode,
        workflow_id=workflow_id,
        lap=lap,
        version=1,
        user_input_messages=user_input_messages.copy(),
        deep_thinking_mode=deep_thinking_mode,
        search_before_planning=search_before_planning,
        coor_agents=coor_agents,
    )

    if instruction_history is not None:
        cache.set_instruction_history(workflow_id, instruction_history, user_id=user_id)
    elif instruction:
        cache.append_instruction(workflow_id, instruction, user_id=user_id)

    if workmode == "production":
        await _prepare_execution_graph(workflow_id, user_id, resume_step=resume_step)

    # Generate a unique task_id for this execution instance if not provided
    if not task_id:
        task_id = CheckpointManager.generate_task_id(workflow_id)

    graph = build_graph()
    if not user_input_messages:
        raise ValueError("Input could not be empty")

    if debug:
        enable_debug_logging()

    logger.info(f"Starting workflow with user input: {user_input_messages}")

    team_members, team_members_description = await _build_team_members(
        user_id=user_id,
        coor_agents=coor_agents,
    )
    tools_description = await _build_tools_description()
    resource_catalog = await _build_resource_catalog()

    global coordinator_cache
    coordinator_cache = []
    global is_handoff_case
    is_handoff_case = False

    async for event_data in _process_workflow(
        graph,
        {
            "user_id": user_id,
            "TEAM_MEMBERS": team_members,
            "TEAM_MEMBERS_DESCRIPTION": team_members_description,
            "TOOLS": tools_description,
            "RESOURCE_CATALOG": resource_catalog,
            "USER_QUERY": user_input_messages[-1]["content"],
            "messages": user_input_messages,
            "deep_thinking_mode": deep_thinking_mode,
            "search_before_planning": search_before_planning,
            "workflow_id": workflow_id,
            "workflow_mode": workmode,
            "polish_instruction": polish_instruction,
            "initialized": False,
            "stop_after_planner": stop_after_planner,
            "instruction_history": cache.get_instruction_history(workflow_id),
        },
        resume_step=resume_step,
        task_id=task_id,
    ):
        yield event_data


async def _process_workflow(
    workflow: CompiledWorkflow, initial_state: dict[str, Any], resume_step: int = None, task_id: str = None
) -> AsyncGenerator[dict[str, Any], None]:
    """处理自定义工作流的事件流
    
    Args:
        resume_step: The step to START executing (not the checkpoint step).
                     So resume_step=5 means: load checkpoint from step 4, then execute step 5.
                     Must be >= 1.
    """
    current_node = None

    workflow_id = initial_state["workflow_id"]
    checkpoint_manager = CheckpointManager()
    step_count = 0

    # Initialize TaskLogger for this execution
    user_query = initial_state.get("USER_QUERY", "")
    if not task_id:
        task_id = CheckpointManager.generate_task_id(workflow_id)
    
    # Resume logic: Check if we are in a mode that supports resuming or resume_step is specified
    # resume_step indicates the step to START executing, so we need checkpoint from (resume_step - 1)
    should_resume = resume_step is not None and resume_step >= 1
    
    if should_resume:
        # Load existing TaskLogger and truncate history
        from src.robust.task_logger import TaskLogger as TL
        existing_logger = TL.load(task_id)
        if existing_logger:
            # Truncate history: remove entries from resume_step onwards and workflow_end events
            existing_logger.history = [
                entry for entry in existing_logger.history
                if entry.get("step", 0) < resume_step and entry.get("event") != "workflow_end"
            ]
            existing_logger.status = "running"
            existing_logger.error = None
            existing_logger._step_counter = {"__global__": resume_step - 1}
            task_logger = existing_logger
            user_query = existing_logger.user_query
            logger.info(f"Resumed TaskLogger for task {task_id}, truncated to step {resume_step - 1}")
        else:
            task_logger = TaskLogger(task_id=task_id, workflow_id=workflow_id, user_query=user_query)
    else:
        task_logger = TaskLogger(task_id=task_id, workflow_id=workflow_id, user_query=user_query)

    # Initialize hook system (controlled by AUTO_RECOVERY_ENABLED)
    hook_engine = None
    if AUTO_RECOVERY_ENABLED:
        initialize_hook_system()
        hook_engine = HookEngine()
    
    # Prepare LLM client for handlers
    llm_client = get_llm_by_type("reasoning")

    yield {
        "event": "start_of_workflow",
        "data": {"workflow_id": workflow_id, "task_id": task_id, "input": initial_state["messages"], "resume_step": resume_step},
    }

    try:
        current_node = workflow.start_node
        state = State(**initial_state)

        if should_resume:
            try:
                # Load checkpoint from (resume_step - 1)
                checkpoint_step = resume_step - 1
                checkpoint = checkpoint_manager.load_checkpoint(workflow_id=workflow_id, task_id=task_id, step=checkpoint_step)
                if checkpoint:
                    logger.info(f"Resuming workflow {workflow_id} (task {task_id}) from checkpoint step {checkpoint.step}, will execute step {resume_step}")
                    if checkpoint.next_node:
                        current_node = checkpoint.next_node
                        state = State(**checkpoint.state)
                        step_count = resume_step
                        # Clean up stale checkpoints from previous failed runs
                        # Delete checkpoints with step >= resume_step (they may be from earlier failed attempts)
                        checkpoint_manager.clean_checkpoints_from_step(task_id=task_id, from_step=resume_step)
                    else:
                        logger.warning("Checkpoint missing next_node, starting from scratch")
            except Exception as e:
                logger.warning(f"Could not load checkpoint for resume, starting from scratch: {e}")

        # Only log workflow_start for new executions, not for resume
        if not should_resume:
            task_logger.log_workflow_start(user_query=user_query)

        while current_node != "__end__":
            agent_name = current_node
            logger.info(f"Started node: {agent_name}")

            # Store original node name to avoid being overwritten in message loop
            original_node_name = agent_name

            # For agent_proxy, get the actual sub-agent name from state["next"]
            # Note: state["next"] is set by publisher in the previous iteration
            sub_agent_name = state.get("next") if agent_name == "agent_proxy" else None
            task_logger.log_agent_start(node_name=original_node_name, step=step_count, sub_agent_name=sub_agent_name)

            # === Hook: NODE_START ===
            if hook_engine:
                hook_ctx = HookContext(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    current_node=agent_name,
                    current_step=step_count,
                    state=dict(state),
                    history=task_logger.history,
                    hook_point=HookPoint.NODE_START,
                    user_query=user_query,
                )
                hook_result = await hook_engine.process(hook_ctx)
                if hook_result.modified_state:
                    state = State(**hook_result.modified_state)

            # Display name for frontend: agent_proxy【researcher】 format
            display_name = f"{agent_name}【{sub_agent_name}】" if sub_agent_name else agent_name
            yield {
                "event": "start_of_agent",
                "data": {
                    "agent_name": display_name,
                    "agent_id": f"{workflow_id}_{agent_name}_1",
                    "sub_agent_name": sub_agent_name,
                },
            }
            node_func = workflow.nodes[current_node]
            command = None
            async for runtime_result in _execute_node_with_runtime_events(
                state,
                node_func,
                enable_runtime_events=agent_name == "planner",
            ):
                if hasattr(runtime_result, "goto"):
                    command = runtime_result
                else:
                    yield runtime_result

            if command is None:
                raise RuntimeError(f"Node '{agent_name}' did not return a command")

            if hasattr(command, "update") and command.update:
                for key, value in command.update.items():
                    if key != "messages":
                        state[key] = value

                    if key == "messages" and isinstance(value, list) and value:
                        # State ignores coordinator messages, which not only lacks contextual benefits
                        # but may also cause other unpredictable effects.
                        if agent_name != "coordinator":
                            state["messages"] += value
                        last_message = value[-1]
                        if "content" in last_message:
                            if agent_name == "coordinator":
                                content = last_message["content"]
                                if content.startswith("handover"):
                                    # mark handoff, do not send maesages
                                    global is_handoff_case
                                    is_handoff_case = True
                                    continue
                            if agent_name in ["planner", "coordinator", "agent_proxy"]:
                                content = last_message["content"]
                                if not isinstance(content, str):
                                    try:
                                        content = json.dumps(content, ensure_ascii=False)
                                    except Exception:
                                        content = str(content)
                                # Log agent message to task log
                                task_logger.log_message(node_name=original_node_name, content=content, step=step_count)
                                chunk_size = 10  # send 10 words for each chunk
                                for i in range(0, len(content), chunk_size):
                                    chunk = content[i : i + chunk_size]
                                    # Use sub_agent_name for display if available
                                    msg_display_name = f"{original_node_name}【{state.get('processing_agent_name')}】" if original_node_name == "agent_proxy" and "processing_agent_name" in state else original_node_name

                                    yield {
                                        "event": "messages",
                                        "agent_name": msg_display_name,
                                        "data": {
                                            "message_id": f"{workflow_id}_{msg_display_name}_msg_{i}",
                                            "delta": {"content": chunk},
                                        },
                                    }
                                    await asyncio.sleep(0.01)

            next_node = command.goto

            # For agent_proxy, get the actual sub-agent name from state["processing_agent_name"]
            # Use original_node_name to ensure correct identification
            sub_agent_name = state.get("processing_agent_name") if original_node_name == "agent_proxy" else None
            task_logger.log_agent_end(node_name=original_node_name, next_node=next_node, step=step_count, sub_agent_name=sub_agent_name)

            # Save checkpoint after node execution and state update
            try:
                checkpoint_manager.save_checkpoint(
                    workflow_id=workflow_id,
                    task_id=task_id,
                    step=step_count,
                    node_name=original_node_name,
                    next_node=next_node,
                    state=state
                )
                step_count += 1
            except Exception as e:
                logger.error(f"Failed to save checkpoint at step {step_count}: {e}")

            # === Hook: NODE_END ===
            if hook_engine:
                hook_ctx = HookContext(
                    task_id=task_id,
                    workflow_id=workflow_id,
                    current_node=next_node,
                    current_step=step_count,
                    state=dict(state),
                    history=task_logger.history,
                    hook_point=HookPoint.NODE_END,
                    user_query=user_query,
                    last_message=content if 'content' in dir() else None,
                    last_agent=sub_agent_name,
                )
                hook_result = await hook_engine.process(hook_ctx)
                if hook_result.modified_state:
                    state = State(**hook_result.modified_state)
                # Handle recovery from hook result
                if hook_result.resume_step is not None and hook_result.modified_state:
                    # Recovery triggered, resume workflow
                    logger.info(f"Hook triggered recovery, resuming from step {hook_result.resume_step}")
                    async for event_data in _process_workflow(
                        workflow,
                        hook_result.modified_state,
                        resume_step=hook_result.resume_step,
                        task_id=task_id,
                    ):
                        yield event_data
                    return

            # Use sub_agent_name for display in end_of_agent event
            end_display_name = f"{original_node_name}【{sub_agent_name}】" if sub_agent_name else original_node_name
            yield {
                "event": "end_of_agent",
                "data": {
                    "agent_name": end_display_name,
                    "agent_id": f"{workflow_id}_{original_node_name}_1",
                    "sub_agent_name": sub_agent_name,
                },
            }

            current_node = next_node

        task_logger.log_workflow_end()

        # === Hook: WORKFLOW_END ===
        if hook_engine:
            hook_ctx = HookContext(
                task_id=task_id,
                workflow_id=workflow_id,
                current_node="__end__",
                current_step=step_count,
                state=dict(state),
                history=task_logger.history,
                hook_point=HookPoint.WORKFLOW_END,
                workflow_status="completed",
                user_query=user_query,
            )
            # Inject dependencies for handlers
            hook_ctx.state["__llm_client__"] = llm_client
            hook_ctx.state["__checkpoint_manager__"] = checkpoint_manager
            
            hook_result = await hook_engine.process(hook_ctx)
            
            # Handle recovery from workflow_end hook
            if hook_result.resume_step is not None and hook_result.modified_state:
                logger.info(f"Workflow end hook triggered recovery, resuming from step {hook_result.resume_step}")
                async for event_data in _process_workflow(
                    workflow,
                    hook_result.modified_state,
                    resume_step=hook_result.resume_step,
                    task_id=task_id,
                ):
                    yield event_data
                return

        yield {
            "event": "end_of_workflow",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "messages": [{"role": "user", "content": "workflow completed"}],
            },
        }

        cache.dump(workflow_id, initial_state["workflow_mode"])

    except Exception as e:
        import traceback

        traceback.print_exc()
        logger.error("Error in Agent workflow: %s", str(e))
        task_logger.log_error(error=str(e), node_name=current_node or "system", step=step_count)
        
        # === Hook: ERROR ===
        if hook_engine:
            hook_ctx = HookContext(
                task_id=task_id,
                workflow_id=workflow_id,
                current_node=current_node,
                current_step=step_count,
                state=dict(state) if 'state' in dir() else {},
                history=task_logger.history,
                error=e,
                error_message=str(e),
                hook_point=HookPoint.ERROR,
                workflow_status="failed",
                user_query=user_query,
            )
            # Inject dependencies for handlers
            hook_ctx.state["__llm_client__"] = llm_client
            hook_ctx.state["__checkpoint_manager__"] = checkpoint_manager
            
            hook_result = await hook_engine.process(hook_ctx)
            
            # Handle recovery from error hook
            if hook_result.resume_step is not None and hook_result.modified_state:
                logger.info(f"Error hook triggered recovery, resuming from step {hook_result.resume_step}")
                async for event_data in _process_workflow(
                    workflow,
                    hook_result.modified_state,
                    resume_step=hook_result.resume_step,
                    task_id=task_id,
                ):
                    yield event_data
                return
        
        yield {
            "event": "error",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "error": str(e),
            },
        }
