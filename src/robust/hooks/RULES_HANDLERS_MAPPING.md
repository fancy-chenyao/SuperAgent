# Rules & Handlers 映射关系

本文档说明 Hook 系统中规则（Rule）与处理器（Handler）的对应关系。

## 核心机制

规则和处理器通过 **ActionType** 建立松耦合的对应关系：

```
Rule.get_action() → Action(type=ActionType.XXX) → Handler.supported_actions 包含该 ActionType
```

### ActionType 枚举

| ActionType | 含义 | 触发场景 |
|------------|------|----------|
| `CONTINUE` | 继续执行 | 无需干预，正常流程 |
| `INTERVENE` | 干预 | 注入修正指令继续执行 |
| `ROLLBACK` | 回滚 | 回滚到指定步骤重试 |
| `ABORT` | 中止 | 终止工作流 |
| `VALIDATE` | 验证 | 请求结果验证校验 |
| `PREVENT` | 预防 | 主动预防潜在问题 |

## 映射表

| Rule | 优先级 | 触发点 | 返回 ActionType | Handler |
|------|--------|--------|-----------------|---------|
| `ExceptionRule` | 10 | ERROR | `ROLLBACK` | `FailureAttributionHandler` |
| `IncompleteTaskRule` | 20 | WORKFLOW_END | `INTERVENE` | `FailureAttributionHandler` |
| `OutputValidationRule` | 30 | WORKFLOW_END | `INTERVENE` | `FailureAttributionHandler` |
| `LoopDetectionRule` | 40 | NODE_END | `ABORT` | *(暂无)* |
| `LongMessageRule` | 50 | NODE_END | `PREVENT` | `PreventionHandler` |

## Handler 支持的动作

| Handler | supported_actions |
|---------|-------------------|
| `FailureAttributionHandler` | `ROLLBACK`, `INTERVENE` |
| `ValidationHandler` | `VALIDATE` |
| `PreventionHandler` | `PREVENT` |

## 执行流程

```
HookContext (上下文)
       ↓
   HookEngine.process()
       ↓
① 按 hook_point 筛选规则
   RuleRegistry.get_by_trigger_point(ctx.hook_point)
       ↓
② 按优先级排序
   rules.sort(key=lambda r: r.priority)
       ↓
③ 遍历匹配
   rule.match(ctx) → True/False
       ↓
④ 获取动作
   rule.get_action(ctx) → Action(type=ActionType.XXX)
       ↓
⑤ 查找处理器
   HandlerRegistry.get_by_action(action.type)
       ↓
⑥ 执行处理
   handler.handle(ctx, action) → HookResult
```

## 扩展指南

### 新增 Rule

1. 继承 `BaseRule` 或 `Rule`
2. 实现 `name`, `trigger_points`, `priority`, `match()`, `get_action()`
3. 在 `setup.py` 中注册

```python
class MyRule(BaseRule):
    @property
    def name(self) -> str:
        return "my_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.NODE_END]
    
    @property
    def priority(self) -> int:
        return 25  # 在 IncompleteTaskRule 和 OutputValidationRule 之间
    
    async def match(self, ctx: HookContext) -> bool:
        # 匹配逻辑
        return True
    
    async def get_action(self, ctx: HookContext) -> Action:
        return Action(type=ActionType.INTERVENE, ...)
```

### 新增 Handler

1. 继承 `BaseHandler` 或 `Handler`
2. 实现 `name`, `supported_actions`, `handle()`
3. 在 `setup.py` 中注册

```python
class MyHandler(BaseHandler):
    @property
    def name(self) -> str:
        return "my_handler"
    
    @property
    def supported_actions(self) -> List[ActionType]:
        return [ActionType.INTERVENE, ActionType.PREVENT]
    
    async def handle(self, ctx: HookContext, action: Action) -> HookResult:
        # 处理逻辑
        return HookResult(should_continue=True, ...)
```

## 文件结构

```
src/robust/hooks/
├── __init__.py          # 模块导出
├── base.py              # 基础类型定义
├── engine.py            # 钩子引擎
├── registry.py          # 注册中心
├── setup.py             # 初始化和默认注册
├── rules/               # 规则目录
│   ├── __init__.py
│   ├── base.py
│   ├── exception.py         # 异常规则 (priority 10)
│   ├── incomplete_task.py   # 未完成任务规则 (priority 20)
│   ├── output_validation.py # 输出验证规则 (priority 30)
│   ├── loop_detection.py    # 循环检测规则 (priority 40)
│   └── long_message.py      # 长消息规则 (priority 50)
└── handlers/            # 处理器目录
    ├── __init__.py
    ├── base.py
    ├── failure_attribution.py  # 故障归因处理器
    ├── validation.py           # 验证处理器
    └── prevention.py           # 预防处理器
```
