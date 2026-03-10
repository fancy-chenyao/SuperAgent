from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Generic, TypeVar
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ExecutionStatus(str, Enum):
    """执行状态枚举"""
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PENDING = "pending"


@dataclass
class ExecuteResult:
    """执行结果统一返回格式"""
    status: ExecutionStatus
    """执行状态"""
    result: Any = None
    """执行结果数据"""
    error: Optional[str] = None
    """错误信息"""
    metadata: Dict[str, Any] = field(default_factory=dict)
    """执行元数据（耗时、token使用等）"""
    
    @property
    def is_success(self) -> bool:
        """是否执行成功"""
        return self.status == ExecutionStatus.SUCCESS
    
    @property
    def is_failed(self) -> bool:
        """是否执行失败"""
        return self.status == ExecutionStatus.FAILED


@dataclass
class ToolCall:
    """工具调用记录"""
    tool_name: str
    arguments: Dict[str, Any]
    result: Any = None
    error: Optional[str] = None
    start_time: float = 0.0
    end_time: float = 0.0
    
    @property
    def duration(self) -> float:
        """调用耗时（秒）"""
        return self.end_time - self.start_time


@dataclass
class ExecutionContext:
    """执行上下文"""
    user_id: str
    """用户ID"""
    workflow_id: Optional[str] = None
    """工作流ID"""
    workflow_mode: Optional[str] = None
    """工作流模式"""
    deep_thinking_mode: bool = False
    """深度思考模式"""
    debug: bool = False
    """调试模式"""
    metadata: Dict[str, Any] = field(default_factory=dict)
    """额外的上下文元数据"""


class AgentExecutor(ABC):
    """Agent执行器抽象基类
    
    定义Agent执行的接口规范：
    - execute(): 执行Agent
    - load_tools(): 加载Agent可用的工具
    - validate(): 验证Agent配置
    
    使用示例：
        executor = ExecutorFactory.get_executor(agent)
        result = await executor.execute(
            agent=agent,
            messages=[...],
            context=ExecutionContext(user_id="user1")
        )
    """
    
    def __init__(self):
        self._initialized = False
    
    @abstractmethod
    async def execute(
        self,
        agent: Any,
        messages: List[Any],
        context: ExecutionContext
    ) -> ExecuteResult:
        """执行Agent
        
        Args:
            agent: Agent实例
            messages: 消息列表
            context: 执行上下文
            
        Returns:
            ExecuteResult: 执行结果
        """
        pass
    
    @abstractmethod
    async def load_tools(self, agent: Any) -> List[Any]:
        """加载Agent可用的工具
        
        Args:
            agent: Agent实例
            
        Returns:
            List[Any]: 工具实例列表
        """
        pass
    
    async def validate(self, agent: Any) -> bool:
        """验证Agent配置是否有效
        
        Args:
            agent: Agent实例
            
        Returns:
            bool: 配置是否有效
        """
        required_fields = ['agent_name', 'llm_type', 'prompt']
        for field in required_fields:
            if not hasattr(agent, field) or not getattr(agent, field):
                logger.warning(f"Agent validation failed: missing required field '{field}'")
                return False
        return True
    
    async def prepare(self, agent: Any):
        """准备执行（可选实现）
        
        在每次执行前调用，可用于初始化资源等
        """
        pass
    
    async def cleanup(self):
        """清理资源（可选实现）
        
        在执行完成后调用，可用于释放资源等
        """
        pass
    
    @property
    def initialized(self) -> bool:
        """是否已初始化"""
        return self._initialized
    
    async def initialize(self):
        """初始化执行器"""
        if not self._initialized:
            await self._do_initialize()
            self._initialized = True
    
    async def _do_initialize(self):
        """子类实现的初始化逻辑"""
        pass
