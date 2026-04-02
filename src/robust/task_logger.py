"""
TaskLogger: Structured logging for each task execution instance.
Log format is inspired by the Agents_Failure_Attribution dataset (Who&When),
designed to support post-hoc failure attribution and step-level rollback.

Log structure per task (stored as JSON):
{
    "task_id": "...",
    "workflow_id": "...",
    "user_query": "...",
    "created_at": "...",
    "finished_at": "...",
    "status": "running|completed|failed",
    "history": [
        {
            "step": 0,
            "node_name": "coordinator",
            "role": "coordinator",
            "content": "...",
            "timestamp": "...",
            "event": "start_of_agent|end_of_agent|message|error"
        },
        ...
    ],
    "error": null | "error message if failed"
}
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.global_variables import checkpoints_dir

logger = logging.getLogger(__name__)

# Task logs are stored alongside checkpoints in a sibling directory
def _get_task_logs_dir() -> Path:
    logs_dir = checkpoints_dir.parent / "task_logs"
    if not logs_dir.exists():
        logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


class TaskLogger:
    """
    Records the complete interaction history of a single task execution.
    Each task run gets its own JSON log file, keyed by task_id.
    The log format mirrors the Agents_Failure_Attribution dataset to support
    failure attribution and step-level replay/rollback.
    """

    def __init__(self, task_id: str, workflow_id: str, user_query: str = ""):
        self.task_id = task_id
        self.workflow_id = workflow_id
        self.user_query = user_query
        self.created_at = datetime.now().isoformat()
        self.history: List[Dict[str, Any]] = []
        self.status = "running"
        self.error: Optional[str] = None
        self.execution_phase: str = "initial_planning"  # 新增: 执行阶段
        self._step_counter: Dict[str, int] = {}  # track per-node step

        self._logs_dir = _get_task_logs_dir()
        self._log_file = self._logs_dir / f"{task_id}.json"
        logger.info(f"TaskLogger initialized: {self._log_file}")

    def _next_step(self, node_name: str) -> int:
        """Return the current global step count (shared across nodes)."""
        count = self._step_counter.get("__global__", -1) + 1
        self._step_counter["__global__"] = count
        return count

    def log_event(
        self,
        node_name: str,
        event: str,
        content: str = "",
        step: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Append a log entry.

        Args:
            node_name: The agent/node name
            event: Event type: start_of_agent | end_of_agent | message | error | workflow_start | workflow_end
            content: Text content of the event
            step: Explicit step number (auto-incremented if not provided)
            extra: Optional extra fields merged into the entry
        """
        if step is None:
            step = self._next_step(node_name)
        else:
            # Update step counter when explicit step is provided
            # to keep it in sync for subsequent auto-increment calls
            self._step_counter["__global__"] = step

        entry: Dict[str, Any] = {
            "step": step,
            "node_name": node_name,
            "role": node_name,
            "event": event,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        if extra:
            entry.update(extra)
        self.history.append(entry)
        self._flush()

    def log_message(self, node_name: str, content: str, step: Optional[int] = None) -> None:
        """Log an agent output message."""
        self.log_event(node_name=node_name, event="message", content=content, step=step)

    def log_agent_start(self, node_name: str, step: Optional[int] = None, sub_agent_name: Optional[str] = None) -> None:
        """Log the start of an agent node."""
        display_name = f"{node_name}【{sub_agent_name}】" if sub_agent_name else node_name
        self.log_event(
            node_name=node_name,
            event="start_of_agent",
            content=f"Agent {display_name} started",
            step=step,
            extra={"sub_agent_name": sub_agent_name} if sub_agent_name else None
        )

    def log_agent_end(self, node_name: str, next_node: Optional[str] = None, step: Optional[int] = None, sub_agent_name: Optional[str] = None) -> None:
        """Log the end of an agent node."""
        display_name = f"{node_name}【{sub_agent_name}】" if sub_agent_name else node_name
        content = f"Agent {display_name} finished"
        if next_node:
            content += f" -> {next_node}"
        extra = {"next_node": next_node}
        if sub_agent_name:
            extra["sub_agent_name"] = sub_agent_name
        self.log_event(node_name=node_name, event="end_of_agent", content=content, step=step, extra=extra)

    def log_workflow_start(self, user_query: str = "") -> None:
        """Log workflow start."""
        if user_query:
            self.user_query = user_query
        self.log_event(node_name="system", event="workflow_start",
                       content=f"Workflow started. Query: {user_query}", step=0)

    def log_workflow_end(self) -> None:
        """Log workflow successful completion."""
        self.status = "completed"
        self.log_event(node_name="system", event="workflow_end", content="Workflow completed successfully.")
        self._flush()

    def log_error(self, error: str, node_name: str = "system", step: Optional[int] = None) -> None:
        """Log an error event."""
        self.status = "failed"
        self.error = error
        self.log_event(node_name=node_name, event="error", content=error, step=step)
        self._flush()

    def set_execution_phase(self, execution_phase: str) -> None:
        """设置执行阶段"""
        self.execution_phase = execution_phase
        self._flush()

    @staticmethod
    def determine_execution_phase(workmode: str, instruction_history: List[str]) -> str:
        """
        判断执行阶段（静态方法，解耦主流程）

        Args:
            workmode: 工作模式 ("launch" 或 "production")
            instruction_history: 指令历史列表

        Returns:
            执行阶段: "initial_planning" | "re_planning" | "execution"
        """
        # 优先级1: workmode="production" → 确认执行
        if workmode == "production":
            return "execution"

        # 优先级2: instruction_history长度判断
        if len(instruction_history) <= 1:
            return "initial_planning"
        else:
            return "re_planning"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "user_query": self.user_query,
            "execution_phase": self.execution_phase,  # 新增
            "created_at": self.created_at,
            "finished_at": datetime.now().isoformat() if self.status != "running" else None,
            "status": self.status,
            "history": self.history,
            "error": self.error,
        }

    def _flush(self) -> None:
        """Write current log state to disk."""
        try:
            with open(self._log_file, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error(f"Failed to flush task log: {e}")

    @classmethod
    def load(cls, task_id: str) -> Optional["TaskLogger"]:
        """Load an existing task log from disk."""
        logs_dir = _get_task_logs_dir()
        log_file = logs_dir / f"{task_id}.json"
        if not log_file.exists():
            return None
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            inst = cls.__new__(cls)
            inst.task_id = data.get("task_id", task_id)
            inst.workflow_id = data.get("workflow_id", "")
            inst.user_query = data.get("user_query", "")
            inst.created_at = data.get("created_at", "")
            inst.history = data.get("history", [])
            inst.status = data.get("status", "unknown")
            inst.error = data.get("error")

            # 兼容性处理：如果缺少新字段，设置默认值
            inst.execution_phase = data.get("execution_phase", "initial_planning")

            inst._step_counter = {"__global__": len(inst.history)}
            inst._logs_dir = logs_dir
            inst._log_file = log_file
            return inst
        except Exception as e:
            logger.error(f"Failed to load task log {task_id}: {e}")
            return None

    @classmethod
    def list_tasks(cls, workflow_id: Optional[str] = None, execution_phase: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all task log summaries (without full history).
        Optionally filter by workflow_id and execution_phase.
        Returns list sorted newest first.
        """
        logs_dir = _get_task_logs_dir()
        results = []
        if not logs_dir.exists():
            return results
        for log_file in logs_dir.glob("*.json"):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if workflow_id and data.get("workflow_id") != workflow_id:
                    continue
                if execution_phase and data.get("execution_phase") != execution_phase:
                    continue

                # 兼容性处理：如果缺少新字段，设置默认值
                task_execution_phase = data.get("execution_phase", "initial_planning")

                results.append({
                    "task_id": data.get("task_id", log_file.stem),
                    "workflow_id": data.get("workflow_id", ""),
                    "user_query": data.get("user_query", ""),
                    "execution_phase": task_execution_phase,
                    "created_at": data.get("created_at", ""),
                    "finished_at": data.get("finished_at"),
                    "status": data.get("status", "unknown"),
                    "step_count": len(data.get("history", [])),
                    "error": data.get("error"),
                })
            except Exception:
                continue
        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return results
