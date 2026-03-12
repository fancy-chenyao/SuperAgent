# 任务执行追踪系统

## 概述

任务执行追踪系统（Task Execution Tracking System）用于记录、追踪和恢复 SuperAgent 工作流的执行过程。该系统引入了 `task_id` 作为任务执行实例的唯一标识，实现了完整的日志记录、检查点保存和断点恢复功能。

## 核心概念

### workflow_id 与 task_id 的区别

| 概念 | 说明 | 示例 |
|------|------|------|
| `workflow_id` | 工作流模板/类型的标识符，格式为 `user_id:polish_id` | `test:abc123def456` |
| `task_id` | 单次任务执行实例的唯一标识，由 workflow_id + 时间戳生成 | `test_abc123def456__20250312_143025` |

**关键区别**：
- 一个 `workflow_id` 可以对应多次执行（多个 `task_id`）
- `task_id` 用于区分同一次工作流的不同执行实例
- 这使得系统能够追踪同一工作流的多次运行历史

### task_id 生成规则

```python
# 格式: {workflow_id_safe}__{YYYYMMDD_HHMMSS}
task_id = CheckpointManager.generate_task_id(workflow_id)
# 示例: test_abc123__20250312_143025
```

## 核心组件

### 1. TaskLogger（任务日志记录器）

TaskLogger 记录每个任务执行的完整交互历史，日志格式参考 Agents_Failure_Attribution 数据集设计，支持事后故障归因和步骤级回滚。

#### 数据结构

```python
{
    "task_id": "test_abc123__20250312_143025",
    "workflow_id": "test:abc123",
    "user_query": "分析小米股票走势",
    "created_at": "2025-03-12T14:30:25.123456",
    "finished_at": "2025-03-12T14:45:30.789012",
    "status": "completed",  # running | completed | failed
    "history": [
        {
            "step": 0,
            "node_name": "coordinator",
            "role": "coordinator",
            "event": "start_of_agent",
            "content": "Agent coordinator started",
            "timestamp": "2025-03-12T14:30:26.000000"
        },
        ...
    ],
    "error": null  # 失败时包含错误信息
}
```

#### 事件类型

| 事件类型 | 说明 |
|----------|------|
| `workflow_start` | 工作流开始执行 |
| `start_of_agent` | Agent 节点开始执行 |
| `message` | Agent 输出的消息内容 |
| `end_of_agent` | Agent 节点执行完成 |
| `workflow_end` | 工作流执行完成 |
| `error` | 执行过程中发生错误 |

#### 主要方法

```python
# 初始化日志记录器
task_logger = TaskLogger(
    task_id="test_abc123__20250312_143025",
    workflow_id="test:abc123",
    user_query="分析小米股票走势"
)

# 记录工作流开始
task_logger.log_workflow_start(user_query="...")

# 记录 Agent 开始执行（支持子代理名称）
task_logger.log_agent_start(
    node_name="agent_proxy",
    step=5,
    sub_agent_name="researcher"  # 可选，显示为 agent_proxy【researcher】
)

# 记录 Agent 输出消息
task_logger.log_message(
    node_name="planner",
    content="正在制定执行计划...",
    step=2
)

# 记录 Agent 执行完成
task_logger.log_agent_end(
    node_name="agent_proxy",
    next_node="publisher",
    step=5,
    sub_agent_name="researcher"
)

# 记录工作流完成
task_logger.log_workflow_end()

# 记录错误
task_logger.log_error(
    error="API rate limit exceeded",
    node_name="researcher",
    step=7
)

# 加载已有日志
task_log = TaskLogger.load(task_id="test_abc123__20250312_143025")

# 列出所有任务
tasks = TaskLogger.list_tasks(workflow_id="test:abc123")
```

### 2. CheckpointManager（检查点管理器）

CheckpointManager 负责工作流状态的保存与恢复，使用 `task_id` 区分不同任务执行实例的检查点。

#### 数据结构

```python
@dataclass
class CheckpointData:
    checkpoint_id: str      # 唯一标识符
    workflow_id: str        # 工作流模板 ID
    task_id: str            # 任务执行实例 ID
    timestamp: str          # 保存时间戳
    step: int               # 执行步数
    node_name: str          # 刚执行完的节点名称
    next_node: str          # 下一个要执行的节点名称
    state: Dict[str, Any]   # 完整的工作流状态
    metadata: Dict[str, Any]  # 额外元数据
```

#### 主要方法

```python
checkpoint_manager = CheckpointManager()

# 生成 task_id
task_id = CheckpointManager.generate_task_id(workflow_id)

# 保存检查点
checkpoint_id = checkpoint_manager.save_checkpoint(
    workflow_id="test:abc123",
    task_id="test_abc123__20250312_143025",
    step=7,
    node_name="StockAnalysisExpert",
    next_node="publisher",
    state=current_state,
    metadata={}
)

# 加载检查点（支持按 task_id 或 workflow_id）
# 加载最新的 checkpoint
checkpoint = checkpoint_manager.load_checkpoint(task_id=task_id)

# 加载指定步数的 checkpoint
checkpoint = checkpoint_manager.load_checkpoint(task_id=task_id, step=5)

# 列出任务的所有检查点
checkpoints = checkpoint_manager.list_checkpoints(task_id=task_id)

# 列出所有任务执行实例
tasks = checkpoint_manager.list_tasks(workflow_id="test:abc123")
```

## 存储结构

```
store/
├── checkpoints/                           # 检查点存储目录
│   ├── test_abc123__20250312_143025/      # task_id 对应的目录
│   │   ├── 0_coordinator.json             # 步数_节点名.json
│   │   ├── 1_planner.json
│   │   ├── 2_researcher.json
│   │   └── ...
│   └── test_abc123__20250312_150000/      # 同一 workflow 的另一次执行
│       └── ...
│
└── task_logs/                             # 任务日志存储目录
    ├── test_abc123__20250312_143025.json   # task_id.json
    ├── test_abc123__20250312_150000.json
    └── ...
```

### Checkpoint 文件示例

```json
{
  "checkpoint_id": "test_abc123__20250312_143025_7_StockAnalysisExpert_1710123456",
  "workflow_id": "test:abc123",
  "task_id": "test_abc123__20250312_143025",
  "timestamp": "2025-03-12T14:45:30.185700",
  "step": 7,
  "node_name": "StockAnalysisExpert",
  "next_node": "publisher",
  "state": {
    "user_id": "test",
    "TEAM_MEMBERS": ["agent_factory", "researcher", "coder", "reporter"],
    "messages": [...],
    "full_plan": "{...}",
    "next": "reporter",
    "workflow_mode": "launch",
    ...
  },
  "metadata": {}
}
```

### Log 文件示例

```json
{
  "task_id": "test_abc123__20250312_143025",
  "workflow_id": "test:abc123",
  "user_query": "查询今天的天气，并给出运动建议。",
  "created_at": "2025-03-12T14:30:25.123456",
  "finished_at": "2025-03-12T14:45:30.789012",
  "status": "completed",
  "history": [
    {
      "step": 0,
      "node_name": "system",
      "role": "system",
      "event": "workflow_start",
      "content": "Workflow started. Query: 查询今天的天气，并给出运动建议。",
      "timestamp": "2025-03-12T14:30:25.123456"
    },
    {
      "step": 0,
      "node_name": "coordinator",
      "role": "coordinator",
      "event": "start_of_agent",
      "content": "Agent coordinator started",
      "timestamp": "2025-03-12T14:30:26.000000"
    },
    {
      "step": 0,
      "node_name": "coordinator",
      "role": "coordinator",
      "event": "end_of_agent",
      "content": "Agent coordinator finished -> planner",
      "timestamp": "2025-03-12T14:30:30.500000",
      "next_node": "planner"
    },
    {
      "step": 1,
      "node_name": "planner",
      "role": "planner",
      "event": "start_of_agent",
      "content": "Agent planner started",
      "timestamp": "2025-03-12T14:30:31.000000"
    },
    {
      "step": 1,
      "node_name": "planner",
      "role": "planner",
      "event": "message",
      "content": "{ \"thought\": \"制定执行计划...\", \"steps\": [...] }",
      "timestamp": "2025-03-12T14:30:45.000000"
    },
    {
      "step": 1,
      "node_name": "planner",
      "role": "planner",
      "event": "end_of_agent",
      "content": "Agent planner finished -> publisher",
      "timestamp": "2025-03-12T14:30:46.000000",
      "next_node": "publisher"
    },
    {
      "step": 3,
      "node_name": "agent_proxy",
      "role": "agent_proxy",
      "event": "start_of_agent",
      "content": "Agent agent_proxy【researcher】 started",
      "timestamp": "2025-03-12T14:31:00.000000",
      "sub_agent_name": "researcher"
    }
  ],
  "error": null
}
```

## API 接口

### 任务列表

```
GET /api/tasks?workflow_id={workflow_id}
```

返回所有任务执行实例列表，可选按 workflow_id 过滤。

**响应示例**：
```json
[
  {
    "task_id": "test_abc123__20250312_143025",
    "workflow_id": "test:abc123",
    "user_query": "分析小米股票走势",
    "created_at": "2025-03-12T14:30:25",
    "finished_at": "2025-03-12T14:45:30",
    "status": "completed",
    "step_count": 12,
    "error": null
  }
]
```

### 任务日志

```
GET /api/tasks/{task_id}/log
```

返回指定任务的完整结构化日志。

### 检查点列表

```
GET /api/tasks/{task_id}/checkpoints
```

返回指定任务的所有检查点列表。

**响应示例**：
```json
[
  {
    "checkpoint_id": "...",
    "task_id": "test_abc123__20250312_143025",
    "step": 0,
    "node_name": "coordinator",
    "next_node": "planner",
    "timestamp": "2025-03-12T14:30:26"
  },
  ...
]
```

### 检查点详情

```
GET /api/tasks/{task_id}/checkpoints/{step}
```

返回指定步数检查点的完整数据（包含完整 state）。

### 断点恢复

```
POST /api/tasks/resume
```

从指定检查点恢复任务执行，以 SSE 流形式返回执行事件。

**请求体**：
```json
{
  "task_id": "test_abc123__20250312_143025",
  "resume_step": 5,
  "workflow_id": "test:abc123",
  "user_id": "test",
  "task_type": "agent_workflow",
  "workmode": "launch",
  "debug": false,
  "deep_thinking_mode": true,
  "search_before_planning": false,
  "coor_agents": null
}
```

## 前端界面

### Task History 页面

Web UI 中的 "Task History" 标签页提供了完整的任务管理界面：

#### 任务列表

- 显示所有任务执行实例
- 展示任务状态（completed/failed/running）
- 显示用户查询摘要、创建时间、步数
- 支持按 workflow_id 过滤

#### Checkpoints 面板

- 显示选中任务的所有检查点
- 每个检查点显示：步数、节点名、下一节点、时间戳
- 支持展开查看完整 JSON 数据
- 提供 "Resume from here" 快速恢复按钮

#### Log 面板

- 显示任务的完整执行日志
- 每条记录显示：步数、角色、事件类型、时间戳
- 支持展开/折叠查看详细内容
- 显示格式：`agent_proxy【researcher】` 形式展示子代理

#### Resume 面板

- 配置恢复参数：task_id、workflow_id、resume_step、user_id
- 实时显示恢复执行的输出流
- 支持停止恢复执行

## 恢复流程

> **注意**：目前恢复功能仅支持从命令行（CLI）执行，前端页面（Task History）的恢复功能尚未完成测试，可能存在功能缺陷。

### 完整流程图

```
┌─────────────────────────────────────────────────────────────┐
│                    前端: Task History 页面                    │
├─────────────────────────────────────────────────────────────┤
│  1. GET /api/tasks → 展示任务列表                            │
│  2. 选择任务 → 加载 checkpoints 和 log                       │
│  3. 选择恢复步数 → POST /api/tasks/resume                   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 Web App: API 处理层                          │
├─────────────────────────────────────────────────────────────┤
│  1. 从 task_log 加载原始用户消息                             │
│  2. 构建 AgentRequest                                       │
│  3. 调用 server._run_agent_workflow_with_resume()           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 Process: 执行层恢复                          │
├─────────────────────────────────────────────────────────────┤
│  1. 初始化 current_node, state, step_count                  │
│  2. load_checkpoint(task_id, step) → 覆盖恢复值             │
│  3. 初始化 TaskLogger（继续记录日志）                        │
│  4. while 循环继续执行                                      │
│  5. 每步执行后 save_checkpoint() 和 log_event()             │
└─────────────────────────────────────────────────────────────┘
```

### 恢复逻辑详解

```python
async def _process_workflow(workflow, initial_state, resume_step=None, task_id=None):
    # 1. 初始化默认值
    current_node = workflow.start_node
    state = State(**initial_state)
    step_count = 0
    
    # 2. 初始化 TaskLogger
    task_logger = TaskLogger(task_id=task_id, workflow_id=workflow_id, user_query=user_query)
    
    # 3. 恢复逻辑：加载检查点覆盖默认值
    should_resume = resume_step is not None or initial_state.get("workflow_mode") in ["polish", "production"]
    
    if should_resume:
        try:
            checkpoint = checkpoint_manager.load_checkpoint(task_id=task_id, step=resume_step)
            if checkpoint:
                current_node = checkpoint.next_node   # 恢复执行位置
                state = State(**checkpoint.state)     # 恢复完整状态
                step_count = checkpoint.step + 1      # 恢复步数计数
        except Exception as e:
            logger.warning(f"Could not load checkpoint, starting from scratch: {e}")
    
    task_logger.log_workflow_start(user_query=user_query)
    
    # 4. 继续执行工作流
    while current_node != "__end__":
        # 执行节点...
        task_logger.log_agent_start(...)
        # ...
        checkpoint_manager.save_checkpoint(...)
        task_logger.log_agent_end(...)
        step_count += 1
```

## 状态字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `messages` | List[Dict] | 完整的消息历史 |
| `TEAM_MEMBERS` | List[str] | 可用的代理列表 |
| `TEAM_MEMBERS_DESCRIPTION` | str | 代理描述 |
| `full_plan` | str | 完整的执行计划 (JSON) |
| `next` | str | 当前计划的下一步代理 |
| `user_id` | str | 用户 ID |
| `workflow_id` | str | 工作流 ID |
| `workflow_mode` | str | 工作模式 (launch/production/polish) |
| `deep_thinking_mode` | bool | 深度思考模式 |
| `search_before_planning` | bool | 规划前搜索 |
| `initialized` | bool | 是否已初始化 |

## 使用场景

### 1. 故障恢复

当工作流执行中断（如网络错误、API 限流、系统崩溃）时，可以从断点继续执行：

```python
# 通过 API 恢复
POST /api/tasks/resume
{
  "task_id": "test_abc123__20250312_143025",
  "resume_step": 7,
  ...
}
```

### 2. 调试与诊断

查看任务执行日志，诊断问题原因：

```
GET /api/tasks/{task_id}/log
```

### 3. 步骤回滚

从特定步骤重新执行，测试不同参数：

```
GET /api/tasks/{task_id}/checkpoints
POST /api/tasks/resume { "resume_step": 5, ... }
```

### 4. 长时间任务

分批执行长时间任务，避免一次性完成：

```python
# 执行一段时间后中断
# 下次从 checkpoint 继续
```

### 5. 生产模式执行

在 `production` 模式下，系统会自动尝试从最新 checkpoint 恢复执行：

```python
# workmode = "production" 时自动尝试加载最新 checkpoint
# 注意：需要提供正确的 task_id 才能定位到具体的 checkpoint
```

**注意**：production 模式的恢复功能需要配合 `task_id` 使用，目前主要用于工作流的增量执行场景。

## 最佳实践

### 1. 日志记录

- 每个关键操作都应记录日志事件
- 使用 `sub_agent_name` 参数区分代理代理的子代理
- 错误时务必调用 `log_error()` 记录详细信息

### 2. 检查点保存

- 每个节点执行完成后保存检查点
- 检查点包含完整状态，确保可恢复性
- 避免在检查点中保存大量临时数据

### 3. 恢复执行

- 恢复时建议使用 `workmode: "launch"` 或 `"production"`
- 确认 `task_id` 和 `resume_step` 正确对应
- 恢复执行会继续追加日志和检查点

### 4. 存储管理

- 定期清理过期的任务日志和检查点
- 监控存储空间使用情况
- 重要任务结果应单独备份

## 注意事项

1. **task_id 唯一性**：每次执行生成唯一的 task_id，不会重复
2. **检查点覆盖**：同一步数的新执行会覆盖旧检查点
3. **状态一致性**：恢复后状态完全来自检查点，不受初始参数影响
4. **文件系统依赖**：检查点和日志存储在本地文件系统，需确保存储空间
5. **向后兼容**：旧版 checkpoint 文件（无 task_id）会自动使用 workflow_id 作为 task_id

## 相关文件

- `src/robust/checkpoint.py` - CheckpointManager 实现
- `src/robust/task_logger.py` - TaskLogger 实现
- `src/robust/CHECKPOINT.md` - 原检查点机制说明
- `src/workflow/process.py` - 工作流执行与恢复逻辑
- `src/service/web_app.py` - REST API 实现
- `web/app.js` - 前端 Task History 页面逻辑
- `web/index.html` - 前端页面结构
