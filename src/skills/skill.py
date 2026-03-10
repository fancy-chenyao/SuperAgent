from pydantic import BaseModel, ConfigDict
from typing import Dict, Any, Optional, List
from enum import Enum, unique


@unique
class SkillCategory(str, Enum):
    """技能类别枚举"""
    GENERAL = "general"
    TOOL = "tool"
    API = "api"
    INTEGRATION = "integration"


class SkillInput(BaseModel):
    """技能输入参数"""
    name: str
    description: str
    type: str
    required: bool = True
    default: Optional[Any] = None


class SkillOutput(BaseModel):
    """技能输出参数"""
    name: str
    description: str
    type: str


class Skill(BaseModel):
    """技能基类"""
    model_config = ConfigDict(extra="allow")
    
    # 技能基本信息
    name: str
    """技能名称"""
    display_name: str
    """技能显示名称"""
    description: str
    """技能描述"""
    category: SkillCategory
    """技能类别"""
    version: str
    """技能版本"""
    
    # 技能输入输出定义
    inputs: List[SkillInput]
    """技能输入参数列表"""
    outputs: List[SkillOutput]
    """技能输出参数列表"""
    
    # 技能配置
    config: Dict[str, Any] = {}
    """技能配置参数"""
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """执行技能的异步方法
        
        Args:
            **kwargs: 技能输入参数
            
        Returns:
            Dict[str, Any]: 技能执行结果
        """
        raise NotImplementedError("Subclasses must implement execute method")
    
    def validate_input(self, **kwargs) -> bool:
        """验证输入参数
        
        Args:
            **kwargs: 技能输入参数
            
        Returns:
            bool: 验证是否通过
        """
        required_inputs = [input.name for input in self.inputs if input.required]
        for required_input in required_inputs:
            if required_input not in kwargs:
                return False
        return True