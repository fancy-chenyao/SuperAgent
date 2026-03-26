# 远程Agent架构重构说明

## 架构概述

新架构采用模块化设计，每个Agent独立成文件，支持通用的多工具调用能力。

## 目录结构

```
remote_agents/
├── __init__.py                    # 包初始化
├── base_agent.py                  # 基类 BaseRemoteAgent
├── factory.py                     # Agent工厂
├── hr_assistant_agent.py          # HR助理Agent（多工具）
├── knowledge_agent.py             # 知识查询Agent（单工具）
├── document_generator_agent.py    # 文档生成Agent（单工具）
└── report_agent.py                # 报告生成Agent（单工具）
```

## 核心设计

### 1. BaseRemoteAgent 基类

所有Agent继承自 `BaseRemoteAgent`，提供:
- `execute()` 抽象方法 - 子类实现具体执行逻辑
- `call_tool()` 通用方法 - 调用单个工具

```python
class BaseRemoteAgent(ABC):
    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """子类实现具体逻辑"""
        pass

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout: int = 10
    ) -> Any:
        """调用单个工具"""
        pass
```

### 2. AgentFactory 工厂模式

统一管理所有Agent实例:
- `register_agent()` - 注册Agent
- `get_agent()` - 获取Agent实例
- `initialize_all()` - 初始化所有Agent

### 3. 多工具Agent示例: RemoteHRAssistantAgent

支持同时调用多个工具并智能合并结果:

```python
class RemoteHRAssistantAgent(BaseRemoteAgent):
    async def execute(self, tools, messages, context, parameter_extractor):
        # 1. 分离工具
        person_tool = ...
        salary_tool = ...

        # 2. 调用person_info_tool
        person_result = await self.call_tool(...)

        # 3. 调用salary_info_tool（使用person结果中的employee_id）
        salary_result = await self.call_tool(...)

        # 4. 合并结果
        merged = self._merge_person_and_salary(person_result, salary_result)
        return merged
```

**关键特性:**
- 自动从person查询结果中提取employee_id
- 将employee_id传递给salary查询
- 按employee_id合并两个结果集
- 返回完整的员工信息（基本信息+工资信息）

### 4. 单工具Agent示例

其他Agent（Knowledge, Document, Report）只需调用单个工具:

```python
class RemoteKnowledgeAgent(BaseRemoteAgent):
    async def execute(self, tools, messages, context, parameter_extractor):
        tool = tools[0]
        arguments = await parameter_extractor.extract(...)
        result = await self.call_tool(tool_name, arguments, timeout=60)
        return result
```

## 使用方式

### 1. 添加新Agent

创建新文件 `remote_agents/my_new_agent.py`:

```python
from .base_agent import BaseRemoteAgent

class MyNewAgent(BaseRemoteAgent):
    def __init__(self):
        super().__init__(
            name="MyNewAgent",
            prompt="Agent description"
        )

    async def execute(self, tools, messages, context, parameter_extractor):
        # 实现你的逻辑
        pass
```

在 `factory.py` 中注册:

```python
from .my_new_agent import MyNewAgent

class AgentFactory:
    @classmethod
    def initialize_all(cls):
        cls.register_agent(MyNewAgent())
        # ... 其他Agent
```

### 2. 多工具Agent模式

如果你的Agent需要调用多个工具:

1. 在 `execute()` 中遍历 `tools` 列表
2. 为每个工具提取参数
3. 调用 `self.call_tool()`
4. 合并结果

参考 `RemoteHRAssistantAgent` 的实现。

### 3. 工具间数据传递

如果后续工具需要前面工具的结果:

```python
# 第一个工具
result1 = await self.call_tool("tool1", params1)

# 从result1中提取数据
ids = [item["id"] for item in result1]

# 传递给第二个工具
params2 = {"id_list": ids}
result2 = await self.call_tool("tool2", params2)
```

## 优势

1. **模块化**: 每个Agent独立文件，易于维护
2. **可扩展**: 添加新Agent只需创建新文件并注册
3. **通用性**: 基类提供通用工具调用能力
4. **灵活性**: 支持单工具和多工具两种模式
5. **智能合并**: 多工具Agent可以智能处理结果合并

## 迁移指南

从旧架构迁移到新架构:

1. 旧代码在 `mock_remote_agent.py` 的 `/agent` endpoint 中只调用 `tools[0]`
2. 新代码使用 `AgentFactory.get_agent()` 获取Agent实例
3. Agent内部处理所有工具调用和结果合并
4. 返回统一格式的结果

## 测试

启动服务:

```bash
# 1. 启动工具服务器
python mock_remote_tool_skill.py

# 2. 启动远程Agent服务器（新架构）
python mock_remote_agent.py

# 3. 启动主服务
python cli.py web --host 0.0.0.0 --port 8001
```

测试多工具调用:

```
用户: "查询张三的完整信息包括工资"
系统: RemoteHRAssistantAgent 会:
  1. 调用 remote_person_info_tool 查询基本信息
  2. 调用 remote_salary_info_tool 查询工资信息
  3. 合并两个结果返回完整数据
```

## 注意事项

1. 所有Agent必须在 `factory.py` 中注册
2. Agent名称必须与 `mock_remote_registry.json` 中的定义一致
3. 多工具Agent需要自行处理工具调用顺序和数据传递
4. 工具超时时间可以在 `call_tool()` 中指定
