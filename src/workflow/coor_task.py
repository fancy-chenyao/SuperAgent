import logging
import json
import time
from copy import deepcopy
from typing import Any, Dict, Literal, Optional

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
from src.security.enforcement import enforce_agent_dispatch
from config.global_variables import artifact_capture_enabled

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
    from src.tools.search import get_search_status, is_search_available, tavily_tool
except Exception:  # pragma: no cover - optional dependency in lightweight test env
    class _NoopTavilyTool:  # type: ignore
        def invoke(self, *args, **kwargs):
            return []

        async def ainvoke(self, *args, **kwargs):
            return []

    tavily_tool = _NoopTavilyTool()

    def is_search_available():  # type: ignore
        return False

    def get_search_status():  # type: ignore
        return {"configured": False, "reason": "search dependencies are unavailable"}


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


def _search_before_planning(state: State) -> list[dict]:
    """Run optional planning search without turning an unavailable provider into a task failure."""
    if not is_search_available():
        status = get_search_status()
        logger.warning("Search before planning skipped: %s",
                       status.get("reason"))
        return []

    user_messages = [
        str(message.get("content", ""))
        for message in state.get("messages", [])
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    query = next((item for item in user_messages if item.strip()), "")
    if not query:
        logger.warning(
            "Search before planning skipped: no user query was found")
        return []

    try:
        result = tavily_tool.invoke(
            {"query": query},
            config={"configurable": {"user_id": state.get("user_id")}},
        )
    except Exception as exc:
        logger.warning(
            "Search before planning failed and was skipped: %s", exc)
        return []

    if not isinstance(result, list):
        logger.warning(
            "Search before planning returned an unexpected result type: %s", type(result).__name__)
        return []
    return [item for item in result if isinstance(item, dict)]


def _append_search_context(messages, searched_content: list[dict]):
    if not searched_content or not messages:
        return messages
    normalized = [
        {
            "title": elem.get("title", ""),
            "content": elem.get("content", ""),
            "url": elem.get("url", ""),
        }
        for elem in searched_content
    ]
    enriched = deepcopy(messages)
    enriched[-1]["content"] += (
        "\n\n# Relevant Search Results\n\n"
        + json.dumps(normalized, ensure_ascii=False)
    )
    return enriched


def _ensure_scenario_prompt_defaults(prompt_state: dict) -> dict:
    """Populate scenario-related prompt fields so template rendering is resilient."""
    task_profile = prompt_state.get("task_profile")
    if not isinstance(task_profile, dict):
        task_profile = {}
        prompt_state["task_profile"] = task_profile

    if not prompt_state.get("TASK_PROFILE_TEXT"):
        prompt_state["TASK_PROFILE_TEXT"] = json.dumps(
            task_profile, ensure_ascii=False, indent=2
        )

    scenario_tags = prompt_state.get("scenario_tags")
    if not isinstance(scenario_tags, list):
        scenario_tags = []
        prompt_state["scenario_tags"] = scenario_tags
    if not prompt_state.get("SCENARIO_TAGS_TEXT"):
        prompt_state["SCENARIO_TAGS_TEXT"] = (
            ", ".join(str(tag) for tag in scenario_tags) or "general"
        )

    expected_capabilities = prompt_state.get("expected_capabilities")
    if not isinstance(expected_capabilities, list):
        expected_capabilities = []
        prompt_state["expected_capabilities"] = expected_capabilities
    if not prompt_state.get("EXPECTED_CAPABILITIES_TEXT"):
        prompt_state["EXPECTED_CAPABILITIES_TEXT"] = (
            ", ".join(str(item) for item in expected_capabilities) or "General"
        )

    routing_decision = prompt_state.get("routing_decision")
    if not isinstance(routing_decision, dict):
        routing_decision = {}
        prompt_state["routing_decision"] = routing_decision
    if not prompt_state.get("ROUTING_DECISION_TEXT"):
        prompt_state["ROUTING_DECISION_TEXT"] = json.dumps(
            routing_decision,
            ensure_ascii=False,
            indent=2,
        )

    return prompt_state


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
        candidates.append(text[first_obj: last_obj + 1])

    first_arr = text.find("[")
    last_arr = text.rfind("]")
    if first_arr >= 0 and last_arr > first_arr:
        candidates.append(text[first_arr: last_arr + 1])

    for candidate in candidates:
        parsed = _try_parse(candidate)
        if parsed is None:
            continue
        if isinstance(parsed, dict):
            if "steps" in parsed and isinstance(parsed.get("steps"), list):
                return parsed.get("steps")
            if "planning_steps" in parsed and isinstance(parsed.get("planning_steps"), list):
                return parsed.get("planning_steps")
        if isinstance(parsed, list):
            return parsed
    return None


def _fallback_plan_steps(state: State) -> list[dict] | None:
    task_type = str(state.get("task_type") or "").upper()
    expected_capabilities = {
        str(item).lower() for item in (state.get("expected_capabilities") or []) if item is not None
    }
    user_query = str(state.get("USER_QUERY") or "")
    team_members = set(state.get("TEAM_MEMBERS") or [])
    lowered = user_query.lower()

    is_engineering_task = (
        task_type == "ENGINEERING"
        or "engineering" in expected_capabilities
        or any(token in lowered for token in ("python", "script", "json", "code", "program", "bash", "shell"))
    )

    if is_engineering_task and "coder" in team_members:
        return [
            {
                "agent_name": "coder",
                "title": "编写并运行脚本完成统计",
                "description": (
                    "使用 coder 编写并执行一个 Python 脚本，统计当前目录下所有 json 文件数量。"
                    "inputs: 用户当前请求；outputs: 可执行脚本与统计结果。"
                ),
                "note": "该任务与 Engineering/coding 场景直接匹配，使用 coder 单步完成即可。",
            }
        ]

    return None


_PROFILE_INTENT_AGENT_PREFERENCES = {
    "employee_information_query": ("RemoteHRAssistantAgent",),
    "salary_query": ("RemoteHRAssistantAgent",),
    "leave_record_query": ("RemoteOfficeAssistantAgent",),
    "information_research": ("researcher", "browser", "RemoteUnicornSelectorAgent"),
    "knowledge_lookup": ("RemoteKnowledgeAgent", "researcher"),
    "risk_analysis": ("RemoteBusinessRiskAgent",),
    "document_generation": ("RemoteDocumentGeneratorAgent",),
    "report_generation": ("RemoteReportAgent", "RemoteDocumentGeneratorAgent"),
    "message_or_email_send": ("RemoteEmailDispatchAgent", "RemoteCommunicationAgent"),
    "meeting_arrangement": ("RemoteMeetingManagerAgent",),
    "schedule_management": ("RemoteScheduleAgent", "RemoteHRCalendarAgent"),
    "weather_query": ("RemoteWeatherAgent",),
    "travel_service": ("RemoteOfficeAssistantAgent",),
}


def _normalize_text(value) -> str:
    return str(value or "").strip().lower()


def _plan_step_text(step: dict) -> str:
    return " ".join(
        _normalize_text(step.get(key))
        for key in ("agent_name", "title", "description", "note")
    )


def _infer_step_intents(step: dict) -> set[str]:
    text = _plan_step_text(step)
    agent_name = str(step.get("agent_name") or "")
    intents: set[str] = set()
    compatible = {
        intent
        for intent, agents in _PROFILE_INTENT_AGENT_PREFERENCES.items()
        if agent_name in agents
    }
    # Agent 能力不等于当前步骤意图。只有步骤文本和 Agent 能力同时匹配才计入，
    # 避免把 HR Agent 描述中的“请假记录”误当成已经执行的独立查询。
    if "employee_information_query" in compatible and any(
        token in text for token in ("员工", "人员", "基础信息", "个人信息", "employee")
    ):
        intents.add("employee_information_query")
    if "salary_query" in compatible and any(token in text for token in ("薪资", "工资", "收入")):
        intents.add("salary_query")
    if "leave_record_query" in compatible and any(
        token in text for token in ("请假记录", "休假记录", "请假申请记录", "考勤记录", "leave record")
    ):
        intents.add("leave_record_query")
    if "information_research" in compatible and any(
        token in text for token in ("搜索", "检索", "公开信息", "调研", "研究", "资料", "research")
    ):
        intents.add("information_research")
    if "knowledge_lookup" in compatible and any(
        token in text for token in ("知识", "政策", "规定", "制度", "资料", "knowledge")
    ):
        intents.add("knowledge_lookup")
    if "risk_analysis" in compatible and any(
        token in text for token in ("风险", "授信", "信用", "合规", "风控", "risk", "credit")
    ):
        intents.add("risk_analysis")
    if "document_generation" in compatible and any(
        token in text for token in ("文档", "证明", "申请书", "请假书", "请假条", "docx", "word")
    ):
        intents.add("document_generation")
    if "report_generation" in compatible and any(token in text for token in ("报告", "总结", "汇报")):
        intents.add("report_generation")
    if "message_or_email_send" in compatible and any(token in text for token in ("发送", "发给", "邮件", "通知")):
        intents.add("message_or_email_send")
    if "meeting_arrangement" in compatible and any(token in text for token in ("会议", "开会", "参会")):
        intents.add("meeting_arrangement")
    if "schedule_management" in compatible and any(token in text for token in ("日程", "待办", "提醒", "有空")):
        intents.add("schedule_management")
    if "weather_query" in compatible and any(
        token in text for token in ("天气", "气温", "温度", "weather")
    ):
        intents.add("weather_query")
    if "travel_service" in compatible and any(
        token in text for token in ("出差", "差旅", "行程", "travel", "trip")
    ):
        intents.add("travel_service")

    # 只有该 Agent 在映射中只承担一种意图时，才允许用 Agent 身份补足无关键词标题。
    if not intents and len(compatible) == 1:
        intents.update(compatible)
    return intents


def _plan_has_intent(steps: list[dict], intent: str) -> bool:
    return any(intent in _infer_step_intents(step) for step in steps if isinstance(step, dict))


def _first_step_index_for_intent(steps: list[dict], intent: str) -> int:
    for index, step in enumerate(steps):
        if isinstance(step, dict) and intent in _infer_step_intents(step):
            return index
    return len(steps)


def _string_list(value) -> list[str]:
    """把 Planner 的单值/数组字段统一为去重后的非空字符串列表。"""
    if value is None:
        return []
    raw_items = value if isinstance(value, (list, tuple, set)) else [value]
    return list(dict.fromkeys(
        str(item).strip() for item in raw_items if str(item).strip()
    ))


def _step_subtask_ids(step: dict) -> list[str]:
    """兼容新版 subtask_ids 与旧版 subtask_id。"""
    ids = _string_list(step.get("subtask_ids"))
    if ids:
        return ids
    return _string_list(step.get("subtask_id"))


def _step_declared_intents(step: dict) -> list[str]:
    """兼容新版 intents 与旧版 intent。"""
    intents = _string_list(step.get("intents"))
    if intents:
        return intents
    return _string_list(step.get("intent"))


def _validate_plan_against_task_profile(steps: list, state: State) -> list[str]:
    """检查 Planner 是否忠实覆盖画像，不自动补写或复制任何计划步骤。"""
    if not isinstance(steps, list):
        return ["计划不是步骤数组"]
    profile = state.get("task_profile") or {}
    subtasks = profile.get("subtasks") or []
    if not isinstance(subtasks, list) or not subtasks:
        return []

    errors: list[str] = []
    subtask_by_id = {
        str(item.get("id")): item
        for item in subtasks
        if isinstance(item, dict) and item.get("id")
    }
    # 当前执行器的调度单位是 Agent 调用，同一 Agent 的多个 assigned_steps
    # 会随同一份 execution brief 一起交给 Agent。把同一 Agent 拆成多个计划步骤
    # 不会形成更细粒度执行，反而可能重复调用同一组工具，因此统一要求合并。
    step_indexes_by_agent: dict[str, list[int]] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        agent_name = str(step.get("agent_name") or "").strip()
        if agent_name:
            step_indexes_by_agent.setdefault(agent_name, []).append(index)
    for agent_name, indexes in step_indexes_by_agent.items():
        if len(indexes) > 1:
            errors.append(
                f"Agent {agent_name} 被拆成了 {len(indexes)} 个执行步骤；"
                "当前执行器按 Agent 调用，请合并为一个步骤"
            )

    # 结构化计划中，TaskProfile 子任务是“逻辑任务”，Planner step 是“执行单元”。
    # 同一个 Agent 能一次完成多个兼容逻辑任务时，允许一个 step 覆盖多个
    # subtask_ids；但每个逻辑子任务仍必须且只能被覆盖一次。
    structured_steps = [
        step for step in steps
        if isinstance(step, dict) and _step_subtask_ids(step)
    ]
    if structured_steps:
        if len(structured_steps) != len(steps):
            errors.append(
                "计划步骤不能混用有 subtask_ids/subtask_id 和无逻辑子任务标识的两种结构"
            )

        step_by_subtask_id: dict[str, dict] = {}
        step_index_by_subtask_id: dict[str, int] = {}
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            covered_ids = _step_subtask_ids(step)
            if not covered_ids:
                continue
            for subtask_id in covered_ids:
                if subtask_id in step_by_subtask_id:
                    errors.append(f"子任务 {subtask_id} 被多个执行步骤重复覆盖")
                    continue
                if subtask_id not in subtask_by_id:
                    errors.append(
                        f"计划引用了 TaskProfile 中不存在的子任务 {subtask_id}"
                    )
                    continue
                step_by_subtask_id[subtask_id] = step
                step_index_by_subtask_id[subtask_id] = index

        for subtask_id in subtask_by_id:
            if subtask_id not in step_by_subtask_id:
                errors.append(f"缺少对子任务 {subtask_id} 的执行覆盖")

        # 按执行步骤校验它覆盖的意图集合，以及跨执行步骤的依赖关系。
        # 同一步内部的逻辑依赖由该 Agent 在一次调用中完成，不应再形成图上的自依赖。
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            covered_ids = [
                subtask_id for subtask_id in _step_subtask_ids(step)
                if subtask_id in subtask_by_id
            ]
            if not covered_ids:
                continue

            expected_intents = {
                str(subtask_by_id[subtask_id].get("intent") or "")
                for subtask_id in covered_ids
                if str(subtask_by_id[subtask_id].get("intent") or "")
            }
            planned_intents = set(_step_declared_intents(step))
            if planned_intents != expected_intents:
                errors.append(
                    f"执行步骤 {step.get('step_id') or index + 1} 的 intents 应为 "
                    f"{sorted(expected_intents)}，实际为 {sorted(planned_intents)}"
                )

            covered_set = set(covered_ids)
            expected_dependencies: set[str] = set()
            for subtask_id in covered_ids:
                expected_dependencies.update(
                    _string_list(subtask_by_id[subtask_id].get("depends_on"))
                )
            # 同一步内的前后关系由 Agent 内部完成，只保留跨步骤依赖。
            expected_dependencies.difference_update(covered_set)
            planned_dependencies = set(
                _string_list(step.get("depends_on")))
            if planned_dependencies != expected_dependencies:
                errors.append(
                    f"执行步骤 {step.get('step_id') or index + 1} 的 depends_on 应为 "
                    f"{sorted(expected_dependencies)}，实际为 {sorted(planned_dependencies)}"
                )

            for dependency_id in expected_dependencies:
                dependency_index = step_index_by_subtask_id.get(
                    dependency_id, len(steps)
                )
                if dependency_index >= index:
                    errors.append(
                        f"子任务 {dependency_id} 所在执行步骤必须在 "
                        f"{step.get('step_id') or index + 1} 之前完成"
                    )

        return list(dict.fromkeys(errors))

    for subtask in subtasks:
        if not isinstance(subtask, dict):
            continue
        intent = str(subtask.get("intent") or "")
        if intent and not _plan_has_intent(steps, intent):
            expected_agents = "、".join(
                _PROFILE_INTENT_AGENT_PREFERENCES.get(intent, ())) or "匹配该意图的 Agent"
            errors.append(f"缺少意图 {intent} 的独立步骤，应由 {expected_agents} 执行")
            continue
        current_index = _first_step_index_for_intent(steps, intent)
        for dependency_id in subtask.get("depends_on") or []:
            dependency = subtask_by_id.get(str(dependency_id))
            dependency_intent = str((dependency or {}).get("intent") or "")
            if not dependency_intent:
                continue
            dependency_index = _first_step_index_for_intent(
                steps, dependency_intent)
            # 两个逻辑意图可由同一 Agent 在同一步内完成；仅后置才是错误。
            if dependency_index > current_index:
                errors.append(
                    f"意图 {dependency_intent} 必须在 {intent} 之前或同一执行步骤内完成"
                )
    return list(dict.fromkeys(errors))


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
            logger.warning(
                f"Step {step_idx + 1}: Agent '{agent_name}' not found in registry")
            errors.append(
                f"Step {step_idx + 1}: Agent '{agent_name}' is not available for this user"
            )
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
                source_references = {
                    str(prev_step.get("agent_name") or ""),
                    str(prev_step.get("step_id") or ""),
                    str(prev_step.get("subtask_id") or ""),
                }
                source_references.update(_step_subtask_ids(prev_step))
                if str(source_step) in source_references:
                    source_found = True
                    # Verify source_output is in the source agent's produces
                    source_metadata = agent_metadata.get(
                        prev_step.get("agent_name")
                    )
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
    logger.info("publisher evaluating next action in %s mode ",
                state["workflow_mode"])

    if state["workflow_mode"] == "launch":
        cache.restore_system_node(
            state["workflow_id"], PUBLISHER, state["user_id"])
        messages = _sanitize_messages(
            apply_prompt_template("publisher", state))
        response = await (
            get_llm_by_type(AGENT_LLM_MAP["publisher"])
            .with_structured_output(Router)
            .ainvoke(messages)
        )

        try:
            agent = response["next"]
        except Exception as e:
            try:
                preview = response.model_dump() if hasattr(
                    response, "model_dump") else response
                try:
                    preview_str = json.dumps(preview, ensure_ascii=False)
                except Exception:
                    preview_str = str(preview)
                logger.error(
                    f"publisher response parse error: {e}; response={preview_str}")
            except Exception as inner:
                logger.error(
                    f"publisher response parse error and printing failed: {inner}")
            raise

        if agent == "FINISH":
            goto = "__end__"
            logger.info("Workflow completed \n")
            cache.restore_node(
                state["workflow_id"], goto, state["initialized"], state["user_id"]
            )
            return Command(goto=goto, update={"next": goto})

        cache.restore_system_node(
            state["workflow_id"], agent, state["user_id"])
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
                    # Publisher 的后续判断依赖严格的 {"next": "agent_name"} 记录。
                    # 使用机器可读 JSON，避免模型无法识别自然语言状态而重复派发。
                    "content": json.dumps({"next": agent}, ensure_ascii=False),
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
        metadata={
            "task_id": state.get("task_id"),
            "current_step": state.get("current_step"),
            "node_name": "agent_proxy",
            "workflow_id": state.get("workflow_id"),
            "workflow_mode": state.get("workflow_mode"),
            "USER_QUERY": state.get("USER_QUERY"),
            "task_profile": state.get("task_profile", {}),
            "task_type": state.get("task_type"),
            "business_goal": state.get("business_goal"),
            "data_scope": state.get("data_scope"),
            "operation_mode": state.get("operation_mode"),
            "scenario_tags": state.get("scenario_tags", []),
            "expected_capabilities": state.get("expected_capabilities", []),
            "risk_profile": state.get("risk_profile", "LOW"),
            "scenario_fit_cache": state.get("scenario_fit_cache", {}),
        },
    )

    # 为执行 Agent 补充明确的当前任务上下文。Production 阶段的用户消息通常只有
    # “Confirm execution”，若不附带原问题和规划步骤，Agent 会自行猜测甚至读取旧文件。
    current_plan = cache.get_planning_steps(state["workflow_id"]) or []
    assigned_steps = [
        step
        for step in current_plan
        if isinstance(step, dict) and step.get("agent_name") == state["next"]
    ]
    # Keep the plan's concrete operation mode for evidence and authorization.
    # Collapsing approve/delete into generic write/send would allow a weaker
    # receipt to satisfy a contract that explicitly requires a trusted verifier.
    def _normalize_legacy_operation_mode(value: Any) -> str:
        mode = str(value or "").strip().lower()
        if mode in {"query", "lookup", "search"}:
            return "read"
        if mode in {
            "read", "write", "send", "delete", "update", "create",
            "submit", "approve", "execute", "export",
        }:
            return mode
        return "write" if mode else ""

    mode_rank = {
        "read": 0,
        "generate": 1,
        "write": 2,
        "update": 3,
        "create": 3,
        "export": 3,
        "execute": 4,
        "send": 5,
        "submit": 6,
        "approve": 7,
        "delete": 7,
    }
    normalized_step_modes = [
        (step, _normalize_legacy_operation_mode(step.get("operation_mode")))
        for step in assigned_steps
        if isinstance(step, dict) and step.get("operation_mode")
    ]
    fallback_mode = _normalize_legacy_operation_mode(state.get("operation_mode")) or "read"
    selected_step, step_operation_mode = max(
        normalized_step_modes or [(None, fallback_mode)],
        key=lambda item: mode_rank.get(item[1], 2),
    )
    selected_contract = (
        selected_step.get("verification_contract")
        if isinstance(selected_step, dict)
        else None
    )
    verification_contract = (
        dict(selected_contract) if isinstance(selected_contract, dict) else {}
    )
    step_risk_level = str(
        (selected_step or {}).get("risk_level")
        or state.get("risk_profile")
        or "LOW"
    )
    context.metadata["operation_mode"] = step_operation_mode
    context.metadata["risk_level"] = step_risk_level
    context.metadata["verification_contract"] = verification_contract
    await enforce_agent_dispatch(_agent, context)
    execution_brief = {
        "original_user_query": state.get("original_user_query")
        or state.get("USER_QUERY")
        or "",
        "assigned_agent": state["next"],
        "assigned_steps": assigned_steps,
        "task_profile": state.get("task_profile", {}),
        "instruction": (
            "Complete only the assigned steps below. Base the answer on the "
            "original user query. Do not inspect unrelated local workflow files."
        ),
    }
    messages_to_send = list(state["messages"]) + [
        {
            "role": "user",
            "content": "EXECUTION_CONTEXT\n"
            + json.dumps(execution_brief, ensure_ascii=False, default=str),
        }
    ]

    execute_result = await execute_agent(_agent, messages_to_send, context)
    if not execute_result.is_success:
        error_detail = execute_result.error or "Unknown executor error"
        logger.warning("Agent '%s' execution failed: %s", _agent.agent_name, error_detail)
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

    # Execution-engine (Phase 2): optionally capture this step's output as a typed
    # Artifact. Gated OFF by default and wrapped so capture never breaks the legacy
    # flow; the messages/return below are unchanged.
    captured_artifact = None
    step_key = f"{state.get('current_step')}:{_agent.agent_name}"
    if artifact_capture_enabled:
        try:
            from src.manager.executor.artifact_adapter import to_artifact
            from src.interface.artifact import StepResult, StepStatus

            captured_artifact = to_artifact(
                execute_result,
                step=None,  # legacy publisher/while path has no TaskStep
                context=context,
                logical_name=f"{state['next']}_result",
            )
            artifacts = state.get("artifacts")
            if not isinstance(artifacts, dict):
                artifacts = {}
            artifacts[captured_artifact.artifact_id] = captured_artifact.model_dump()
            state["artifacts"] = artifacts

            step_results = state.get("step_results")
            if not isinstance(step_results, dict):
                step_results = {}
            captured_status = (
                StepStatus.SUCCEEDED
                if execute_result.is_success
                else StepStatus.FAILED
            )
            step_results[step_key] = StepResult(
                step_id=step_key,
                status=captured_status,
                outputs=(
                    {captured_artifact.logical_name: captured_artifact.ref()}
                    if execute_result.is_success
                    else {}
                ),
                error=None if execute_result.is_success else execute_result.error,
            ).model_dump()
            state["step_results"] = step_results
            logger.info(
                "artifact captured: %s (%s) for agent %s",
                captured_artifact.artifact_id,
                captured_artifact.logical_name,
                _agent.agent_name,
            )
        except Exception as exc:  # pragma: no cover - defensive; never break flow
            logger.warning("artifact capture skipped: %s", exc)

    skill_step_evidence = dict(state.get("skill_step_evidence") or {})
    try:
        from src.skills.execution_evidence import build_step_evidence

        evidence = build_step_evidence(
            step_id=step_key,
            agent_name=_agent.agent_name,
            operation_mode=step_operation_mode,
            risk_level=step_risk_level,
            verification_contract=verification_contract,
            execute_result=execute_result,
            artifact=captured_artifact,
        )
        skill_step_evidence[step_key] = evidence.model_dump(mode="json")
    except Exception as exc:  # pragma: no cover - evidence must not break execution
        logger.warning("skill execution evidence capture skipped: %s", exc)

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
                },
                {
                    # LangChain 消息 content 只接受字符串或内容块列表，不能直接放 dict。
                    # 序列化后仍可在 Publisher 上下文中保留完整结构化结果。
                    "content": json.dumps(structured_result, ensure_ascii=False, default=str),
                    "tool": "agent_proxy",
                    "role": "assistant",
                }
            ],
            "processing_agent_name": _agent.agent_name,
            "agent_name": _agent.agent_name,
            "workflow_execution_failed": bool(state.get("workflow_execution_failed"))
            or not execute_result.is_success,
            "skill_step_evidence": skill_step_evidence,
        },
        # A failed Agent cannot produce a valid dependency for publisher or any
        # subsequent Agent. End the legacy loop after recording the failure.
        goto="__end__" if not execute_result.is_success else "publisher",
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
    plan_validation_failed = False
    runtime_event_handler = state.get("runtime_event_handler")

    if state.get("workflow_mode") == "launch" and state.get("workflow_skill_match"):
        steps = state.get("planning_steps") or cache.get_planning_steps(state["workflow_id"])
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except Exception:
                steps = []
        validation_errors: list[str] = []
        skill_plan_valid = False
        if isinstance(steps, list) and steps:
            try:
                data_flow_valid, data_flow_errors = await _validate_plan_data_flow(
                    steps, state.get("user_id", "")
                )
                profile_errors = _validate_plan_against_task_profile(steps, state)
                validation_errors = list(data_flow_errors) + list(profile_errors)
                skill_plan_valid = data_flow_valid and not profile_errors
            except Exception as exc:
                validation_errors = [f"skill plan validation error: {exc}"]
        if isinstance(steps, list) and steps and skill_plan_valid:
            raw_content = json.dumps({"steps": steps}, ensure_ascii=False)
            message_content = json.dumps({"steps": steps}, indent=2, ensure_ascii=False)
            cache.restore_planning_steps(state["workflow_id"], steps, state["user_id"])
            goto = "__end__" if state.get("stop_after_planner") else "publisher"
            cache.restore_system_node(state["workflow_id"], goto, state["user_id"])
            return Command(
                update={
                    "messages": [{"content": message_content, "tool": "planner", "role": "assistant"}],
                    "agent_name": "planner",
                    "full_plan": raw_content,
                    "planning_steps": steps,
                },
                goto=goto,
            )
        if validation_errors:
            logger.warning(
                "Rejected matched workflow skill during current-plan validation: %s",
                "; ".join(validation_errors),
            )
            if callable(runtime_event_handler):
                await runtime_event_handler(
                    {
                        "event": "skill_rejected",
                        "agent_name": "planner",
                        "data": {
                            "skill_id": (
                                state.get("workflow_skill_match") or {}
                            ).get("skill_id", ""),
                            "reason": "current_plan_validation_failed",
                            "errors": validation_errors,
                        },
                    }
                )
        state["workflow_skill_match"] = {}
        state["reused_skill_id"] = ""
        state["reused_skill_owner_id"] = ""
        state["planning_steps"] = []
        cache.restore_planning_steps(state["workflow_id"], [], state["user_id"])

    routing_decision = state.get("routing_decision") or {}
    if state["workflow_mode"] == "launch" and routing_decision.get("decision") != "DISPATCH":
        decision = routing_decision.get("decision", "CLARIFY")
        reason_codes = routing_decision.get("reason_codes") or [
            "NO_CAPABLE_AGENT"]
        task_profile = state.get("task_profile") or {}
        missing_fields = task_profile.get("missing_fields") or []
        thought = (
            f"主Agent路由决策为 {decision}，原因：{', '.join(reason_codes)}。"
            + (f" 需要补充字段：{', '.join(missing_fields)}。" if missing_fields else "")
        )
        content = json.dumps(
            {"thought": thought, "steps": [], "new_agents_needed": []},
            ensure_ascii=False,
        )
        cache.restore_planning_steps(
            state["workflow_id"], [], state["user_id"])
        cache.restore_system_node(
            state["workflow_id"], "__end__", state["user_id"])
        if callable(runtime_event_handler):
            await runtime_event_handler(
                {
                    "event": "planner_delta",
                    "agent_name": "planner",
                    "data": {
                        "delta": {"content": content},
                        "full_content": content,
                        "is_final": True,
                    },
                }
            )
        return Command(
            update={
                "messages": [{"content": content, "tool": "planner", "role": "assistant"}],
                "agent_name": "planner",
                "full_plan": content,
                "planning_steps": [],
            },
            goto="__end__",
        )

    if state["workflow_mode"] == "launch":
        prompt_state = dict(state)
        prompt_state = _ensure_scenario_prompt_defaults(prompt_state)
        history = prompt_state.get("instruction_history") or []
        if not isinstance(history, list):
            history = [str(history)]
        if history:
            history_text = "\n".join(
                f"{idx + 1}. {item}" for idx, item in enumerate(history))
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
        messages = _sanitize_messages(
            apply_prompt_template("planner", prompt_state))
        llm = get_llm_by_type(AGENT_LLM_MAP["planner"])
        if state.get("deep_thinking_mode"):
            llm = get_llm_by_type("reasoning")

        # Log LLM preparation time
        prep_time = time.time()
        prep_duration = prep_time - start_time
        logger.info("[PERF] Prompt preparation: %.2fs", prep_duration)

        if state.get("search_before_planning"):
            search_start = time.time()
            searched_content = _search_before_planning(state)
            search_time = time.time() - search_start
            logger.info("[PERF] Web search: %.2fs", search_time)
            messages = _append_search_context(messages, searched_content)

        cache.restore_system_node(
            state["workflow_id"], PLANNER, state["user_id"])

        # Log LLM call start
        llm_start = time.time()
        model_type = "reasoning" if state.get(
            "deep_thinking_mode") else AGENT_LLM_MAP["planner"]
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
        state["historical_plan"] = cache.get_planning_steps(
            state["workflow_id"])
        state["adjustment_instruction"] = state.get("polish_instruction")

        messages = _sanitize_messages(
            apply_prompt_template("planner_polishment", state))
        llm = get_llm_by_type(AGENT_LLM_MAP["planner"])
        if state.get("deep_thinking_mode"):
            llm = get_llm_by_type("reasoning")

        prep_time = time.time()
        logger.info("[PERF] Polish prompt preparation: %.2fs",
                    prep_time - polish_start)

        if state.get("search_before_planning"):
            search_start = time.time()
            searched_content = _search_before_planning(state)
            search_time = time.time() - search_start
            logger.info("[PERF] Polish web search: %.2fs", search_time)
            messages = _append_search_context(messages, searched_content)

        llm_start = time.time()
        model_type = "reasoning" if state.get(
            "deep_thinking_mode") else AGENT_LLM_MAP["planner"]
        logger.info(
            "[PERF] Polish starting LLM call (model: %s)...", model_type)

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
                retry_content = clean_response_tags(
                    getattr(retry_response, "content", ""))

                retry_time = time.time() - retry_start
                logger.info("[PERF] Retry LLM call: %.2fs", retry_time)

                if retry_content:
                    steps = _extract_plan_steps(retry_content)
                    if steps is not None:
                        raw_content = retry_content
                        logger.info("[PERF] Retry succeeded")
                    else:
                        logger.warning(
                            "[PERF] Retry failed: still cannot parse JSON")
            except Exception as exc:
                logger.warning("[PERF] Retry exception: %s", exc)

        # 同时校验 Agent 数据流与 TaskProfile 意图/依赖一致性。
        # 校验失败时要求 Planner 重新生成，绝不按数量复制画像步骤。
        if steps is not None and state["workflow_mode"] == "launch":
            validation_start = time.time()
            is_valid, validation_errors = await _validate_plan_data_flow(
                steps, state.get("user_id", "")
            )
            profile_errors = _validate_plan_against_task_profile(steps, state)
            validation_errors = list(validation_errors) + profile_errors
            is_valid = is_valid and not profile_errors
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
                        logger.info(
                            "[PERF] Attempting to fix plan validation errors...")

                        error_summary = "\n".join(
                            f"- {err}" for err in validation_errors)
                        required_subtask_contract = [
                            {
                                "subtask_id": str(item.get("id") or ""),
                                "intent": str(item.get("intent") or ""),
                                "depends_on": [
                                    str(dep) for dep in (item.get("depends_on") or [])
                                ],
                            }
                            for item in (
                                (state.get("task_profile") or {}).get("subtasks") or []
                            )
                            if isinstance(item, dict) and item.get("id")
                        ]
                        required_subtask_contract_text = json.dumps(
                            required_subtask_contract,
                            ensure_ascii=False,
                            indent=2,
                        )
                        # The retry must repair the plan structure, not merely
                        # paraphrase the same invalid merged steps.
                        fix_note = (
                            "The generated plan does not match TaskProfile or its data dependencies.\n\n"
                            f"Validation errors:\n{error_summary}\n\n"
                            "Required logical subtask contract (cover every item exactly once):\n"
                            f"{required_subtask_contract_text}\n\n"
                            "Return a corrected JSON object with a `steps` array only.\n"
                            "Requirements:\n"
                            "1. Each executable step must contain `step_id`, `subtask_ids`, "
                            "`intents`, and `depends_on`. Across the whole plan, every contract "
                            "subtask_id must appear exactly once.\n"
                            "2. The current executor dispatches by Agent invocation. Every Agent "
                            "name may therefore appear in at most one executable step; put all "
                            "logical subtasks assigned to that Agent into that step.\n"
                            "3. `intents` must exactly match the intents of that step's "
                            "`subtask_ids`. `depends_on` contains only dependencies belonging to "
                            "other executable steps; omit dependencies internal to the same step.\n"
                            "4. Preserve execution order for all external dependencies.\n"
                            "5. Use only available Agent names from the supplied catalog.\n"
                            "6. For every Agent `requires` field, add complete input mappings from "
                            "an earlier step and use outputs declared by that source Agent.\n"
                            "Do not create duplicate execution steps for the same Agent.\n"
                            "Do not include explanations outside the JSON."
                        )

                        fix_payload = deepcopy(retry_messages)
                        fix_payload.append(
                            {"role": "assistant", "content": raw_content})
                        fix_payload.append(
                            {"role": "user", "content": fix_note})

                        fix_response = await retry_llm.ainvoke(fix_payload)
                        fix_content = clean_response_tags(
                            getattr(fix_response, "content", ""))

                        fix_time = time.time() - fix_start
                        logger.info(
                            "[PERF] Plan fix LLM call: %.2fs", fix_time)

                        if fix_content:
                            fixed_steps = _extract_plan_steps(fix_content)
                            if fixed_steps is not None:
                                # Validate the fixed plan
                                is_fixed_valid, fixed_errors = await _validate_plan_data_flow(
                                    fixed_steps, state.get("user_id", "")
                                )
                                fixed_profile_errors = _validate_plan_against_task_profile(
                                    fixed_steps, state
                                )
                                fixed_errors = list(
                                    fixed_errors) + fixed_profile_errors
                                is_fixed_valid = is_fixed_valid and not fixed_profile_errors
                                if is_fixed_valid:
                                    steps = fixed_steps
                                    raw_content = fix_content
                                    is_valid = True
                                    logger.info("[PERF] Plan fix succeeded")
                                else:
                                    logger.warning(
                                        f"[PERF] Plan fix failed: still has {len(fixed_errors)} errors"
                                    )
                                    for error in fixed_errors:
                                        logger.warning(f"  - {error}")
                            else:
                                logger.warning(
                                    "[PERF] Plan fix failed: cannot parse JSON")
                    except Exception as exc:
                        logger.warning(f"[PERF] Plan fix exception: {exc}")

                if not is_valid:
                    plan_validation_failed = True
                    steps = []
                    raw_content = json.dumps(
                        {
                            "thought": "Planner 计划未通过任务画像一致性校验，已停止执行。",
                            "validation_errors": validation_errors,
                            "steps": [],
                            "new_agents_needed": [],
                        },
                        ensure_ascii=False,
                    )

        if steps == [] and state["workflow_mode"] == "launch" and not plan_validation_failed:
            fallback_steps = _fallback_plan_steps(state)
            if fallback_steps:
                steps = fallback_steps
                raw_content = json.dumps({"steps": steps}, ensure_ascii=False)
                logger.info(
                    "[PERF] Applied fallback planner steps for obvious single-agent task")

        if steps is not None:
            cache.restore_planning_steps(
                state["workflow_id"], steps, state["user_id"])
            # The final planner message is authoritative for the frontend.
            # Preserve validation errors when a streamed draft was rejected;
            # otherwise the browser keeps showing the raw draft and enables
            # Confirm execution even though the backend stored zero steps.
            if plan_validation_failed:
                message_content = raw_content
            else:
                message_content = json.dumps(
                    {"steps": steps}, indent=2, ensure_ascii=False)
            if state.get("stop_after_planner") and state["workflow_mode"] == "launch":
                goto = "__end__"
        else:
            logger.warning("Planner response is not a valid JSON \n")
            goto = "__end__"
        cache.restore_system_node(state["workflow_id"], goto, state["user_id"])

    total_time = time.time() - start_time
    logger.info("=" * 60)
    logger.info("[PERF] PLANNER TOTAL TIME: %.2fs (mode: %s)",
                total_time, state["workflow_mode"])
    logger.info("=" * 60)

    # Execution-engine wiring: when the scheduler is enabled, convert the
    # validated plan into an explicit TaskGraph so the real web/API path can
    # drive the DAG scheduler. Gated OFF by default and fully fail-safe: any
    # conversion problem (or an unclassifiable side-effect step) leaves
    # ``task_graph`` unset so the legacy publisher/while loop runs unchanged.
    plan_update: dict = {}
    if steps and not plan_validation_failed:
        try:
            from src.service.env import ORCHESTRATION_SCHEDULER_ENABLED

            if ORCHESTRATION_SCHEDULER_ENABLED:
                from src.orchestration.plan_to_task_graph import plan_to_task_graph
                from src.orchestration.runtime import unknown_operation_modes

                tg = plan_to_task_graph(
                    steps,
                    task_id=state.get("workflow_id") or "task",
                    subject=state.get("user_id"),
                    goal=state.get("original_user_query", "")
                    or state.get("USER_QUERY", ""),
                )
                unknown = unknown_operation_modes(tg)
                if unknown:
                    logger.info(
                        "scheduler wiring: skip task_graph (unclassified steps: %s)",
                        unknown,
                    )
                else:
                    tg_dict = tg.model_dump()
                    plan_update["task_graph"] = tg_dict
                    logger.info(
                        "scheduler wiring: task_graph built (%d steps)", len(tg.steps))
                    # Persist a validated PlanSnapshot so a later production
                    # execution can load + verify (workflow/user/plan_hash) the
                    # exact approved plan before entering the scheduler.
                    try:
                        from src.orchestration.plan_snapshot import save_plan_snapshot

                        save_plan_snapshot(
                            workflow_id=state.get("workflow_id") or "task",
                            user_id=state.get("user_id"),
                            planning_steps=steps,
                            task_graph=tg_dict,
                        )
                        logger.info(
                            "scheduler wiring: plan snapshot persisted")
                    except Exception as snap_exc:  # noqa: BLE001 - never break planning
                        logger.warning(
                            "scheduler wiring: plan snapshot persist failed: %s", snap_exc
                        )
        except Exception as exc:  # noqa: BLE001 - stay on the legacy path on any error
            logger.warning(
                "scheduler wiring: plan_to_task_graph failed, staying on legacy: %s", exc
            )

    return Command(
        update={
            "messages": [{"content": message_content, "tool": "planner", "role": "assistant"}],
            "agent_name": "planner",
            "full_plan": raw_content,
            "planning_steps": steps if steps is not None else [],
            **plan_update,
        },
        goto=goto,
    )


async def coordinator_node(state: State) -> Command[Literal["planner", "__end__"]]:
    """Coordinator node."""
    logger.info("Coordinator talking. \n")

    goto = "__end__"
    content = ""

    if state.get("workflow_mode") == "launch" and state.get("workflow_skill_match"):
        cache.restore_system_node(state["workflow_id"], COORDINATOR, state["user_id"])
        cache.restore_system_node(state["workflow_id"], "planner", state["user_id"])
        return Command(
            update={
                "messages": [
                    {"content": "handover_to_planner", "tool": "coordinator", "role": "assistant"}
                ],
                "agent_name": "coordinator",
            },
            goto="planner",
        )

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
        cache.restore_system_node(
            state["workflow_id"], COORDINATOR, state["user_id"])

    content = clean_response_tags(response.content)  # type: ignore
    if "handover_to_planner" in content:
        goto = "planner"
    if state["workflow_mode"] == "launch":
        cache.restore_system_node(
            state["workflow_id"], "planner", state["user_id"])
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
