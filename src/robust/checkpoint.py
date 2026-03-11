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
        return cls(**data)

class CheckpointManager:
    """
    Main checkpoint manager for SuperAgent workflows.
    Inspired by AG2's independent checkpoint system.
    """
    
    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            self.base_dir = checkpoints_dir
        else:
            self.base_dir = Path(base_dir)
            
        if not self.base_dir.exists():
            self.base_dir.mkdir(parents=True, exist_ok=True)
            
        logger.info(f"CheckpointManager initialized at {self.base_dir}")

    def _get_workflow_dir(self, workflow_id: str) -> Path:
        # Sanitize workflow_id for filesystem
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
        step: int, 
        node_name: str, 
        next_node: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Save a checkpoint of the current workflow state.
        
        Args:
            workflow_id: The ID of the workflow execution
            step: The current step number
            node_name: The name of the node just executed
            state: The current state dictionary
            metadata: Additional metadata
            
        Returns:
            str: The checkpoint ID
        """
        try:
            timestamp = datetime.now().isoformat()
            checkpoint_id = f"{workflow_id}_{step}_{node_name}_{int(datetime.now().timestamp())}"
            
            # Create checkpoint data
            checkpoint_data = CheckpointData(
                checkpoint_id=checkpoint_id,
                workflow_id=workflow_id,
                timestamp=timestamp,
                step=step,
                node_name=node_name,
                next_node=next_node,
                state=state,
                metadata=metadata or {}
            )
            
            # Save to file
            workflow_dir = self._get_workflow_dir(workflow_id)
            checkpoint_file = workflow_dir / f"{step}_{node_name}.json"
            
            with open(checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(checkpoint_data.to_dict(), f, indent=2, ensure_ascii=False, default=self._json_serializer)
                
            logger.info(f"Saved checkpoint: {checkpoint_file}")
            return checkpoint_id
            
        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            raise

    def load_checkpoint(self, workflow_id: str, step: Optional[int] = None, node_name: Optional[str] = None) -> CheckpointData:
        """
        Load a checkpoint. If step/node_name not provided, loads the latest.
        """
        try:
            workflow_dir = self._get_workflow_dir(workflow_id)
            
            if step is not None:
                # Find specific checkpoint
                pattern = f"{step}_*.json"
                files = list(workflow_dir.glob(pattern))
                if not files:
                    raise FileNotFoundError(f"No checkpoint found for step {step}")
                checkpoint_file = files[0] # Assuming step is unique enough or take first
            else:
                # Find latest checkpoint
                files = list(workflow_dir.glob("*.json"))
                if not files:
                    raise FileNotFoundError(f"No checkpoints found for workflow {workflow_id}")
                
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

    def list_checkpoints(self, workflow_id: str) -> List[Dict[str, Any]]:
        """List all checkpoints for a workflow."""
        try:
            workflow_dir = self._get_workflow_dir(workflow_id)
            files = list(workflow_dir.glob("*.json"))
            
            checkpoints = []
            for f in files:
                try:
                    with open(f, 'r', encoding='utf-8') as file:
                        # lightweight read - maybe just parse filename or read partial?
                        # For now, full read is safer
                        data = json.load(file)
                        checkpoints.append({
                            "checkpoint_id": data.get("checkpoint_id"),
                            "step": data.get("step"),
                            "node_name": data.get("node_name"),
                            "timestamp": data.get("timestamp")
                        })
                except:
                    continue
                    
            return sorted(checkpoints, key=lambda x: x["step"])
            
        except Exception as e:
            logger.error(f"Failed to list checkpoints: {e}")
            return []
