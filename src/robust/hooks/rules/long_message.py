"""
Long Message Rule - 长消息规则（骨架）

触发条件：检测到消息内容过长
触发点：NODE_END
动作：PREVENT
"""

from typing import List

from src.robust.hooks.base import Action, ActionType, HookContext, HookPoint
from src.robust.hooks.rules.base import BaseRule


class LongMessageRule(BaseRule):
    """
    长消息规则：检测消息内容过长，触发主动预防。
    
    TODO: 具体实现
    """
    
    # 消息长度阈值
    MAX_MESSAGE_LENGTH = 10000
    
    @property
    def name(self) -> str:
        return "long_message_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.NODE_END]
    
    @property
    def priority(self) -> int:
        return 50
    
    async def match(self, ctx: HookContext) -> bool:
        """
        检查消息是否过长。
        
        TODO: 实现具体逻辑
        """
        # 骨架代码：暂不匹配
        return False
    
    async def get_action(self, ctx: HookContext) -> Action:
        """返回预防动作。"""
        return Action(
            type=ActionType.PREVENT,
            intervention_text="检测到消息内容过长，建议进行简化或分段处理。",
            metadata={
                "reason": "long_message",
            }
        )
