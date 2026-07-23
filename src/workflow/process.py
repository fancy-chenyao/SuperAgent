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
from src.service.env import USE_BROWSER, AUTO_RECOVERY_ENABLED, DISABLE_DEFAULT_AGENTS, S_ABAC_ENABLED
from src.workflow.cache import workflow_cache as cache
from src.workflow.graph import CompiledWorkflow
from src.interface.agent import WorkMode
from src.manager.registry import ToolRegistry
from src.robust.checkpoint import CheckpointManager
from src.robust.task_logger import TaskLogger
from config.s_abac_demo_users import get_user_available_agents
from src.llm.llm import get_llm_by_type
from src.manager.resource import get_resource_registry

# Hook system imports
from src.robust.hooks import (
    HookEngine,
    HookContext,
    HookPoint,
    initialize_hook_system,
)
from src.security.enforcement import PermissionDeniedError
from src.security.scenario_analyzer import analyze_task_context
from src.orchestrator import make_routing_decision
from src.skills.workflow_skill import get_workflow_skill_manager

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

console = Console()


DEFAULT_PLANNER_AGENTS = ["researcher", "coder", "reporter", "browser"]


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
        if "steps" in raw and isinstance(raw.get("steps"), list):
            return raw.get("steps")
        if "planning_steps" in raw and isinstance(raw.get("planning_steps"), list):
            return raw.get("planning_steps")
        return []
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


def _agent_contract_fingerprints(agent_cards: Any) -> dict[str, str]:
    """Hash the current dispatch contract without persisting runtime state."""

    fingerprints: dict[str, str] = {}
    for card in agent_cards if isinstance(agent_cards, list) else []:
        if not isinstance(card, dict):
            continue
        agent_id = str(card.get("agent_id") or card.get("name") or "").strip()
        if not agent_id:
            continue
        contract = {
            "capabilities": card.get("capabilities") or [],
            "intents": card.get("intents") or [],
            "supported_actions": card.get("supported_actions") or [],
            "accepted_data_scopes": card.get("accepted_data_scopes") or [],
            "risk_ceiling": card.get("risk_ceiling") or "LOW",
            "input_schema": card.get("input_schema") or {},
            "output_schema": card.get("output_schema") or {},
            "version": card.get("version") or "1.0.0",
        }
        fingerprints[agent_id] = hashlib.sha256(
            json.dumps(
                contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return fingerprints


def _agent_capability_bindings(agent_cards: Any) -> dict[str, list[str]]:
    bindings: dict[str, list[str]] = {}
    for card in agent_cards if isinstance(agent_cards, list) else []:
        if not isinstance(card, dict):
            continue
        agent_id = str(card.get("agent_id") or card.get("name") or "").strip()
        if agent_id:
            bindings[agent_id] = [
                str(item) for item in card.get("capabilities") or [] if str(item)
            ]
    return bindings


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
        raise RuntimeError("no planning steps found for execution")

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
        raise RuntimeError(f"missing agents for execution: {', '.join(missing)}")

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
    member_desc = ""
    members = []

    available = get_user_available_agents(user_id)
    has_user_profile = bool(available)

    agents = await agent_manager.agent_registry.list()
    for agent in agents:
        should_include = (
            agent.user_id == "share"
            or agent.user_id == user_id
            or agent.agent_name in coor_agents
        )
        if has_user_profile and available != ["*"]:
            if agent.agent_name not in available:
                should_include = False
                if agent.agent_name in coor_agents:
                    logger.warning(
                        "S-ABAC: ignored explicitly selected unauthorized agent '%s' for user '%s'",
                        agent.agent_name,
                        user_id,
                    )

        if should_include and agent.agent_name not in members:
            members.append(agent.agent_name)
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

    if has_user_profile and available != ["*"]:
        for agent_name in available:
            if agent_name in DEFAULT_PLANNER_AGENTS and agent_name not in members:
                members.append(agent_name)

    if not members and has_user_profile:
        logger.warning(
            "S-ABAC: No agents available for user '%s' (available=%s, DISABLE_DEFAULT_AGENTS=%s). "
            "Planner will have an empty team.",
            user_id, available, DISABLE_DEFAULT_AGENTS,
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
    original_user_query: str | None = None,
    memory_session_id: str | None = None,
    memory_context: dict[str, Any] | None = None,
    skill_reuse_enabled: bool | None = None,
    request_input_messages: list | None = None,
):
    """Run the agent workflow with the given user input.

    Args:
        user_input_messages: The user request messages
        debug: If True, enables debug level logging

    Returns:
        The final state after the workflow completes
    """
    identity_messages = request_input_messages or user_input_messages
    if not workflow_id:
        if not polish_id:
            if workmode == "launch":
                msg = f"{user_id}_{identity_messages}_{deep_thinking_mode}_{search_before_planning}_{coor_agents}"
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
        user_input_messages=identity_messages.copy(),
        deep_thinking_mode=deep_thinking_mode,
        search_before_planning=search_before_planning,
        coor_agents=coor_agents,
    )

    if instruction_history is not None:
        cache.set_instruction_history(workflow_id, instruction_history, user_id=user_id)
    elif instruction:
        cache.append_instruction(workflow_id, instruction, user_id=user_id)

    if workmode == "production":
        try:
            await _prepare_execution_graph(workflow_id, user_id, resume_step=resume_step)
        except RuntimeError as exc:
            logger.warning("S-ABAC workflow preparation blocked execution: %s", exc)
            yield {
                "event": "workflow_error",
                "data": {
                    "workflow_id": workflow_id,
                    "task_id": task_id or CheckpointManager.generate_task_id(workflow_id),
                    "error": str(exc),
                    "reason": "No executable planning steps after permission filtering",
                },
            }
            return

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
    registered_agents = await agent_manager.agent_registry.list()
    instruction_history_for_route = cache.get_instruction_history(workflow_id) or []
    routing_query = "\n".join(
        str(item) for item in instruction_history_for_route if str(item).strip()
    ) or original_user_query or user_input_messages[-1]["content"]
    task_profile_model, agent_cards, routing_decision_model = await make_routing_decision(
        user_query=routing_query,
        task_id=task_id,
        workflow_id=workflow_id,
        agents=registered_agents,
        authorized_agent_ids=set(team_members),
        metadata={
            "workflow_mode": str(workmode),
            "s_abac_enabled": S_ABAC_ENABLED,
        },
    )
    routing_decision = routing_decision_model.model_dump()
    routing_decision_for_prompt = dict(routing_decision)
    routing_decision_for_prompt.pop("excluded_agents", None)
    task_profile = task_profile_model.to_legacy_scenario()
    routed_member_ids = [
        item.agent_id for item in routing_decision_model.candidate_agents
    ]
    if routing_decision_model.decision == "DISPATCH":
        team_members = routed_member_ids
        routed_cards = {
            card.agent_id: card for card in agent_cards if card.agent_id in set(routed_member_ids)
        }
        team_members_description = "\n".join(
            (
                f"- **`{agent_id}`**: {routed_cards[agent_id].description}\n"
                f"  - Department: {routed_cards[agent_id].department}\n"
                f"  - Capabilities: {', '.join(routed_cards[agent_id].capabilities)}\n"
                f"  - Intents: {', '.join(routed_cards[agent_id].intents)}\n"
                f"  - Actions: {', '.join(routed_cards[agent_id].supported_actions)}"
            )
            for agent_id in team_members
            if agent_id in routed_cards
        )
    elif workmode != "production":
        team_members = []
        team_members_description = ""
    tools_description = await _build_tools_description()
    resource_catalog = await _build_resource_catalog()

    global coordinator_cache
    coordinator_cache = []
    global is_handoff_case
    is_handoff_case = False

    # 判断执行阶段（调用TaskLogger的静态方法）
    instruction_history_list = cache.get_instruction_history(workflow_id) or []
    execution_phase = TaskLogger.determine_execution_phase(workmode, instruction_history_list)
    logger.info(f"Execution phase determined: {execution_phase}")

    async for event_data in _process_workflow(
        graph,
        {
            "user_id": user_id,
            "TEAM_MEMBERS": team_members,
            "TEAM_MEMBERS_DESCRIPTION": team_members_description,
            "TOOLS": tools_description,
            "RESOURCE_CATALOG": resource_catalog,
            "USER_QUERY": original_user_query or user_input_messages[-1]["content"],
            "execution_user_query": user_input_messages[-1]["content"],
            "original_user_query": original_user_query or user_input_messages[-1]["content"],
            "messages": user_input_messages,
            "deep_thinking_mode": deep_thinking_mode,
            "search_before_planning": search_before_planning,
            "workflow_id": workflow_id,
            "workflow_mode": workmode,
            "polish_instruction": polish_instruction,
            "initialized": False,
            "stop_after_planner": stop_after_planner,
            "instruction_history": cache.get_instruction_history(workflow_id),
            "task_profile": task_profile,
            "task_profile_reason": task_profile.get("reason", ""),
            "scenario_tags": task_profile.get("scenario_tags", []),
            "expected_capabilities": task_profile.get("expected_capabilities", []),
            "task_type": task_profile.get("task_type", "GENERAL"),
            "business_goal": task_profile.get("business_goal", routing_query),
            "data_scope": task_profile.get("data_scope", "general"),
            "operation_mode": task_profile.get("operation_mode", "read"),
            "risk_profile": task_profile.get("risk_profile", "LOW"),
            "scenario_fit_cache": {},
            "TASK_PROFILE_TEXT": json.dumps(task_profile, ensure_ascii=False, indent=2),
            "SCENARIO_TAGS_TEXT": ", ".join(task_profile.get("scenario_tags", [])),
            "EXPECTED_CAPABILITIES_TEXT": ", ".join(task_profile.get("expected_capabilities", [])),
            "routing_decision": routing_decision,
            "ROUTING_DECISION_TEXT": json.dumps(
                routing_decision_for_prompt,
                ensure_ascii=False,
                indent=2,
            ),
            "agent_cards": [card.model_dump() for card in agent_cards],
            "memory_session_id": memory_session_id or "",
            "memory_context": dict(memory_context or {}),
            "skill_reuse_enabled": skill_reuse_enabled is not False,
            "reused_skill_id": "",
            "reused_skill_owner_id": "",
            "workflow_skill_match": {},
            "workflow_execution_failed": False,
        },
        resume_step=resume_step,
        task_id=task_id,
        execution_phase=execution_phase,  # 新增：传递执行阶段
    ):
        yield event_data


async def _process_workflow(
    workflow: CompiledWorkflow, 
    initial_state: dict[str, Any], 
    resume_step: int = None, 
    task_id: str = None,
    execution_phase: str = "initial_planning"  # 新增：执行阶段参数
) -> AsyncGenerator[dict[str, Any], None]:
    """处理自定义工作流的事件流
    
    Args:
        resume_step: The step to START executing (not the checkpoint step).
                     So resume_step=5 means: load checkpoint from step 4, then execute step 5.
                     Must be >= 1.
        execution_phase: 执行阶段 ("initial_planning" | "re_planning" | "execution")
    """
    current_node = None

    runtime_context = {
        key: initial_state.get(key)
        for key in (
            "TEAM_MEMBERS",
            "TEAM_MEMBERS_DESCRIPTION",
            "TOOLS",
            "RESOURCE_CATALOG",
        )
        if initial_state.get(key) is not None
    }

    workflow_id = initial_state["workflow_id"]
    checkpoint_manager = CheckpointManager()
    step_count = 0

    def _restore_scenario_state_from_source(target_state: dict[str, Any], source_state: dict[str, Any] | None) -> None:
        if not isinstance(source_state, dict):
            return
        for key in (
            "original_user_query",
            "execution_user_query",
            "task_profile",
            "task_profile_reason",
            "scenario_tags",
            "expected_capabilities",
            "task_type",
            "business_goal",
            "data_scope",
            "operation_mode",
            "risk_profile",
            "scenario_fit_cache",
            "TASK_PROFILE_TEXT",
            "SCENARIO_TAGS_TEXT",
            "EXPECTED_CAPABILITIES_TEXT",
            "routing_decision",
            "ROUTING_DECISION_TEXT",
            "agent_cards",
            "skill_reuse_enabled",
            "reused_skill_id",
            "reused_skill_owner_id",
            "workflow_skill_match",
            "workflow_execution_failed",
        ):
            if not target_state.get(key) and source_state.get(key) is not None:
                target_state[key] = source_state.get(key)

    # Initialize TaskLogger for this execution
    user_query = initial_state.get("original_user_query") or initial_state.get("USER_QUERY", "")
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
            task_logger.set_execution_phase(execution_phase)  # 设置执行阶段
    else:
        task_logger = TaskLogger(task_id=task_id, workflow_id=workflow_id, user_query=user_query)
        task_logger.set_execution_phase(execution_phase)  # 设置执行阶段

    def _record_reused_skill_outcome(
        source_state: dict[str, Any] | None,
        success: bool,
    ):
        source = source_state if isinstance(source_state, dict) else initial_state
        skill_id = str(source.get("reused_skill_id") or "")
        owner_id = str(source.get("reused_skill_owner_id") or source.get("user_id") or "")
        if not skill_id or not owner_id:
            return None
        try:
            manager = get_workflow_skill_manager()
            return manager.store.record_outcome(
                owner_id,
                skill_id,
                success=success,
                failure_threshold=manager.settings.failure_disable_threshold,
            )
        except Exception as exc:
            logger.warning("Could not update reused workflow skill health: %s", exc)
            return None

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

    if initial_state.get("routing_decision"):
        yield {
            "event": "routing_decision",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "task_profile": initial_state.get("task_profile", {}),
                "routing_decision": initial_state.get("routing_decision", {}),
            },
        }

    try:
        current_node = workflow.start_node
        state = State(**initial_state)

        if state.get("workflow_mode") == "production" and workflow_id in cache.cache:
            workflow_snapshot = cache.cache.get(workflow_id) or {}
            _restore_scenario_state_from_source(state, workflow_snapshot)

        if state.get("workflow_mode") == "production" and task_id:
            try:
                checkpoint_zero = checkpoint_manager.load_checkpoint(
                    workflow_id=workflow_id,
                    task_id=task_id,
                    step=0,
                )
                if checkpoint_zero and isinstance(checkpoint_zero.state, dict):
                    _restore_scenario_state_from_source(state, checkpoint_zero.state)
            except Exception:
                pass

        if not state.get("task_profile"):
            original_user_query = state.get("original_user_query") or state.get("USER_QUERY", "")
            if original_user_query:
                state["USER_QUERY"] = original_user_query
            task_profile = await analyze_task_context(
                original_user_query,
                {
                    "workflow_mode": state.get("workflow_mode"),
                    "risk_profile": state.get("risk_profile", "LOW"),
                },
            )
            state["task_profile"] = task_profile
            state["task_profile_reason"] = task_profile.get("reason", "")
            state["scenario_tags"] = task_profile.get("scenario_tags", [])
            state["expected_capabilities"] = task_profile.get("expected_capabilities", [])
            state["task_type"] = task_profile.get("task_type", "GENERAL")
            state["business_goal"] = task_profile.get("business_goal", original_user_query)
            state["data_scope"] = task_profile.get("data_scope", "targeted")
            state["operation_mode"] = task_profile.get("operation_mode", "read")
            state["risk_profile"] = task_profile.get("risk_profile", "LOW")
            state["scenario_fit_cache"] = {}
            state["TASK_PROFILE_TEXT"] = json.dumps(task_profile, ensure_ascii=False, indent=2)
            state["SCENARIO_TAGS_TEXT"] = ", ".join(task_profile.get("scenario_tags", []))
            state["EXPECTED_CAPABILITIES_TEXT"] = ", ".join(task_profile.get("expected_capabilities", []))

        if state.get("workflow_mode") == "launch" and not should_resume:
            workflow_snapshot = cache.cache.get(workflow_id)
            if isinstance(workflow_snapshot, dict):
                workflow_snapshot["task_profile"] = state.get("task_profile") or {}
                workflow_snapshot["workflow_skill_match"] = {}
                workflow_snapshot["reused_skill_id"] = ""
                workflow_snapshot["reused_skill_owner_id"] = ""

        if state.get("workflow_mode") == "production":
            if not should_resume or not getattr(task_logger, "planning_steps", []):
                task_logger.set_workflow_snapshot(
                    _normalize_planning_steps(cache.get_planning_steps(workflow_id)),
                    state.get("task_profile") or {},
                )
                if hasattr(task_logger, "set_agent_contract_fingerprints"):
                    task_logger.set_agent_contract_fingerprints(
                        _agent_contract_fingerprints(state.get("agent_cards"))
                    )
                if hasattr(task_logger, "set_agent_capability_bindings"):
                    task_logger.set_agent_capability_bindings(
                        _agent_capability_bindings(state.get("agent_cards"))
                    )

        if (
            state.get("workflow_mode") == "launch"
            and state.get("skill_reuse_enabled", True)
            and not should_resume
        ):
            try:
                skill_manager = get_workflow_skill_manager()
                if not skill_manager.settings.enabled or not skill_manager.settings.reuse_enabled:
                    skill_manager = None
                skill_match = (
                    skill_manager.match(
                        user_id=state.get("user_id", ""),
                        query=state.get("USER_QUERY", ""),
                        task_profile=state.get("task_profile") or {},
                        available_agents=state.get("TEAM_MEMBERS") or [],
                        agent_contracts=_agent_contract_fingerprints(
                            state.get("agent_cards")
                        ),
                    )
                    if (
                        skill_manager is not None
                        and (
                            not state.get("routing_decision")
                            or (state.get("routing_decision") or {}).get("decision")
                            == "DISPATCH"
                        )
                    )
                    else None
                )
                if skill_match is not None:
                    state["reused_skill_id"] = skill_match.skill.skill_id
                    state["reused_skill_owner_id"] = skill_match.skill.user_id
                    state["workflow_skill_match"] = {
                        "skill_id": skill_match.skill.skill_id,
                        "owner_user_id": skill_match.skill.user_id,
                        "version": skill_match.skill.version,
                        "score": skill_match.score,
                        "reason": skill_match.reason,
                    }
                    state["planning_steps"] = skill_match.bound_planning_steps
                    cache.restore_planning_steps(
                        workflow_id,
                        skill_match.bound_planning_steps,
                        state["user_id"],
                    )
                    if isinstance(cache.cache.get(workflow_id), dict):
                        cache.cache[workflow_id]["workflow_skill_match"] = state["workflow_skill_match"]
                        cache.cache[workflow_id]["reused_skill_id"] = state["reused_skill_id"]
                        cache.cache[workflow_id]["reused_skill_owner_id"] = state["reused_skill_owner_id"]
                    yield {
                        "event": "skill_matched",
                        "data": {
                            **state["workflow_skill_match"],
                            "schema_version": skill_match.skill.schema_version,
                            "applicability_checks": skill_match.applicability_checks,
                            "quality": skill_match.skill.quality.model_dump(mode="json"),
                            "planning_steps": skill_match.bound_planning_steps,
                        },
                    }
                elif skill_manager is not None:
                    yield {
                        "event": "skill_fallback",
                        "data": {"reason": "no_valid_skill_match"},
                    }
            except Exception as exc:
                logger.warning("Workflow skill matching failed; using normal planning: %s", exc)
                yield {
                    "event": "skill_fallback",
                    "data": {"reason": "skill_match_error"},
                }

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
                        # Runtime capabilities are authoritative at resume time. A
                        # checkpoint restores execution state, not stale prompt,
                        # tool, MCP, or remote-resource catalogs.
                        state.update(runtime_context)
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
            state["task_id"] = task_id
            state["current_step"] = step_count
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

        execution_failed = bool(state.get("workflow_execution_failed"))
        if execution_failed:
            task_logger.log_error(
                error="One or more Agent executions returned a non-success status",
                node_name="agent_proxy",
                step=step_count,
            )
        else:
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
                workflow_status="failed" if execution_failed else "completed",
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

        if state.get("workflow_mode") == "production":
            if execution_failed:
                failed_skill = _record_reused_skill_outcome(state, success=False)
                if failed_skill is not None:
                    yield {
                        "event": "skill_disabled"
                        if failed_skill.status.value == "disabled"
                        else "skill_execution_failed",
                        "data": {
                            "skill_id": failed_skill.skill_id,
                            "status": failed_skill.status.value,
                            "consecutive_failures": failed_skill.consecutive_failures,
                        },
                    }
            else:
                try:
                    manager = get_workflow_skill_manager()
                    distilled_card = None
                    if manager.settings.enabled and manager.settings.auto_distill_enabled:
                        planning_steps = _normalize_planning_steps(task_logger.planning_steps)
                        if planning_steps:
                            distilled_card = manager.distill(
                                user_id=state.get("user_id", ""),
                                task_id=task_id,
                                user_query=user_query,
                                planning_steps=planning_steps,
                                task_profile=task_logger.task_profile,
                                agent_contracts=getattr(
                                    task_logger,
                                    "agent_contract_fingerprints",
                                    _agent_contract_fingerprints(state.get("agent_cards")),
                                ),
                                agent_capabilities=getattr(
                                    task_logger,
                                    "agent_capability_bindings",
                                    _agent_capability_bindings(state.get("agent_cards")),
                                ),
                                outcome_summary={
                                    "technical_success": True,
                                    "business_success": state.get("business_success"),
                                },
                            )
                            yield {
                                "event": "skill_distilled",
                                "data": {
                                    "skill_id": distilled_card.skill_id,
                                    "status": distilled_card.status.value,
                                    "version": distilled_card.version,
                                    "schema_version": distilled_card.schema_version,
                                    "evidence_count": distilled_card.evidence_count,
                                    "bucket_signature": distilled_card.family_signature,
                                    "quality": distilled_card.quality.model_dump(mode="json"),
                                },
                            }
                    reused_skill_id = str(state.get("reused_skill_id") or "")
                    if reused_skill_id:
                        owner_id = str(state.get("reused_skill_owner_id") or state.get("user_id") or "")
                        if distilled_card is not None and distilled_card.skill_id == reused_skill_id:
                            manager.store.mark_successful_reuse(owner_id, reused_skill_id)
                        else:
                            _record_reused_skill_outcome(state, success=True)
                except Exception as exc:
                    logger.warning("Successful workflow could not be distilled: %s", exc)
                    _record_reused_skill_outcome(state, success=True)

        cache.dump(workflow_id, initial_state["workflow_mode"])

        yield {
            "event": "end_of_workflow",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "status": "failed" if execution_failed else "completed",
                "messages": [{
                    "role": "user",
                    "content": "workflow failed" if execution_failed else "workflow completed",
                }],
            },
        }

    except PermissionDeniedError as e:
        logger.warning("S-ABAC permission denied: %s", str(e))
        payload = dict(e.payload)
        task_logger.log_error(
            error=f"S-ABAC permission denied: {payload.get('policy_result', {}).get('reason', str(e))}",
            node_name=current_node or "security",
            step=step_count,
        )
        failed_skill = _record_reused_skill_outcome(
            state if "state" in locals() else initial_state,
            success=False,
        )
        if failed_skill is not None:
            yield {
                "event": "skill_disabled"
                if failed_skill.status.value == "disabled"
                else "skill_execution_failed",
                "data": {
                    "skill_id": failed_skill.skill_id,
                    "status": failed_skill.status.value,
                    "consecutive_failures": failed_skill.consecutive_failures,
                },
            }
        yield {
            "event": "permission_denied",
            "data": {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "error": str(e),
                "policy_result": payload.get("policy_result", {}),
                "subject": payload.get("subject", {}),
                "object": payload.get("object", {}),
                "action": payload.get("action", {}),
                "scenario": payload.get("scenario", {}),
                "scenario_fit_result": (
                    payload.get("scenario", {})
                    .get("task_scenario", {})
                    .get("scenario_fit_result", {})
                ),
            },
        }

    except Exception as e:
        import traceback

        traceback.print_exc()
        logger.error("Error in Agent workflow: %s", str(e))
        task_logger.log_error(error=str(e), node_name=current_node or "system", step=step_count)
        failed_skill = _record_reused_skill_outcome(
            state if "state" in locals() else initial_state,
            success=False,
        )
        if failed_skill is not None:
            yield {
                "event": "skill_disabled"
                if failed_skill.status.value == "disabled"
                else "skill_execution_failed",
                "data": {
                    "skill_id": failed_skill.skill_id,
                    "status": failed_skill.status.value,
                    "consecutive_failures": failed_skill.consecutive_failures,
                },
            }

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
