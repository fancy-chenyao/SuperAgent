"""
Base types and interfaces for the hook system.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ActionType(Enum):
    """动作类型枚举。"""
    CONTINUE = "continue"           # 继续执行，无需干预
    INTERVENE = "intervene"         # 干预：注入修正指令
    ROLLBACK = "rollback"           # 回滚到指定步骤重试
    ABORT = "abort"                 # 中止工作流
    VALIDATE = "validate"           # 请求验证校验
    PREVENT = "prevent"             # 主动预防


class HookPoint(Enum):
    """钩子触发点。"""
    NODE_START = "node_start"       # 节点开始前
    NODE_END = "node_end"           # 节点结束后
    WORKFLOW_END = "workflow_end"   # 工作流结束
    ERROR = "error"                 # 异常发生时


@dataclass
class Action:
    """规则匹配后返回的动作。"""
    type: ActionType
    target_step: Optional[int] = None       # 回滚目标步骤
    target_node: Optional[str] = None       # 目标节点
    intervention_text: Optional[str] = None # 干预文本
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HookContext:
    """钩子执行上下文。"""
    task_id: str
    workflow_id: str
    current_node: Optional[str] = None
    current_step: int = 0
    state: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[Exception] = None
    error_message: Optional[str] = None
    hook_point: HookPoint = HookPoint.NODE_END
    workflow_status: str = "running"  # "running", "completed", "failed"
    user_query: str = ""
    
    # 用于节点间传递信息
    last_message: Optional[str] = None
    last_agent: Optional[str] = None


@dataclass
class HookResult:
    """钩子处理结果。"""
    should_continue: bool = True            # 是否继续执行工作流
    modified_state: Optional[Dict[str, Any]] = None  # 修改后的状态
    resume_step: Optional[int] = None       # 恢复执行的步骤
    message: str = ""                       # 结果消息
    metadata: Dict[str, Any] = field(default_factory=dict)


class Rule(ABC):
    """规则基类。"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """规则名称。"""
        pass
    
    @property
    @abstractmethod
    def trigger_points(self) -> List[HookPoint]:
        """触发点列表。"""
        pass
    
    @property
    def priority(self) -> int:
        """优先级，数值越小优先级越高。"""
        return 100
    
    @abstractmethod
    async def match(self, ctx: HookContext) -> bool:
        """判断是否匹配规则。"""
        pass
    
    @abstractmethod
    async def get_action(self, ctx: HookContext) -> Action:
        """返回匹配后的动作。"""
        pass


class Handler(ABC):
    """处理器基类。"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """处理器名称。"""
        pass
    
    @property
    @abstractmethod
    def supported_actions(self) -> List[ActionType]:
        """支持的动作类型。"""
        pass
    
    @abstractmethod
    async def handle(self, ctx: HookContext, action: Action) -> HookResult:
        """处理动作。"""
        pass
