"""
Base handler implementation with common utilities.
"""

from abc import abstractmethod
from typing import Any, Dict, List, Optional

from src.robust.hooks.base import Action, Handler, HookContext, HookResult


class BaseHandler(Handler):
    """
    处理器基类，提供通用工具方法。
    """
    
    def _get_llm_client(self, ctx: HookContext) -> Optional[Any]:
        """从上下文获取 LLM 客户端。"""
        return ctx.state.get("__llm_client__")
    
    def _get_checkpoint_manager(self, ctx: HookContext) -> Optional[Any]:
        """从上下文获取检查点管理器。"""
        return ctx.state.get("__checkpoint_manager__")
    
    def _inject_message(self, state: Dict[str, Any], message: str, role: str = "system") -> Dict[str, Any]:
        """向状态注入消息。"""
        import copy
        patched = copy.deepcopy(state)
        messages = patched.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        messages.append({"role": role, "content": message})
        patched["messages"] = messages
        return patched
    
    def _mark_auto_recovery_attempted(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """标记已尝试自动恢复。"""
        import copy
        patched = copy.deepcopy(state)
        patched["__auto_recovery_attempted"] = True
        return patched
