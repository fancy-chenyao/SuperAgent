from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.robust.checkpoint import CheckpointData, CheckpointManager


@dataclass
class RollbackTarget:
    checkpoint: CheckpointData
    rollback_step: int
    original_mistake_step: int
    rollback_reason: str


class RollbackController:
    def __init__(self, checkpoint_manager: CheckpointManager) -> None:
        """初始化回退控制器。"""
        self.checkpoint_manager = checkpoint_manager

    def find_rollback_point(
        self,
        task_id: str,
        mistake_step: int,
        strategy: str = "before_mistake",
        workflow_id: Optional[str] = None,
    ) -> RollbackTarget:
        """定位回退点并返回 RollbackTarget。"""
        if mistake_step is None:
            raise ValueError("mistake_step is required")
        checkpoints = self.checkpoint_manager.list_checkpoints(workflow_id=workflow_id, task_id=task_id)
        steps = [c.get("step") for c in checkpoints if isinstance(c.get("step"), int)]
        if not steps:
            raise FileNotFoundError("No checkpoints found for rollback")
        if strategy == "at_mistake":
            candidate_steps = [s for s in steps if s <= mistake_step]
        else:
            candidate_steps = [s for s in steps if s < mistake_step]
        if not candidate_steps:
            candidate_steps = [min(steps)]
        rollback_step = max(candidate_steps)
        checkpoint = self.checkpoint_manager.load_checkpoint(task_id=task_id, step=rollback_step, workflow_id=workflow_id)
        return RollbackTarget(
            checkpoint=checkpoint,
            rollback_step=rollback_step,
            original_mistake_step=mistake_step,
            rollback_reason=strategy,
        )

    def load_rollback_state(self, rollback_target: RollbackTarget) -> Dict[str, Any]:
        """加载回退状态字典。"""
        return dict(rollback_target.checkpoint.state)

    def save_patched_checkpoint(
        self,
        rollback_target: RollbackTarget,
        patched_state: Dict[str, Any],
        task_id: str,
    ) -> CheckpointData:
        """保存纠错注入后的 patched checkpoint。"""
        checkpoint = rollback_target.checkpoint
        metadata = dict(checkpoint.metadata or {})
        metadata["patched_from"] = checkpoint.checkpoint_id
        metadata["rollback_reason"] = rollback_target.rollback_reason
        checkpoint_id = self.checkpoint_manager.save_checkpoint(
            workflow_id=checkpoint.workflow_id,
            task_id=task_id,
            step=checkpoint.step,
            node_name=checkpoint.node_name,
            next_node=checkpoint.next_node,
            state=patched_state,
            metadata=metadata,
        )
        return CheckpointData(
            checkpoint_id=checkpoint_id,
            workflow_id=checkpoint.workflow_id,
            task_id=task_id,
            timestamp=checkpoint.timestamp,
            step=checkpoint.step,
            node_name=checkpoint.node_name,
            next_node=checkpoint.next_node,
            state=patched_state,
            metadata=metadata,
        )
