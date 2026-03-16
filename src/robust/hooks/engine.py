"""
Hook Engine - Core execution engine for the hook system.
"""

import logging
from typing import List, Optional

from src.robust.hooks.base import (
    Action,
    ActionType,
    Handler,
    HookContext,
    HookPoint,
    HookResult,
    Rule,
)
from src.robust.hooks.registry import HandlerRegistry, RuleRegistry

logger = logging.getLogger(__name__)


class HookEngine:
    """
    钩子引擎：负责在工作流执行过程中匹配规则并调用处理器。
    
    工作流程：
    1. 接收 HookContext
    2. 根据 hook_point 筛选匹配的规则
    3. 按优先级顺序检查规则是否匹配
    4. 如果匹配，获取 Action 并找到对应的 Handler
    5. 执行 Handler 并返回结果
    """
    
    def __init__(
        self,
        rule_registry: Optional[RuleRegistry] = None,
        handler_registry: Optional[HandlerRegistry] = None,
    ) -> None:
        self.rule_registry = rule_registry or RuleRegistry.get_instance()
        self.handler_registry = handler_registry or HandlerRegistry.get_instance()
    
    async def process(self, ctx: HookContext) -> HookResult:
        """
        处理钩子上下文，返回处理结果。
        
        Args:
            ctx: 钩子上下文
            
        Returns:
            HookResult: 处理结果
        """
        # 获取当前触发点的所有规则
        rules = self.rule_registry.get_by_trigger_point(ctx.hook_point)
        
        # 按优先级排序
        rules.sort(key=lambda r: r.priority)
        
        logger.debug(
            f"HookEngine processing at {ctx.hook_point.value}, "
            f"found {len(rules)} rules"
        )
        
        # 遍历规则，找到第一个匹配的
        for rule in rules:
            try:
                is_match = await rule.match(ctx)
                if is_match:
                    logger.info(f"Rule matched: {rule.name}")
                    action = await rule.get_action(ctx)
                    return await self._execute_handler(ctx, action, rule)
            except Exception as e:
                logger.error(f"Error in rule {rule.name}: {e}")
                continue
        
        # 没有匹配的规则，返回继续执行
        return HookResult(should_continue=True, message="No rules matched")
    
    async def _execute_handler(
        self,
        ctx: HookContext,
        action: Action,
        rule: Rule,
    ) -> HookResult:
        """执行匹配的处理器。"""
        handlers = self.handler_registry.get_by_action(action.type)
        
        if not handlers:
            logger.warning(
                f"No handler found for action type: {action.type}, "
                f"rule: {rule.name}"
            )
            return HookResult(
                should_continue=True,
                message=f"No handler for {action.type.value}",
            )
        
        # 使用第一个支持的处理器
        handler = handlers[0]
        logger.info(f"Executing handler: {handler.name} for action: {action.type}")
        
        try:
            result = await handler.handle(ctx, action)
            logger.info(f"Handler {handler.name} completed: {result.message}")
            return result
        except Exception as e:
            logger.error(f"Handler {handler.name} failed: {e}")
            return HookResult(
                should_continue=True,
                message=f"Handler failed: {str(e)}",
            )
    
    async def trigger(
        self,
        hook_point: HookPoint,
        task_id: str,
        workflow_id: str,
        **kwargs,
    ) -> HookResult:
        """
        触发钩子的便捷方法。
        
        Args:
            hook_point: 钩子触发点
            task_id: 任务 ID
            workflow_id: 工作流 ID
            **kwargs: 其他上下文参数
            
        Returns:
            HookResult: 处理结果
        """
        ctx = HookContext(
            task_id=task_id,
            workflow_id=workflow_id,
            hook_point=hook_point,
            **kwargs,
        )
        return await self.process(ctx)
