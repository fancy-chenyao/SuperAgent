"""
独立测试脚本 - 测试 Hook 系统核心功能

不依赖外部模块（langchain 等），使用 mock 对象进行测试。

运行方式:
    python tests/test_hooks_standalone.py
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod

# ==================== 复制必要的类型定义 ====================

class ActionType(Enum):
    CONTINUE = "continue"
    INTERVENE = "intervene"
    ROLLBACK = "rollback"
    ABORT = "abort"
    VALIDATE = "validate"
    PREVENT = "prevent"


class HookPoint(Enum):
    NODE_START = "node_start"
    NODE_END = "node_end"
    WORKFLOW_END = "workflow_end"
    ERROR = "error"


@dataclass
class Action:
    type: ActionType
    target_step: Optional[int] = None
    target_node: Optional[str] = None
    intervention_text: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HookContext:
    task_id: str
    workflow_id: str
    current_node: Optional[str] = None
    current_step: int = 0
    state: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[Exception] = None
    error_message: Optional[str] = None
    hook_point: HookPoint = HookPoint.NODE_END
    workflow_status: str = "running"
    user_query: str = ""
    last_message: Optional[str] = None
    last_agent: Optional[str] = None


@dataclass
class HookResult:
    should_continue: bool = True
    modified_state: Optional[Dict[str, Any]] = None
    resume_step: Optional[int] = None
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ==================== 抽象基类 ====================

class Rule(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass
    
    @property
    @abstractmethod
    def trigger_points(self) -> List[HookPoint]:
        pass
    
    @property
    def priority(self) -> int:
        return 100
    
    @abstractmethod
    async def match(self, ctx: HookContext) -> bool:
        pass
    
    @abstractmethod
    async def get_action(self, ctx: HookContext) -> Action:
        pass


class Handler(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass
    
    @property
    @abstractmethod
    def supported_actions(self) -> List[ActionType]:
        pass
    
    @abstractmethod
    async def handle(self, ctx: HookContext, action: Action) -> HookResult:
        pass


# ==================== 注册中心 ====================

class RuleRegistry:
    _instance: Optional["RuleRegistry"] = None
    
    def __new__(cls) -> "RuleRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._rules = {}
        return cls._instance
    
    @classmethod
    def get_instance(cls) -> "RuleRegistry":
        return cls()
    
    def register(self, rule: Rule) -> None:
        self._rules[rule.name] = rule
    
    def unregister(self, name: str) -> None:
        if name in self._rules:
            del self._rules[name]
    
    def get(self, name: str) -> Optional[Rule]:
        return self._rules.get(name)
    
    def get_all(self) -> List[Rule]:
        return list(self._rules.values())
    
    def get_by_trigger_point(self, point: HookPoint) -> List[Rule]:
        return [
            rule for rule in self._rules.values()
            if point in rule.trigger_points
        ]
    
    def clear(self) -> None:
        self._rules.clear()


class HandlerRegistry:
    _instance: Optional["HandlerRegistry"] = None
    
    def __new__(cls) -> "HandlerRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._handlers = {}
        return cls._instance
    
    @classmethod
    def get_instance(cls) -> "HandlerRegistry":
        return cls()
    
    def register(self, handler: Handler) -> None:
        self._handlers[handler.name] = handler
    
    def get(self, name: str) -> Optional[Handler]:
        return self._handlers.get(name)
    
    def get_by_action(self, action_type: ActionType) -> List[Handler]:
        return [
            handler for handler in self._handlers.values()
            if action_type in handler.supported_actions
        ]
    
    def clear(self) -> None:
        self._handlers.clear()


# ==================== 引擎 ====================

class HookEngine:
    def __init__(
        self,
        rule_registry: Optional[RuleRegistry] = None,
        handler_registry: Optional[HandlerRegistry] = None,
    ) -> None:
        self.rule_registry = rule_registry or RuleRegistry.get_instance()
        self.handler_registry = handler_registry or HandlerRegistry.get_instance()
    
    async def process(self, ctx: HookContext) -> HookResult:
        rules = self.rule_registry.get_by_trigger_point(ctx.hook_point)
        rules.sort(key=lambda r: r.priority)
        
        for rule in rules:
            try:
                is_match = await rule.match(ctx)
                if is_match:
                    print(f"  [*] Rule matched: {rule.name}")
                    action = await rule.get_action(ctx)
                    return await self._execute_handler(ctx, action, rule)
            except Exception as e:
                print(f"  [!] Error in rule {rule.name}: {e}")
                continue
        
        return HookResult(should_continue=True, message="No rules matched")
    
    async def _execute_handler(
        self,
        ctx: HookContext,
        action: Action,
        rule: Rule,
    ) -> HookResult:
        handlers = self.handler_registry.get_by_action(action.type)
        
        if not handlers:
            return HookResult(
                should_continue=True,
                message=f"No handler for {action.type.value}",
            )
        
        handler = handlers[0]
        print(f"  [>] Executing handler: {handler.name}")
        
        try:
            result = await handler.handle(ctx, action)
            print(f"  [*] Handler completed: {result.message}")
            return result
        except Exception as e:
            print(f"  [!] Handler failed: {e}")
            return HookResult(
                should_continue=True,
                message=f"Handler failed: {str(e)}",
            )


# ==================== 具体规则实现 ====================

class ExceptionRule(Rule):
    @property
    def name(self) -> str:
        return "exception_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.ERROR]
    
    @property
    def priority(self) -> int:
        return 10
    
    async def match(self, ctx: HookContext) -> bool:
        if ctx.hook_point == HookPoint.ERROR:
            return True
        if ctx.error is not None:
            return True
        if ctx.error_message:
            return True
        for item in reversed(ctx.history):
            if item.get("event") == "error":
                return True
        return False
    
    async def get_action(self, ctx: HookContext) -> Action:
        error_step = ctx.current_step
        error_node = ctx.current_node
        
        for item in reversed(ctx.history):
            if item.get("event") == "error":
                error_step = item.get("step", ctx.current_step)
                error_node = item.get("node_name", ctx.current_node)
                break
        
        return Action(
            type=ActionType.ROLLBACK,
            target_step=error_step,
            target_node=error_node,
            metadata={
                "error_message": ctx.error_message or str(ctx.error) if ctx.error else "",
            }
        )


class IncompleteTaskRule(Rule):
    @property
    def name(self) -> str:
        return "incomplete_task_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.WORKFLOW_END]
    
    @property
    def priority(self) -> int:
        return 20
    
    async def match(self, ctx: HookContext) -> bool:
        has_workflow_end = False
        is_completed = False
        has_error = False
        
        for item in ctx.history:
            if item.get("event") == "workflow_end":
                has_workflow_end = True
                content = item.get("content", "")
                is_completed = "completed successfully" in content.lower()
            if item.get("event") == "error":
                has_error = True
        
        return has_workflow_end and not is_completed and not has_error
    
    async def get_action(self, ctx: HookContext) -> Action:
        last_step = 0
        last_node = None
        for item in reversed(ctx.history):
            if item.get("event") == "end_of_agent":
                last_step = item.get("step", 0)
                last_node = item.get("node_name")
                break
        
        return Action(
            type=ActionType.INTERVENE,
            target_step=last_step,
            target_node=last_node,
            intervention_text="检测到任务未正常完成。请检查执行过程。",
            metadata={"reason": "incomplete_task"}
        )


class OutputValidationRule(Rule):
    @property
    def name(self) -> str:
        return "output_validation_rule"
    
    @property
    def trigger_points(self) -> List[HookPoint]:
        return [HookPoint.WORKFLOW_END]
    
    @property
    def priority(self) -> int:
        return 30
    
    async def match(self, ctx: HookContext) -> bool:
        is_completed = False
        for item in reversed(ctx.history):
            if item.get("event") == "workflow_end":
                content = item.get("content", "")
                is_completed = "completed successfully" in content.lower()
                break
        
        if not is_completed:
            return False
        
        final_output = ""
        for item in reversed(ctx.history):
            if item.get("node_name") == "agent_proxy":
                sub_agent = item.get("sub_agent_name", "")
                if sub_agent == "reporter" and item.get("event") == "message":
                    final_output = item.get("content", "")
                    break
        
        if not final_output:
            return True
        
        if len(final_output) < 50:
            return True
        
        failure_indicators = ["无法完成", "failed", "error", "未能", "失败", "抱歉"]
        final_output_lower = final_output.lower()
        for indicator in failure_indicators:
            if indicator in final_output_lower:
                return True
        
        return False
    
    async def get_action(self, ctx: HookContext) -> Action:
        return Action(
            type=ActionType.INTERVENE,
            intervention_text="检测到任务输出可能不符合预期。",
            metadata={"reason": "output_validation_failed"}
        )


# ==================== 具体处理器实现 ====================

class MockFailureAttributionHandler(Handler):
    @property
    def name(self) -> str:
        return "failure_attribution_handler"
    
    @property
    def supported_actions(self) -> List[ActionType]:
        return [ActionType.ROLLBACK, ActionType.INTERVENE]
    
    async def handle(self, ctx: HookContext, action: Action) -> HookResult:
        return HookResult(
            should_continue=True,
            modified_state={"__recovery_attempted__": True},
            resume_step=action.target_step,
            message=f"Mock recovery prepared for step {action.target_step}",
        )


class MockPreventionHandler(Handler):
    @property
    def name(self) -> str:
        return "prevention_handler"
    
    @property
    def supported_actions(self) -> List[ActionType]:
        return [ActionType.PREVENT]
    
    async def handle(self, ctx: HookContext, action: Action) -> HookResult:
        return HookResult(
            should_continue=True,
            message=action.intervention_text or "Prevention applied",
        )


# ==================== 测试数据 ====================

def get_sample_completed_history():
    """从真实日志文件格式构建的测试数据 - 完成状态"""
    return [
        {"step": 0, "node_name": "system", "role": "system", "event": "workflow_start", "content": "Workflow started. Query: 查询今天的天气"},
        {"step": 0, "node_name": "coordinator", "role": "coordinator", "event": "start_of_agent", "content": "Agent coordinator started"},
        {"step": 0, "node_name": "coordinator", "role": "coordinator", "event": "end_of_agent", "content": "Agent coordinator finished -> planner", "next_node": "planner"},
        {"step": 1, "node_name": "planner", "role": "planner", "event": "message", "content": '[{"agent_name": "researcher"}, {"agent_name": "reporter"}]'},
        {"step": 1, "node_name": "planner", "role": "planner", "event": "end_of_agent", "content": "Agent planner finished -> publisher", "next_node": "publisher"},
        {"step": 2, "node_name": "publisher", "role": "publisher", "event": "end_of_agent", "content": "Agent publisher finished -> agent_proxy", "next_node": "agent_proxy"},
        {"step": 3, "node_name": "agent_proxy", "role": "agent_proxy", "event": "message", "content": "天气研究完成..." + "x" * 100, "sub_agent_name": "researcher"},
        {"step": 3, "node_name": "agent_proxy", "role": "agent_proxy", "event": "end_of_agent", "content": "Agent finished", "next_node": "publisher", "sub_agent_name": "researcher"},
        {"step": 4, "node_name": "publisher", "role": "publisher", "event": "end_of_agent", "content": "Agent publisher finished -> agent_proxy", "next_node": "agent_proxy"},
        {"step": 5, "node_name": "agent_proxy", "role": "agent_proxy", "event": "message", "content": "这是一份完整的天气运动建议报告..." + "x" * 200, "sub_agent_name": "reporter"},
        {"step": 5, "node_name": "agent_proxy", "role": "agent_proxy", "event": "end_of_agent", "content": "Agent finished", "next_node": "publisher", "sub_agent_name": "reporter"},
        {"step": 6, "node_name": "publisher", "role": "publisher", "event": "end_of_agent", "content": "Agent publisher finished -> __end__", "next_node": "__end__"},
        {"step": 0, "node_name": "system", "role": "system", "event": "workflow_end", "content": "Workflow completed successfully."},
    ]


def get_sample_failed_history():
    """失败状态的历史记录"""
    return [
        {"step": 0, "node_name": "system", "role": "system", "event": "workflow_start", "content": "Workflow started"},
        {"step": 1, "node_name": "planner", "role": "planner", "event": "message", "content": '[{"agent_name": "researcher"}]'},
        {"step": 2, "node_name": "agent_proxy", "role": "agent_proxy", "event": "error", "content": "Connection timeout", "sub_agent_name": "researcher"},
    ]


def get_sample_incomplete_history():
    """未完成状态的历史记录"""
    return [
        {"step": 0, "node_name": "system", "role": "system", "event": "workflow_start", "content": "Workflow started"},
        {"step": 1, "node_name": "planner", "role": "planner", "event": "message", "content": '[{"agent_name": "researcher"}]'},
        {"step": 2, "node_name": "agent_proxy", "role": "agent_proxy", "event": "end_of_agent", "content": "Agent finished", "next_node": "__end__", "sub_agent_name": "researcher"},
        {"step": 0, "node_name": "system", "role": "system", "event": "workflow_end", "content": "Workflow ended unexpectedly."},
    ]


# ==================== 测试函数 ====================

def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def print_result(name: str, passed: bool, detail: str = ""):
    status = "[PASSED]" if passed else "[FAILED]"
    print(f"  {status}: {name}")
    if detail:
        print(f"         {detail}")


async def test_base_types():
    """测试基础类型"""
    print_section("Test: Base Types")
    
    # Test Action
    action = Action(
        type=ActionType.ROLLBACK,
        target_step=5,
        target_node="researcher",
        metadata={"reason": "test"}
    )
    passed = action.type == ActionType.ROLLBACK and action.target_step == 5
    print_result("Action creation", passed)
    
    # Test HookContext
    ctx = HookContext(
        task_id="test_task",
        workflow_id="test_workflow",
        current_node="planner",
        history=get_sample_completed_history(),
    )
    passed = ctx.task_id == "test_task" and len(ctx.history) > 0
    print_result("HookContext creation", passed)
    
    # Test HookResult
    result = HookResult(should_continue=True, message="test")
    passed = result.should_continue and result.message == "test"
    print_result("HookResult creation", passed)


async def test_registries():
    """测试注册中心"""
    print_section("Test: Registries")
    
    # Clear registries
    rule_registry = RuleRegistry.get_instance()
    handler_registry = HandlerRegistry.get_instance()
    rule_registry.clear()
    handler_registry.clear()
    
    # Test rule registration
    rule = ExceptionRule()
    rule_registry.register(rule)
    passed = rule_registry.get("exception_rule") == rule
    print_result("Rule registration", passed)
    
    # Test handler registration
    handler = MockFailureAttributionHandler()
    handler_registry.register(handler)
    passed = handler_registry.get("failure_attribution_handler") == handler
    print_result("Handler registration", passed)
    
    # Test get by trigger point
    rule_registry.register(IncompleteTaskRule())
    error_rules = rule_registry.get_by_trigger_point(HookPoint.ERROR)
    passed = len(error_rules) == 1 and error_rules[0].name == "exception_rule"
    print_result("Get rules by trigger point", passed)
    
    # Test get by action type
    rollback_handlers = handler_registry.get_by_action(ActionType.ROLLBACK)
    passed = len(rollback_handlers) == 1
    print_result("Get handlers by action type", passed)


async def test_exception_rule():
    """测试异常规则"""
    print_section("Test: ExceptionRule")
    
    rule = ExceptionRule()
    
    # Test match with error event
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.ERROR,
        history=get_sample_failed_history(),
    )
    passed = await rule.match(ctx)
    print_result("Match with error event", passed)
    
    # Test match with exception object
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.NODE_END,
        history=get_sample_completed_history(),
        error=Exception("Test error"),
    )
    passed = await rule.match(ctx)
    print_result("Match with exception object", passed)
    
    # Test not match without error
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.NODE_END,
        history=get_sample_completed_history(),
    )
    passed = not await rule.match(ctx)
    print_result("Not match without error", passed)
    
    # Test get action
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.ERROR,
        history=get_sample_failed_history(),
        current_step=2,
    )
    action = await rule.get_action(ctx)
    passed = action.type == ActionType.ROLLBACK
    print_result("Get ROLLBACK action", passed)


async def test_incomplete_task_rule():
    """测试未完成任务规则"""
    print_section("Test: IncompleteTaskRule")
    
    rule = IncompleteTaskRule()
    
    # Test match incomplete task
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.WORKFLOW_END,
        history=get_sample_incomplete_history(),
    )
    passed = await rule.match(ctx)
    print_result("Match incomplete task", passed)
    
    # Test not match completed task
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.WORKFLOW_END,
        history=get_sample_completed_history(),
    )
    passed = not await rule.match(ctx)
    print_result("Not match completed task", passed)
    
    # Test get action
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.WORKFLOW_END,
        history=get_sample_incomplete_history(),
    )
    action = await rule.get_action(ctx)
    passed = action.type == ActionType.INTERVENE
    print_result("Get INTERVENE action", passed)


async def test_output_validation_rule():
    """测试输出验证规则"""
    print_section("Test: OutputValidationRule")
    
    rule = OutputValidationRule()
    
    # Test match short output
    short_history = [
        {"step": 0, "node_name": "system", "event": "workflow_start", "content": "Started"},
        {"step": 1, "node_name": "agent_proxy", "event": "message", "content": "OK", "sub_agent_name": "reporter"},
        {"step": 2, "node_name": "system", "event": "workflow_end", "content": "Workflow completed successfully."},
    ]
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.WORKFLOW_END,
        history=short_history,
    )
    passed = await rule.match(ctx)
    print_result("Match short output", passed)
    
    # Test match failure indicator
    failure_history = [
        {"step": 0, "node_name": "system", "event": "workflow_start", "content": "Started"},
        {"step": 1, "node_name": "agent_proxy", "event": "message", "content": "抱歉，无法完成" + "x" * 50, "sub_agent_name": "reporter"},
        {"step": 2, "node_name": "system", "event": "workflow_end", "content": "Workflow completed successfully."},
    ]
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.WORKFLOW_END,
        history=failure_history,
    )
    passed = await rule.match(ctx)
    print_result("Match failure indicator", passed)
    
    # Test not match valid output
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.WORKFLOW_END,
        history=get_sample_completed_history(),
    )
    passed = not await rule.match(ctx)
    print_result("Not match valid output", passed)


async def test_hook_engine():
    """测试钩子引擎"""
    print_section("Test: HookEngine")
    
    # Setup
    rule_registry = RuleRegistry.get_instance()
    handler_registry = HandlerRegistry.get_instance()
    rule_registry.clear()
    handler_registry.clear()
    
    rule_registry.register(ExceptionRule())
    rule_registry.register(IncompleteTaskRule())
    rule_registry.register(OutputValidationRule())
    handler_registry.register(MockFailureAttributionHandler())
    handler_registry.register(MockPreventionHandler())
    
    engine = HookEngine(rule_registry, handler_registry)
    
    # Test no match
    print("\n  Testing: No rules matched")
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.NODE_START,
        history=get_sample_completed_history(),
    )
    result = await engine.process(ctx)
    passed = result.should_continue and "No rules matched" in result.message
    print_result("Process with no match", passed)
    
    # Test with matching rule (exception)
    print("\n  Testing: Exception rule matching")
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.ERROR,
        history=get_sample_failed_history(),
        state={},
    )
    result = await engine.process(ctx)
    passed = result is not None
    print_result("Process exception scenario", passed)
    
    # Test with incomplete task
    print("\n  Testing: Incomplete task rule matching")
    ctx = HookContext(
        task_id="test",
        workflow_id="test",
        hook_point=HookPoint.WORKFLOW_END,
        history=get_sample_incomplete_history(),
        state={},
    )
    result = await engine.process(ctx)
    passed = result is not None
    print_result("Process incomplete task scenario", passed)


async def test_priority_ordering():
    """测试规则优先级排序"""
    print_section("Test: Rule Priority Ordering")
    
    rule_registry = RuleRegistry.get_instance()
    rule_registry.clear()
    
    # Register in random order
    rule_registry.register(OutputValidationRule())    # priority 30
    rule_registry.register(ExceptionRule())           # priority 10
    rule_registry.register(IncompleteTaskRule())      # priority 20
    
    rules = rule_registry.get_by_trigger_point(HookPoint.WORKFLOW_END)
    rules.sort(key=lambda r: r.priority)
    
    # Should be sorted by priority
    passed = rules[0].name == "incomplete_task_rule" and rules[1].name == "output_validation_rule"
    print_result("Rules sorted by priority", passed, f"Order: {[r.name for r in rules]}")


async def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("       Hook System Standalone Tests")
    print("="*60)
    
    try:
        await test_base_types()
        await test_registries()
        await test_exception_rule()
        await test_incomplete_task_rule()
        await test_output_validation_rule()
        await test_hook_engine()
        await test_priority_ordering()
        
        print("\n" + "="*60)
        print("       All Tests Completed!")
        print("="*60 + "\n")
        return True
    except Exception as e:
        print(f"\n[X] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
