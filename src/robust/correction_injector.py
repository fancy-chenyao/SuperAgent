import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from config.global_variables import checkpoints_dir
from src.robust.failure_attributor import FailureAttribution
from src.robust.rollback_controller import RollbackTarget


@dataclass
class InjectionResult:
    patched_state: Dict[str, Any]
    injection_text: str
    target_node: str
    target_step: int
    metadata: Dict[str, Any]


def _get_task_logs_dir() -> Path:
    """返回任务日志目录路径。"""
    logs_dir = checkpoints_dir.parent / "task_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


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


def _select_context_history(
    history: List[Dict[str, Any]],
    target_step: int,
    window: int = 2,
) -> List[Dict[str, Any]]:
    """截取目标步骤前后上下文。"""
    if not history:
        return []
    step_to_item = {int(item.get("step", 0)): item for item in history}
    start = max(0, target_step - window)
    end = target_step
    return [step_to_item[s] for s in range(start, end + 1) if s in step_to_item]


class CorrectionInjector:
    def __init__(self, llm_client: Any = None, model: Optional[str] = None) -> None:
        """初始化纠错注入器。"""
        self.llm_client = llm_client
        self.model = model

    async def generate_injection(
        self,
        user_query: str,
        attribution: FailureAttribution,
        context_history: List[Dict[str, Any]],
    ) -> str:
        """调用 LLM 生成纠错替换文本。"""
        if not self.llm_client:
            reason = attribution.mistake_reason or "unknown"
            return f"请根据失败原因进行纠正，避免重复错误。失败原因: {reason}"
        context_lines = []
        for item in context_history:
            step = item.get("step", 0)
            name = item.get("node_name", "unknown")
            content = item.get("content", "")
            context_lines.append(f"[Step {step}] {name}: {content}")
        prompt = (
            "你是多智能体纠错注入器，需要生成最小干预指令。\n"
            f"user_query: {user_query}\n"
            f"mistake_node: {attribution.mistake_node}\n"
            f"mistake_reason: {attribution.mistake_reason}\n"
            "context:\n" + "\n".join(context_lines) + "\n"
            "请返回 JSON：{category: str, replacement_text: str}"
        )
        response = self.llm_client.invoke([SystemMessage(content="你是纠错注入助手。"), HumanMessage(content=prompt)])
        parsed = _safe_json_loads(getattr(response, "content", "") or "")
        if parsed and parsed.get("replacement_text"):
            return str(parsed.get("replacement_text"))
        return getattr(response, "content", "") or "请修正当前步骤并继续执行。"

    def inject_into_state(
        self,
        state: Dict[str, Any],
        injection_text: str,
        target_node: str,
    ) -> Dict[str, Any]:
        """将纠错文本注入 state 并返回 patched_state。"""
        patched = copy.deepcopy(state)
        messages = patched.get("messages")
        if not isinstance(messages, list):
            messages = []
        messages.append({"role": "system", "content": injection_text})
        patched["messages"] = messages
        metadata = patched.get("__injection_metadata__") if isinstance(patched.get("__injection_metadata__"), dict) else {}
        metadata.update({"target_node": target_node})
        patched["__injection_metadata__"] = metadata
        patched["__resume_hint__"] = target_node
        patched["__auto_recovery_attempted"] = True
        return patched

    async def apply(
        self,
        task_id: str,
        attribution: FailureAttribution,
        rollback_target: RollbackTarget,
        task_log: Dict[str, Any],
    ) -> InjectionResult:
        """生成纠错文本并注入到回退状态。"""
        history = task_log.get("history", [])
        context_history = _select_context_history(history, attribution.mistake_step or rollback_target.rollback_step)
        user_query = task_log.get("user_query", "")
        injection_text = await self.generate_injection(user_query, attribution, context_history)
        target_node = attribution.mistake_node or rollback_target.checkpoint.node_name
        patched_state = self.inject_into_state(rollback_target.checkpoint.state, injection_text, target_node)
        metadata = {
            "task_id": task_id,
            "mistake_step": attribution.mistake_step,
            "rollback_step": rollback_target.rollback_step,
            "target_node": target_node,
            "injection_text": injection_text,
        }
        out_path = _get_task_logs_dir() / f"{task_id}_injection.json"
        out_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return InjectionResult(
            patched_state=patched_state,
            injection_text=injection_text,
            target_node=target_node,
            target_step=rollback_target.rollback_step,
            metadata=metadata,
        )
