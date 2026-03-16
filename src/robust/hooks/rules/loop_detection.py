"""
Loop Detection Rule - 循环检测规则（骨架）

触发条件：检测到工作流陷入循环
触发点：NODE_END
动作：INTERVENE
"""

from typing import List

from src.robust.hooks.base import Action, ActionType, HookContext, HookPoint
from src.robust.hooks.rules.base import BaseRule


class LoopDetectionRule(BaseRule):
    """
    循环检测规则：检测工作流是否陷入循环。
    
    TODO: 具体实现
    """
    
    # 最大重复次数
    MAX_REPETITIONS = 3
    
    @property
    def name(self) -> str:
        return "loop_detection_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.NODE_END]
    
    @property
    def priority(self) -> int:
        return 40
    
    async def match(self, ctx: HookContext) -> bool:
        """
        检查是否陷入循环。
        
        TODO: 实现具体逻辑
        """
        # 骨架代码：暂不匹配
        return False
    
    async def get_action(self, ctx: HookContext) -> Action:
        """返回干预动作。"""
        return Action(
            type=ActionType.INTERVENE,
            intervention_text="检测到可能的执行循环，请尝试不同的策略。",
            metadata={
                "reason": "loop_detected",
            }
        )
