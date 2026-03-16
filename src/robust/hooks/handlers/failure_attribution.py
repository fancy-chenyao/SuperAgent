"""
Failure Attribution Handler - 故障归因处理器

处理动作：ROLLBACK, INTERVENE
"""

import copy
import logging
from typing import List

from src.robust.hooks.base import Action, ActionType, HookContext, HookResult
from src.robust.hooks.handlers.base import BaseHandler
from src.robust.failure_attributor import FailureAttributor
from src.robust.rollback_controller import RollbackController
from src.robust.correction_injector import CorrectionInjector
from src.robust.task_logger import TaskLogger

logger = logging.getLogger(__name__)


class FailureAttributionHandler(BaseHandler):
    """
    故障归因处理器：执行故障归因、回滚和纠错注入。
    """
    
    @property
    def name(self) -> str:
        return "failure_attribution_handler"
    
    @property
    def supported_actions(self) -> List[ActionType]:
        return [ActionType.ROLLBACK, ActionType.INTERVENE]
    
    async def handle(self, ctx: HookContext, action: Action) -> HookResult:
        """
        处理故障归因和恢复。
        
        流程：
        1. 加载任务日志
        2. 执行故障归因
        3. 找到回滚点
        4. 生成纠错指令
        5. 返回修改后的状态
        """
        # 获取 LLM 客户端
        llm_client = self._get_llm_client(ctx)
        
        # 执行故障归因
        attributor = FailureAttributor(llm_client=llm_client)
        attribution = await attributor.attribute(ctx.task_id)
        
        if not attribution:
            logger.warning(f"Failed to get attribution for task {ctx.task_id}")
            return HookResult(
                should_continue=True,
                message="Attribution failed",
            )
        
        # 检查是否需要恢复
        if attribution.is_succeed:
            logger.info(f"Task {ctx.task_id} succeeded, no recovery needed")
            return HookResult(
                should_continue=True,
                message="Task succeeded",
            )
        
        # 获取检查点管理器
        checkpoint_manager = self._get_checkpoint_manager(ctx)
        if not checkpoint_manager:
            logger.warning("No checkpoint manager available")
            return HookResult(
                should_continue=True,
                message="No checkpoint manager",
            )
        
        # 确定回滚目标步骤
        target_step = action.target_step
        if target_step is None and attribution.mistake_step is not None:
            target_step = attribution.mistake_step
        
        if target_step is None:
            logger.warning("No target step for rollback")
            return HookResult(
                should_continue=True,
                message="No target step",
            )
        
        # 查找回滚点
        rollback_ctrl = RollbackController(checkpoint_manager)
        rollback_target = rollback_ctrl.find_rollback_point(
            task_id=ctx.task_id,
            target_step=target_step,
            workflow_id=ctx.workflow_id,
        )
        
        if not rollback_target:
            logger.warning(f"Failed to find rollback point for step {target_step}")
            return HookResult(
                should_continue=True,
                message="Rollback point not found",
            )
        
        # 加载任务日志
        task_log = TaskLogger.load(ctx.task_id)
        task_log_dict = task_log.to_dict() if task_log else {}
        
        # 生成纠错指令
        injector = CorrectionInjector(llm_client=llm_client)
        injection_result = await injector.apply(
            task_id=ctx.task_id,
            attribution=attribution,
            rollback_target=rollback_target,
            task_log=task_log_dict,
        )
        
        # 保存修补后的检查点
        rollback_ctrl.save_patched_checkpoint(
            rollback_target,
            injection_result.patched_state,
            ctx.task_id,
        )
        
        logger.info(
            f"Recovery prepared: rollback to step {rollback_target.rollback_step}, "
            f"target node: {injection_result.target_node}"
        )
        
        return HookResult(
            should_continue=True,
            modified_state=injection_result.patched_state,
            resume_step=rollback_target.rollback_step,
            message=f"Recovery prepared: {injection_result.injection_text[:100]}...",
            metadata={
                "attribution": attribution.__dict__,
                "rollback_step": rollback_target.rollback_step,
                "target_node": injection_result.target_node,
            }
        )
