# Checkpoint 保存与恢复机制

## 概述

Checkpoint（检查点）机制用于在工作流执行过程中保存完整状态，支持从任意断点恢复执行。这对于长时间运行的任务、故障恢复和调试场景非常有用。

## 核心组件

### 1. CheckpointData 数据结构

```python
@dataclass
class CheckpointData:
    checkpoint_id: str      # 唯一标识符
    workflow_id: str        # 工作流 ID
    timestamp: str          # 保存时间戳
    step: int               # 执行步数
    node_name: str          # 刚执行完的节点名称
    next_node: str          # 下一个要执行的节点名称
    state: Dict[str, Any]   # 完整的工作流状态
    metadata: Dict[str, Any]  # 额外元数据
```

### 2. CheckpointManager 管理器

负责 checkpoint 的保存、加载和列表管理。

## 存储结构

```
store/checkpoints/
├── test_workflow_id_abc123/        # workflow_id 对应的目录
│   ├── 0_coordinator.json          # 步数_节点名.json
│   ├── 1_planner.json
│   ├── 2_publisher.json
│   ├── 3_XiaomiStockAnalyst.json
│   └── ...
└── another_workflow_id/
    └── ...
```

## Checkpoint 文件示例

```json
{
  "checkpoint_id": "test:abc123_7_StockAnalysisExpert_1710123456",
  "workflow_id": "test:abc123",
  "timestamp": "2025-03-11T15:33:55.185700",
  "step": 7,
  "node_name": "StockAnalysisExpert",
  "next_node": "publisher",
  "state": {
    "user_id": "test",
    "TEAM_MEMBERS": ["researcher", "coder", "reporter"],
    "messages": [...],
    "full_plan": "{...}",
    "next": "reporter",
    "workflow_mode": "launch",
    ...
  },
  "metadata": {}
}
```

## API 说明

### 保存 Checkpoint

```python
checkpoint_manager = CheckpointManager()

checkpoint_id = checkpoint_manager.save_checkpoint(
    workflow_id="test:abc123",      # 工作流 ID
    step=7,                         # 当前步数
    node_name="StockAnalysisExpert", # 刚执行完的节点
    next_node="publisher",          # 下一个要执行的节点
    state=current_state,            # 当前完整状态
    metadata={}                     # 可选元数据
)
```

### 加载 Checkpoint

```python
# 加载最新的 checkpoint
checkpoint = checkpoint_manager.load_checkpoint("test:abc123")

# 加载指定步数的 checkpoint
checkpoint = checkpoint_manager.load_checkpoint("test:abc123", step=5)

# 获取恢复所需信息
next_node = checkpoint.next_node    # 下一个要执行的节点
state = checkpoint.state            # 完整状态
step_count = checkpoint.step + 1    # 继续计数的步数
```

### 列出所有 Checkpoints

```python
checkpoints = checkpoint_manager.list_checkpoints("test:abc123")
# 返回: [
#   {"checkpoint_id": "...", "step": 0, "node_name": "coordinator", "timestamp": "..."},
#   {"checkpoint_id": "...", "step": 1, "node_name": "planner", "timestamp": "..."},
#   ...
# ]
```

## 恢复流程

### 1. CLI 命令

```bash
# 列出所有 checkpoints 并选择恢复
python cli.py resume -w test:abc123

# 直接恢复到指定步数
python cli.py resume -w test:abc123 -s 7
```

### 2. 恢复逻辑 (process.py)

```python
async def _process_workflow(workflow, initial_state, resume_step=None):
    # 1. 初始化默认值
    current_node = workflow.start_node
    state = State(**initial_state)
    step_count = 0

    # 2. 恢复逻辑：覆盖默认值
    if resume_step is not None:
        checkpoint = checkpoint_manager.load_checkpoint(workflow_id, step=resume_step)
        current_node = checkpoint.next_node   # 恢复执行位置
        state = State(**checkpoint.state)     # 恢复完整状态
        step_count = checkpoint.step + 1      # 恢复步数

    # 3. 继续执行
    while current_node != "__end__":
        # 执行节点...
        # 保存新 checkpoint...
```

### 3. 完整流程图

```
┌─────────────────────────────────────────────────────────────┐
│                    CLI: resume 命令                          │
├─────────────────────────────────────────────────────────────┤
│  1. list_checkpoints() → 展示可用断点                        │
│  2. 用户选择 step                                           │
│  3. load_checkpoint() → 加载状态（确认用）                   │
│  4. 调用 server._run_agent_workflow_with_resume()           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 Server: 服务层转发                           │
├─────────────────────────────────────────────────────────────┤
│  run_agent_workflow(..., resume_step=step)                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 Process: 执行层恢复                          │
├─────────────────────────────────────────────────────────────┤
│  1. 初始化 current_node, state, step_count                  │
│  2. load_checkpoint() → 覆盖恢复值                          │
│  3. while 循环继续执行                                      │
│  4. 每步执行后 save_checkpoint()                            │
└─────────────────────────────────────────────────────────────┘
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

1. **故障恢复**：工作流执行中断后从断点继续
2. **调试**：从特定步骤重新执行，观察行为
3. **长时间任务**：分批执行，避免一次性完成
4. **生产环境重跑**：使用 production 模式恢复已保存的工作流

## 注意事项

1. **恢复时 workmode 应为 production**：避免重新执行规划节点
2. **checkpoint 覆盖**：同一步数的新执行会覆盖旧 checkpoint
3. **状态一致性**：恢复后状态完全来自 checkpoint，不受初始参数影响
4. **文件系统依赖**：checkpoint 存储在本地文件系统，需确保存储空间
