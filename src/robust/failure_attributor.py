import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from config.global_variables import checkpoints_dir
from src.robust.task_logger import TaskLogger


@dataclass
class FailureAttribution:
    """故障归因结果。"""
    task_id: str
    user_query: str
    is_succeed: bool
    plan: List[str]
    execution_summary: str
    mistake_node: Optional[str]
    mistake_step: Optional[int]
    mistake_reason: Optional[str]
    overall_summary: str


def _get_task_logs_dir() -> Path:
    """返回任务日志目录路径。"""
    logs_dir = checkpoints_dir.parent / "task_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def _format_session_logs(history: List[Dict[str, Any]]) -> str:
    """将历史记录格式化为 DoVer 风格的 session_logs 字符串。

    格式示例：
    [Step 1] coordinator (coordinator) [start_of_agent]: Agent coordinator started
    [Step 2] agent_proxy (agent_proxy:researcher) [message]: <content>
    [Step 3] publisher (publisher) [end_of_agent -> reporter]: Agent publisher finished
    """
    lines = []
    for item in history:
        step = item.get("step", 0)
        name = item.get("node_name", "unknown")
        role = item.get("role", "")
        event = item.get("event", "")
        content = item.get("content", "")

        # 构建节点标识：node_name 或 node_name:sub_agent_name
        if name == "agent_proxy" and item.get("sub_agent_name"):
            node_id = f"{name}:{item.get('sub_agent_name')}"
        else:
            node_id = name

        # 构建事件标识：包含流转信息
        event_str = event or "unknown"
        if event == "end_of_agent" and item.get("next_node"):
            event_str = f"{event} -> {item.get('next_node')}"

        lines.append(
            f"[Step {step}] {node_id} ({role}) [{event_str}]: {json.dumps(content, ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _safe_json_loads(text: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 返回的 JSON，失败时返回 None。"""
    if not text:
        return None
    raw = text.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    if "```" in raw:
        try:
            first = raw.find("```")
            last = raw.rfind("```")
            if first != -1 and last != -1 and last > first:
                inner = raw[first + 3:last].strip()
                if inner.startswith("json"):
                    inner = inner[4:].strip()
                return json.loads(inner)
        except Exception:
            return None
    return None


def _extract_plan_from_history(history: List[Dict[str, Any]]) -> List[str]:
    """从历史记录中提取规划内容。"""
    plans = []
    for item in history:
        if item.get("node_name") == "planner" and item.get("event") == "message":
            content = item.get("content", "")
            if content:
                plans.append(content)
    return plans


def _extract_error_from_history(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从历史记录中提取错误信息。"""
    for item in reversed(history):
        if item.get("event") == "error":
            return item
    return None


class FailureAttributor:
    def __init__(self, llm_client: Any = None, model: Optional[str] = None) -> None:
        """初始化故障归因器。"""
        self.llm_client = llm_client
        self.model = model

    def analyze(self, task_log: Dict[str, Any]) -> FailureAttribution:
        """对全量日志进行故障归因分析。"""
        history = task_log.get("history", [])
        task_id = task_log.get("task_id", "")
        user_query = task_log.get("user_query", "")
        is_succeed = task_log.get("status") == "completed"

        # 提取规划内容
        plan = _extract_plan_from_history(history)

        # 提取执行摘要（非 planner 的消息）
        execution_parts = []
        for item in history:
            if item.get("event") == "message" and item.get("node_name") != "planner":
                content = item.get("content", "")
                if content:
                    execution_parts.append(content)
        execution_summary = "\n".join(execution_parts)

        # 提取错误信息
        error_item = _extract_error_from_history(history)
        mistake_node = None
        mistake_step = None
        mistake_reason = None

        if not is_succeed:
            if error_item:
                mistake_node = error_item.get("node_name")
                mistake_step = int(error_item.get("step", 0))
                mistake_reason = error_item.get("content", "")
            elif history:
                # 如果没有显式错误，取最后一条记录作为启发式归因
                last_entry = history[-1]
                mistake_node = last_entry.get("node_name")
                mistake_step = int(last_entry.get("step", 0))
                mistake_reason = "heuristic: no explicit error found"

        overall_summary = f"Task {'succeeded' if is_succeed else 'failed'}"

        # 使用 LLM 进行深度分析
        if self.llm_client:
            prompt = """你是多智能体系统的故障归因专家。请分析以下任务日志，生成故障归因报告。

输出 JSON 格式：
{
    "is_succeed": bool,           // 任务是否成功
    "plan": ["规划内容"],          // 提取的规划步骤
    "execution_summary": "str",    // 执行过程摘要
    "mistake_node": "str|null",    // 出错的节点名称
    "mistake_step": int|null,      // 出错的步骤编号
    "mistake_reason": "str|null",  // 错误原因分析
    "overall_summary": "str"       // 整体摘要
}"""
            session_logs = _format_session_logs(history)
            user_msg = (
                f"task_id: {task_id}\n"
                f"user_query: {user_query}\n"
                f"task_status: {task_log.get('status', 'unknown')}\n\n"
                f"session_logs:\n{session_logs}"
            )
            try:
                response = self.llm_client.invoke(
                    [SystemMessage(content=prompt), HumanMessage(content=user_msg)]
                )
                parsed = _safe_json_loads(getattr(response, "content", "") or "")
                if parsed:
                    is_succeed = bool(parsed.get("is_succeed")) if parsed.get("is_succeed") is not None else is_succeed
                    plan = parsed.get("plan") or plan
                    execution_summary = parsed.get("execution_summary") or execution_summary
                    mistake_node = parsed.get("mistake_node") or mistake_node
                    mistake_step = parsed.get("mistake_step") if parsed.get("mistake_step") is not None else mistake_step
                    mistake_reason = parsed.get("mistake_reason") or mistake_reason
                    overall_summary = parsed.get("overall_summary") or overall_summary
            except Exception as e:
                # LLM 分析失败，使用规则提取的结果
                pass

        return FailureAttribution(
            task_id=task_id,
            user_query=user_query,
            is_succeed=is_succeed,
            plan=plan,
            execution_summary=execution_summary,
            mistake_node=mistake_node,
            mistake_step=mistake_step,
            mistake_reason=mistake_reason,
            overall_summary=overall_summary,
        )

    async def attribute(self, task_id: str) -> Optional[FailureAttribution]:
        """加载 TaskLogger 并生成归因结果。"""
        task_log = TaskLogger.load(task_id)
        if not task_log:
            return None
        task_log_dict = task_log.to_dict()
        result = self.analyze(task_log_dict)
        
        # 保存归因结果
        out_path = _get_task_logs_dir() / f"{task_id}_attribution.json"
        out_path.write_text(
            json.dumps(result.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return result

    def get_failed_attribution(self, attribution: FailureAttribution) -> Optional[FailureAttribution]:
        """如果任务失败，返回归因结果；否则返回 None。"""
        if not attribution.is_succeed:
            return attribution
        return None
