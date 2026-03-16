"""
Base rule implementation with common utilities.
"""

from abc import abstractmethod
from typing import List

from src.robust.hooks.base import Action, HookContext, HookPoint, Rule


class BaseRule(Rule):
    """
    规则基类，提供通用工具方法。
    """
    
    @property
    def priority(self) -> int:
        return 100
    
    def _get_last_message(self, ctx: HookContext) -> str:
        """获取最后一条消息内容。"""
        if ctx.history:
            for item in reversed(ctx.history):
                if item.get("event") == "message":
                    return item.get("content", "")
        return ""
    
    def _get_plans(self, ctx: HookContext) -> List[str]:
        """获取所有规划内容。"""
        plans = []
        for item in ctx.history:
            if item.get("node_name") == "planner" and item.get("event") == "message":
                content = item.get("content", "")
                if content:
                    plans.append(content)
        return plans
    
    def _get_error(self, ctx: HookContext) -> dict:
        """获取错误信息。"""
        for item in reversed(ctx.history):
            if item.get("event") == "error":
                return item
        return {}
    
    def _count_agent_executions(self, ctx: HookContext, agent_name: str) -> int:
        """统计某 agent 的执行次数。"""
        count = 0
        for item in ctx.history:
            if item.get("node_name") == "agent_proxy":
                if item.get("sub_agent_name") == agent_name:
                    count += 1
        return count
    
    def _get_workflow_status(self, ctx: HookContext) -> str:
        """获取工作流状态。"""
        for item in reversed(ctx.history):
            if item.get("event") == "workflow_end":
                return "completed"
            if item.get("event") == "error":
                return "failed"
        return "running"
