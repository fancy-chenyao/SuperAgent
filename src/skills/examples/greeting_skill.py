from src.skills.skill import Skill, SkillCategory, SkillInput, SkillOutput
from typing import Dict, Any


class GreetingSkill(Skill):
    """简单的问候技能"""
    
    name = "greeting"
    display_name = "问候"
    description = "向用户发送问候消息"
    category = SkillCategory.GENERAL
    version = "1.0.0"
    
    inputs = [
        SkillInput(
            name="name",
            description="用户姓名",
            type="string",
            required=True
        ),
        SkillInput(
            name="language",
            description="语言（en/zh）",
            type="string",
            required=False,
            default="zh"
        )
    ]
    
    outputs = [
        SkillOutput(
            name="message",
            description="问候消息",
            type="string"
        )
    ]
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """执行问候技能
        
        Args:
            name: 用户姓名
            language: 语言（en/zh）
            
        Returns:
            Dict: 包含问候消息的结果
        """
        name = kwargs.get("name")
        language = kwargs.get("language", "zh")
        
        if language == "en":
            message = f"Hello, {name}! How can I help you today?"
        else:
            message = f"你好，{name}！今天我能为你做些什么？"
        
        return {"message": message}
