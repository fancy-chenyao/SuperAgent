"""
Output Validation Rule - 输出验证规则

触发条件：任务完成但输出结果不符合预期
触发点：WORKFLOW_END
动作：INTERVENE
"""

from typing import List

from src.robust.hooks.base import Action, ActionType, HookContext, HookPoint
from src.robust.hooks.rules.base import BaseRule


class OutputValidationRule(BaseRule):
    """
    输出验证规则：检测任务完成但输出不符合预期的情况。
    
    验证逻辑（启发式）：
    1. 检查最终输出是否包含有效内容
    2. 检查是否有明确的结论或结果
    3. 检查输出是否过短（可能是不完整）
    """
    
    @property
    def name(self) -> str:
        return "output_validation_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.WORKFLOW_END]
    
    @property
    def priority(self) -> int:
        return 30
    
    async def match(self, ctx: HookContext) -> bool:
        """
        检查输出是否有效。
        
        匹配条件（任一）：
        1. 任务标记为完成但没有有效输出
        2. 输出过短（< 50 字符）
        3. 输出包含失败标志
        """
        # 检查是否完成
        is_completed = False
        for item in reversed(ctx.history):
            if item.get("event") == "workflow_end":
                content = item.get("content", "")
                is_completed = "completed successfully" in content.lower()
                break
        
        if not is_completed:
            return False
        
        # 获取最终输出（reporter 的消息）
        final_output = ""
        for item in reversed(ctx.history):
            if item.get("node_name") == "agent_proxy":
                sub_agent = item.get("sub_agent_name", "")
                if sub_agent == "reporter" and item.get("event") == "message":
                    final_output = item.get("content", "")
                    break
        
        # 验证输出
        if not final_output:
            return True  # 没有输出
        
        if len(final_output) < 50:
            return True  # 输出过短
        
        # 检查失败标志
        failure_indicators = [
            "无法完成",
            "failed",
            "error",
            "未能",
            "失败",
            "抱歉",
            "无法提供",
        ]
        final_output_lower = final_output.lower()
        for indicator in failure_indicators:
            if indicator in final_output_lower:
                return True
        
        return False
    
    async def get_action(self, ctx: HookContext) -> Action:
        """返回干预动作。"""
        return Action(
            type=ActionType.INTERVENE,
            intervention_text=(
                "检测到任务输出可能不符合预期。"
                "请重新评估输出质量，确保完整回答用户问题。"
                "如果确实无法完成，请明确说明原因。"
            ),
            metadata={
                "reason": "output_validation_failed",
            }
        )
