"""
Incomplete Task Rule - 任务未完成规则

触发条件：工作流结束时状态不是 completed，但没有显式异常
触发点：WORKFLOW_END
动作：INTERVENE
"""

from typing import List

from src.robust.hooks.base import Action, ActionType, HookContext, HookPoint
from src.robust.hooks.rules.base import BaseRule


class IncompleteTaskRule(BaseRule):
    """任务未完成规则：检测任务未正常完成的情况。"""
    
    @property
    def name(self) -> str:
        return "incomplete_task_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.WORKFLOW_END]
    
    @property
    def priority(self) -> int:
        return 20
    
    async def match(self, ctx: HookContext) -> bool:
        """
        检查任务是否未完成但没有显式异常。
        
        匹配条件：
        1. 工作流结束
        2. 状态不是 completed
        3. 没有显式的错误事件
        """
        # 检查是否有 workflow_end 事件
        has_workflow_end = False
        is_completed = False
        has_error = False
        
        for item in ctx.history:
            if item.get("event") == "workflow_end":
                has_workflow_end = True
                content = item.get("content", "")
                is_completed = "completed successfully" in content.lower()
            if item.get("event") == "error":
                has_error = True
        
        # 匹配：有 workflow_end，但不是 completed，且没有显式错误
        if has_workflow_end and not is_completed and not has_error:
            return True
        
        return False
    
    async def get_action(self, ctx: HookContext) -> Action:
        """返回干预动作。"""
        # 找到最后的执行步骤
        last_step = 0
        last_node = None
        for item in reversed(ctx.history):
            if item.get("event") == "end_of_agent":
                last_step = item.get("step", 0)
                last_node = item.get("node_name")
                break
        
        return Action(
            type=ActionType.INTERVENE,
            target_step=last_step,
            target_node=last_node,
            intervention_text="检测到任务未正常完成。请检查执行过程，分析原因并尝试继续完成任务。",
            metadata={
                "reason": "incomplete_task",
                "last_step": last_step,
                "last_node": last_node,
            }
        )
