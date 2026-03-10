# CoorAgent 技能系统指南

## 概述

CoorAgent 技能系统允许开发者和用户创建、管理和使用各种技能，以扩展系统的功能。技能是一种可重用的功能模块，可以被 Agent 调用执行特定任务。

## 技能系统架构

技能系统由以下组件组成：

1. **Skill 基类**：定义了技能的基本结构和接口
2. **SkillsManager**：负责技能的加载、注册和执行
3. **AgentManager**：集成了技能系统，允许 Agent 使用技能

## 创建自定义技能

要创建自定义技能，您需要：

1. 创建一个继承自 `Skill` 类的新类
2. 实现必要的属性和方法
3. 将技能文件放在 `src/skills/` 目录下

### 技能示例

以下是一个简单的问候技能示例：

```python
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
        """执行问候技能"""
        name = kwargs.get("name")
        language = kwargs.get("language", "zh")
        
        if language == "en":
            message = f"Hello, {name}! How can I help you today?"
        else:
            message = f"你好，{name}！今天我能为你做些什么？"
        
        return {"message": message}
```

## 使用技能

### 执行技能

要执行技能，您可以使用 `AgentManager` 的 `execute_skill` 方法：

```python
from src.manager.agents import agent_manager

# 执行问候技能
result = await agent_manager.execute_skill(
    "greeting",
    name="张三",
    language="zh"
)
print(result)  # 输出: {"message": "你好，张三！今天我能为你做些什么？"}

# 执行计算器技能
result = await agent_manager.execute_skill(
    "calculator",
    operation="add",
    num1=10,
    num2=20
)
print(result)  # 输出: {"result": 30}
```

### 列出可用技能

要列出所有可用技能，您可以使用 `AgentManager` 的 `list_skills` 方法：

```python
skills = agent_manager.list_skills()
for skill in skills:
    print(f"{skill.name}: {skill.description}")
```

### 按类别列出技能

要按类别列出技能，您可以使用 `AgentManager` 的 `list_skills_by_category` 方法：

```python
from src.skills.skill import SkillCategory

general_skills = agent_manager.list_skills_by_category(SkillCategory.GENERAL)
for skill in general_skills:
    print(f"{skill.name}: {skill.description}")
```

## 技能类别

技能可以分为以下类别：

- **GENERAL**：通用技能，如问候、信息查询等
- **TOOL**：工具类技能，如计算器、文件处理等
- **API**：API 集成技能，如调用外部 API
- **INTEGRATION**：系统集成技能，如与其他系统的集成

## 技能输入输出定义

每个技能都需要定义输入参数和输出参数：

### 输入参数

输入参数使用 `SkillInput` 类定义，包括：

- `name`：参数名称
- `description`：参数描述
- `type`：参数类型
- `required`：是否必填
- `default`：默认值

### 输出参数

输出参数使用 `SkillOutput` 类定义，包括：

- `name`：参数名称
- `description`：参数描述
- `type`：参数类型

## 技能配置

技能可以通过 `config` 属性设置配置参数，这些参数可以在技能执行时使用。

## 示例技能

系统内置了以下示例技能：

1. **greeting**：问候技能，向用户发送问候消息
2. **calculator**：计算器技能，执行基本的数学计算

## 最佳实践

- 技能名称应简洁明了，避免使用空格和特殊字符
- 技能描述应清晰准确，说明技能的功能和用途
- 输入参数应合理设置，避免过多的必填参数
- 技能执行方法应处理异常情况，确保技能的稳定性
- 技能应具有良好的可测试性，便于单元测试
