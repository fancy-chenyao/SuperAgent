import json
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List, Union
from dataclasses import dataclass, field, asdict

from config.global_variables import checkpoints_dir

logger = logging.getLogger(__name__)

@dataclass
class CheckpointData:
    """Data structure for storing complete workflow state."""
    checkpoint_id: str
    workflow_id: str
    task_id: str  # Unique task execution instance ID
    timestamp: str
    step: int
    node_name: str
    next_node: Optional[str]
    state: Dict[str, Any]  # The State dict
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CheckpointData':
        # Backward compatibility: task_id may not exist in old checkpoints
        if 'task_id' not in data:
            data['task_id'] = data.get('workflow_id', '')
        return cls(**data)

class CheckpointManager:
    """
    Main checkpoint manager for SuperAgent workflows.
    Uses task_id to distinguish individual task executions from workflow templates.
    A workflow_id identifies the workflow template/type, while task_id is unique
    per execution run (workflow_id + timestamp).
    """
    
    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            self.base_dir = checkpoints_dir
        else:
            self.base_dir = Path(base_dir)
            
        if not self.base_dir.exists():
            self.base_dir.mkdir(parents=True, exist_ok=True)
            
        logger.info(f"CheckpointManager initialized at {self.base_dir}")

    @staticmethod
    def generate_task_id(workflow_id: str) -> str:
        """Generate a unique task execution ID based on workflow_id and current timestamp."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_wf = "".join([c if c.isalnum() or c in ('-', '_') else '_' for c in workflow_id])
        return f"{safe_wf}__{ts}"

    def _get_task_dir(self, task_id: str) -> Path:
        """Get or create the checkpoint directory for a specific task execution."""
        task_dir = self.base_dir / task_id
        if not task_dir.exists():
            task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    def _get_workflow_dir(self, workflow_id: str) -> Path:
        """Legacy: get directory by workflow_id (for backward compatibility)."""
        safe_id = "".join([c if c.isalnum() or c in ('-', '_') else '_' for c in workflow_id])
        workflow_dir = self.base_dir / safe_id
        if not workflow_dir.exists():
            workflow_dir.mkdir(parents=True, exist_ok=True)
        return workflow_dir

    def _json_serializer(self, obj: Any) -> Any:
        """Helper to serialize objects that are not JSON serializable by default."""
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        return str(obj)

    def save_checkpoint(
        self, 
        workflow_id: str, 
        task_id: str,
        step: int, 
        node_name: str, 
        next_node: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Save a checkpoint of the current workflow state.
        
        Args:
            workflow_id: The ID of the workflow template
            task_id: The unique task execution ID (generated per run)
            step: The current step number
            node_name: The name of the node just executed
            next_node: The next node to execute
            state: The current state dictionary
            metadata: Additional metadata
            
        Returns:
            str: The checkpoint ID
        """
        try:
            timestamp = datetime.now().isoformat()
            checkpoint_id = f"{task_id}_{step}_{node_name}_{int(datetime.now().timestamp())}"
            
            # Create checkpoint data
            checkpoint_data = CheckpointData(
                checkpoint_id=checkpoint_id,
                workflow_id=workflow_id,
                task_id=task_id,
                timestamp=timestamp,
                step=step,
                node_name=node_name,
                next_node=next_node,
                state=state,
                metadata=metadata or {}
            )
            
            # Save to file under task-specific directory
            task_dir = self._get_task_dir(task_id)
            checkpoint_file = task_dir / f"{step}_{node_name}.json"
            
            with open(checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(checkpoint_data.to_dict(), f, indent=2, ensure_ascii=False, default=self._json_serializer)
                
            logger.info(f"Saved checkpoint: {checkpoint_file}")
            return checkpoint_id
            
        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            raise

    def load_checkpoint(self, workflow_id: str = None, step: Optional[int] = None, node_name: Optional[str] = None, task_id: Optional[str] = None) -> CheckpointData:
        """
        Load a checkpoint. 
        - If task_id is provided, loads from that task's directory.
        - Otherwise falls back to workflow_id-based lookup (legacy).
        - If step not provided, loads the latest.
        """
        try:
            if task_id:
                search_dir = self._get_task_dir(task_id)
            else:
                search_dir = self._get_workflow_dir(workflow_id)
            
            if step is not None:
                # Find specific checkpoint
                pattern = f"{step}_*.json"
                files = list(search_dir.glob(pattern))
                if not files:
                    raise FileNotFoundError(f"No checkpoint found for step {step}")
                checkpoint_file = files[0]
            else:
                # Find latest checkpoint
                files = list(search_dir.glob("*.json"))
                if not files:
                    raise FileNotFoundError(f"No checkpoints found for task {task_id or workflow_id}")
                
                # Sort by step number (extracted from filename)
                def get_step(f):
                    try:
                        return int(f.name.split('_')[0])
                    except:
                        return -1
                
                checkpoint_file = max(files, key=get_step)
                
            with open(checkpoint_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            return CheckpointData.from_dict(data)
            
        except Exception as e:
            logger.error(f"Failed to load checkpoint: {e}")
            raise

    def list_checkpoints(self, workflow_id: str = None, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all checkpoints for a task or workflow."""
        try:
            if task_id:
                search_dir = self._get_task_dir(task_id)
            else:
                search_dir = self._get_workflow_dir(workflow_id)
            files = list(search_dir.glob("*.json"))
            
            checkpoints = []
            for f in files:
                try:
                    with open(f, 'r', encoding='utf-8') as file:
                        data = json.load(file)
                        checkpoints.append({
                            "checkpoint_id": data.get("checkpoint_id"),
                            "task_id": data.get("task_id", data.get("workflow_id", "")),
                            "step": data.get("step"),
                            "node_name": data.get("node_name"),
                            "next_node": data.get("next_node"),
                            "timestamp": data.get("timestamp")
                        })
                except:
                    continue
                    
            return sorted(checkpoints, key=lambda x: x["step"])
            
        except Exception as e:
            logger.error(f"Failed to list checkpoints: {e}")
            return []

    def list_tasks(self, workflow_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all task execution instances.
        If workflow_id is provided, filter tasks belonging to that workflow.
        Returns a list of task metadata sorted by creation time (newest first).
        """
        tasks = []
        try:
            if not self.base_dir.exists():
                return []
            for task_dir in self.base_dir.iterdir():
                if not task_dir.is_dir():
                    continue
                # Try to read the first (step 0) checkpoint to get metadata
                files = sorted(task_dir.glob("*.json"), key=lambda f: int(f.name.split('_')[0]) if f.name.split('_')[0].isdigit() else -1)
                if not files:
                    continue
                try:
                    with open(files[0], 'r', encoding='utf-8') as fh:
                        data = json.load(fh)
                    tid = data.get("task_id", task_dir.name)
                    wid = data.get("workflow_id", "")
                    # Filter by workflow_id if specified
                    if workflow_id and wid != workflow_id:
                        continue
                    # Get latest checkpoint step
                    latest_step = max(int(f.name.split('_')[0]) for f in files if f.name.split('_')[0].isdigit())
                    tasks.append({
                        "task_id": tid,
                        "workflow_id": wid,
                        "dir_name": task_dir.name,
                        "checkpoint_count": len(files),
                        "latest_step": latest_step,
                        "created_at": data.get("timestamp", ""),
                        "user_query": (data.get("state") or {}).get("USER_QUERY", ""),
                    })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Failed to list tasks: {e}")
        # Sort newest first
        tasks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return tasks
