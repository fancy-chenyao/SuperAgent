from src.skills.skill import Skill, SkillCategory, SkillInput, SkillOutput
from typing import Dict, Any


class CalculatorSkill(Skill):
    """简单的计算器技能"""
    
    name = "calculator"
    display_name = "计算器"
    description = "执行基本的数学计算"
    category = SkillCategory.TOOL
    version = "1.0.0"
    
    inputs = [
        SkillInput(
            name="operation",
            description="操作类型（add/subtract/multiply/divide）",
            type="string",
            required=True
        ),
        SkillInput(
            name="num1",
            description="第一个数字",
            type="number",
            required=True
        ),
        SkillInput(
            name="num2",
            description="第二个数字",
            type="number",
            required=True
        )
    ]
    
    outputs = [
        SkillOutput(
            name="result",
            description="计算结果",
            type="number"
        )
    ]
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """执行计算技能
        
        Args:
            operation: 操作类型（add/subtract/multiply/divide）
            num1: 第一个数字
            num2: 第二个数字
            
        Returns:
            Dict: 包含计算结果的字典
        """
        operation = kwargs.get("operation")
        num1 = kwargs.get("num1")
        num2 = kwargs.get("num2")
        
        if operation == "add":
            result = num1 + num2
        elif operation == "subtract":
            result = num1 - num2
        elif operation == "multiply":
            result = num1 * num2
        elif operation == "divide":
            if num2 == 0:
                raise ValueError("除数不能为零")
            result = num1 / num2
        else:
            raise ValueError(f"不支持的操作类型: {operation}")
        
        return {"result": result}
