"""
Exception Rule - 异常规则

触发条件：工作流执行过程中出现异常
触发点：ERROR
动作：ROLLBACK
"""

from typing import List

from src.robust.hooks.base import Action, ActionType, HookContext, HookPoint
from src.robust.hooks.rules.base import BaseRule


class ExceptionRule(BaseRule):
    """异常规则：捕获工作流异常并触发回滚。"""
    
    @property
    def name(self) -> str:
        return "exception_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.ERROR]
    
    @property
    def priority(self) -> int:
        # 异常规则优先级最高
        return 10
    
    async def match(self, ctx: HookContext) -> bool:
        """检查是否有异常发生。"""
        # ERROR 触发点本身就表示有异常
        if ctx.hook_point == HookPoint.ERROR:
            return True
        
        # 其他触发点检查是否有错误记录
        if ctx.error is not None:
            return True
        
        if ctx.error_message:
            return True
        
        # 检查历史中的错误
        for item in reversed(ctx.history):
            if item.get("event") == "error":
                return True
        
        return False
    
    async def get_action(self, ctx: HookContext) -> Action:
        """返回回滚动作。"""
        error_info = self._get_error(ctx)
        error_step = error_info.get("step", ctx.current_step)
        error_node = error_info.get("node_name", ctx.current_node)
        
        return Action(
            type=ActionType.ROLLBACK,
            target_step=error_step,
            target_node=error_node,
            metadata={
                "error_message": ctx.error_message or str(ctx.error) if ctx.error else "",
                "error_step": error_step,
                "error_node": error_node,
            }
        )
