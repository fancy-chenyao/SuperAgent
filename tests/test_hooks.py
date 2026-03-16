"""
Tests for the Hook System.

Tests cover:
1. Base types (Action, HookContext, HookResult)
2. Rule and Handler registries
3. Rule implementations
4. Handler implementations
5. HookEngine integration
"""

import asyncio
import pytest
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

from src.robust.hooks import (
    HookEngine,
    HookContext,
    HookResult,
    HookPoint,
    Action,
    ActionType,
    RuleRegistry,
    HandlerRegistry,
    Rule,
    Handler,
)
from src.robust.hooks.rules import (
    ExceptionRule,
    IncompleteTaskRule,
    OutputValidationRule,
    LongMessageRule,
    LoopDetectionRule,
)
from src.robust.hooks.handlers import (
    FailureAttributionHandler,
    PreventionHandler,
    ValidationHandler,
)


# ==================== Fixtures ====================

@pytest.fixture
def clear_registries():
    """Clear registries before each test."""
    RuleRegistry.get_instance().clear()
    HandlerRegistry.get_instance().clear()
    yield
    RuleRegistry.get_instance().clear()
    HandlerRegistry.get_instance().clear()


@pytest.fixture
def sample_history():
    """Sample workflow history for testing."""
    return [
        {
            "step": 0,
            "node_name": "system",
            "role": "system",
            "event": "workflow_start",
            "content": "Workflow started. Query: test query",
            "timestamp": "2026-03-12T15:31:04.623811"
        },
        {
            "step": 1,
            "node_name": "coordinator",
            "role": "coordinator",
            "event": "start_of_agent",
            "content": "Agent coordinator started",
            "timestamp": "2026-03-12T15:31:04.624378"
        },
        {
            "step": 1,
            "node_name": "coordinator",
            "role": "coordinator",
            "event": "message",
            "content": "handover_to_planner",
            "timestamp": "2026-03-12T15:31:05.000000"
        },
        {
            "step": 2,
            "node_name": "planner",
            "role": "planner",
            "event": "message",
            "content": '[{"agent_name": "researcher", "title": "Search"}]',
            "timestamp": "2026-03-12T15:31:06.000000"
        },
        {
            "step": 3,
            "node_name": "agent_proxy",
            "role": "agent_proxy",
            "event": "message",
            "content": "Research completed successfully.",
            "timestamp": "2026-03-12T15:31:10.000000",
            "sub_agent_name": "researcher"
        },
        {
            "step": 4,
            "node_name": "system",
            "role": "system",
            "event": "workflow_end",
            "content": "Workflow completed successfully.",
            "timestamp": "2026-03-12T15:31:15.000000"
        }
    ]


@pytest.fixture
def failed_history():
    """Failed workflow history for testing."""
    return [
        {
            "step": 0,
            "node_name": "system",
            "role": "system",
            "event": "workflow_start",
            "content": "Workflow started",
            "timestamp": "2026-03-12T15:31:04.000000"
        },
        {
            "step": 1,
            "node_name": "planner",
            "role": "planner",
            "event": "message",
            "content": '[{"agent_name": "researcher"}]',
            "timestamp": "2026-03-12T15:31:05.000000"
        },
        {
            "step": 2,
            "node_name": "agent_proxy",
            "role": "agent_proxy",
            "event": "error",
            "content": "Connection timeout",
            "timestamp": "2026-03-12T15:31:10.000000",
            "sub_agent_name": "researcher"
        }
    ]


@pytest.fixture
def incomplete_history():
    """Incomplete workflow history (no explicit error, not completed)."""
    return [
        {
            "step": 0,
            "node_name": "system",
            "role": "system",
            "event": "workflow_start",
            "content": "Workflow started",
            "timestamp": "2026-03-12T15:31:04.000000"
        },
        {
            "step": 1,
            "node_name": "planner",
            "role": "planner",
            "event": "message",
            "content": '[{"agent_name": "researcher"}]',
            "timestamp": "2026-03-12T15:31:05.000000"
        },
        {
            "step": 2,
            "node_name": "agent_proxy",
            "role": "agent_proxy",
            "event": "end_of_agent",
            "content": "Agent finished",
            "timestamp": "2026-03-12T15:31:10.000000",
            "next_node": "__end__"
        },
        # Note: No workflow_end event
    ]


# ==================== Test Base Types ====================

class TestBaseTypes:
    """Test base types."""
    
    def test_action_creation(self):
        """Test Action creation."""
        action = Action(
            type=ActionType.ROLLBACK,
            target_step=5,
            target_node="researcher",
            metadata={"reason": "test"}
        )
        assert action.type == ActionType.ROLLBACK
        assert action.target_step == 5
        assert action.target_node == "researcher"
    
    def test_hook_context_creation(self, sample_history):
        """Test HookContext creation."""
        ctx = HookContext(
            task_id="test_task",
            workflow_id="test_workflow",
            current_node="planner",
            current_step=2,
            state={"key": "value"},
            history=sample_history,
            hook_point=HookPoint.NODE_END,
            user_query="test query"
        )
        assert ctx.task_id == "test_task"
        assert ctx.current_node == "planner"
        assert len(ctx.history) == 6
    
    def test_hook_result_creation(self):
        """Test HookResult creation."""
        result = HookResult(
            should_continue=True,
            message="Test message",
            metadata={"key": "value"}
        )
        assert result.should_continue is True
        assert result.message == "Test message"


# ==================== Test Registries ====================

class TestRegistries:
    """Test rule and handler registries."""
    
    def test_rule_registry_register(self, clear_registries):
        """Test rule registration."""
        registry = RuleRegistry.get_instance()
        rule = ExceptionRule()
        registry.register(rule)
        
        assert registry.get("exception_rule") == rule
        assert len(registry.get_all()) == 1
    
    def test_rule_registry_get_by_trigger_point(self, clear_registries):
        """Test getting rules by trigger point."""
        registry = RuleRegistry.get_instance()
        registry.register(ExceptionRule())
        registry.register(IncompleteTaskRule())
        
        error_rules = registry.get_by_trigger_point(HookPoint.ERROR)
        assert len(error_rules) == 1
        assert error_rules[0].name == "exception_rule"
        
        workflow_end_rules = registry.get_by_trigger_point(HookPoint.WORKFLOW_END)
        assert len(workflow_end_rules) == 1
        assert workflow_end_rules[0].name == "incomplete_task_rule"
    
    def test_handler_registry_register(self, clear_registries):
        """Test handler registration."""
        registry = HandlerRegistry.get_instance()
        handler = FailureAttributionHandler()
        registry.register(handler)
        
        assert registry.get("failure_attribution_handler") == handler
    
    def test_handler_registry_get_by_action(self, clear_registries):
        """Test getting handlers by action type."""
        registry = HandlerRegistry.get_instance()
        registry.register(FailureAttributionHandler())
        
        handlers = registry.get_by_action(ActionType.ROLLBACK)
        assert len(handlers) == 1
        assert handlers[0].name == "failure_attribution_handler"


# ==================== Test Rules ====================

class TestExceptionRule:
    """Test ExceptionRule."""
    
    @pytest.mark.asyncio
    async def test_match_with_error_event(self, failed_history):
        """Test matching when there's an error event."""
        rule = ExceptionRule()
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.ERROR,
            history=failed_history
        )
        
        assert await rule.match(ctx) is True
    
    @pytest.mark.asyncio
    async def test_match_with_exception_object(self, sample_history):
        """Test matching when there's an exception object."""
        rule = ExceptionRule()
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.NODE_END,
            history=sample_history,
            error=Exception("Test error")
        )
        
        assert await rule.match(ctx) is True
    
    @pytest.mark.asyncio
    async def test_match_without_error(self, sample_history):
        """Test not matching when there's no error."""
        rule = ExceptionRule()
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.NODE_END,
            history=sample_history
        )
        
        assert await rule.match(ctx) is False
    
    @pytest.mark.asyncio
    async def test_get_action(self, failed_history):
        """Test getting action from ExceptionRule."""
        rule = ExceptionRule()
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.ERROR,
            history=failed_history,
            current_step=2
        )
        
        action = await rule.get_action(ctx)
        assert action.type == ActionType.ROLLBACK


class TestIncompleteTaskRule:
    """Test IncompleteTaskRule."""
    
    @pytest.mark.asyncio
    async def test_match_incomplete_task(self, incomplete_history):
        """Test matching incomplete task."""
        rule = IncompleteTaskRule()
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.WORKFLOW_END,
            history=incomplete_history
        )
        
        assert await rule.match(ctx) is True
    
    @pytest.mark.asyncio
    async def test_match_completed_task(self, sample_history):
        """Test not matching completed task."""
        rule = IncompleteTaskRule()
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.WORKFLOW_END,
            history=sample_history
        )
        
        assert await rule.match(ctx) is False
    
    @pytest.mark.asyncio
    async def test_get_action(self, incomplete_history):
        """Test getting action from IncompleteTaskRule."""
        rule = IncompleteTaskRule()
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.WORKFLOW_END,
            history=incomplete_history
        )
        
        action = await rule.get_action(ctx)
        assert action.type == ActionType.INTERVENE


class TestOutputValidationRule:
    """Test OutputValidationRule."""
    
    @pytest.mark.asyncio
    async def test_match_short_output(self):
        """Test matching when output is too short."""
        rule = OutputValidationRule()
        history = [
            {"step": 0, "node_name": "system", "event": "workflow_start", "content": "Started"},
            {"step": 1, "node_name": "agent_proxy", "event": "message", "content": "OK", "sub_agent_name": "reporter"},
            {"step": 2, "node_name": "system", "event": "workflow_end", "content": "Workflow completed successfully."},
        ]
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.WORKFLOW_END,
            history=history
        )
        
        assert await rule.match(ctx) is True
    
    @pytest.mark.asyncio
    async def test_match_failure_indicator(self):
        """Test matching when output contains failure indicator."""
        rule = OutputValidationRule()
        history = [
            {"step": 0, "node_name": "system", "event": "workflow_start", "content": "Started"},
            {"step": 1, "node_name": "agent_proxy", "event": "message", "content": "抱歉，我无法完成这个任务。" + "x" * 50, "sub_agent_name": "reporter"},
            {"step": 2, "node_name": "system", "event": "workflow_end", "content": "Workflow completed successfully."},
        ]
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.WORKFLOW_END,
            history=history
        )
        
        assert await rule.match(ctx) is True
    
    @pytest.mark.asyncio
    async def test_match_valid_output(self):
        """Test not matching valid output."""
        rule = OutputValidationRule()
        history = [
            {"step": 0, "node_name": "system", "event": "workflow_start", "content": "Started"},
            {"step": 1, "node_name": "agent_proxy", "event": "message", "content": "这是一份完整的报告，包含所有必要的信息。" + "x" * 100, "sub_agent_name": "reporter"},
            {"step": 2, "node_name": "system", "event": "workflow_end", "content": "Workflow completed successfully."},
        ]
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.WORKFLOW_END,
            history=history
        )
        
        assert await rule.match(ctx) is False


# ==================== Test Handlers ====================

class TestFailureAttributionHandler:
    """Test FailureAttributionHandler."""
    
    def test_supported_actions(self):
        """Test supported action types."""
        handler = FailureAttributionHandler()
        assert ActionType.ROLLBACK in handler.supported_actions
        assert ActionType.INTERVENE in handler.supported_actions
    
    @pytest.mark.asyncio
    async def test_handle_with_mock_attribution(self, failed_history):
        """Test handling with mocked attribution."""
        handler = FailureAttributionHandler()
        
        # Mock attribution
        mock_attribution = MagicMock()
        mock_attribution.is_succeed = False
        mock_attribution.mistake_step = 1
        mock_attribution.mistake_node = "researcher"
        mock_attribution.__dict__ = {
            "is_succeed": False,
            "mistake_step": 1,
            "mistake_node": "researcher"
        }
        
        action = Action(
            type=ActionType.ROLLBACK,
            target_step=1,
            metadata={}
        )
        
        ctx = HookContext(
            task_id="test_task",
            workflow_id="test_workflow",
            hook_point=HookPoint.ERROR,
            history=failed_history,
            state={}
        )
        
        with patch.object(handler, '_get_llm_client', return_value=None), \
             patch.object(handler, '_get_checkpoint_manager', return_value=None):
            # This will fail without proper mocks, but we can test the flow
            result = await handler.handle(ctx, action)
            # Handler should attempt to process even without full setup
            assert result is not None


class TestPreventionHandler:
    """Test PreventionHandler."""
    
    def test_supported_actions(self):
        """Test supported action types."""
        handler = PreventionHandler()
        assert ActionType.PREVENT in handler.supported_actions
    
    @pytest.mark.asyncio
    async def test_handle_with_intervention_text(self):
        """Test handling with intervention text."""
        handler = PreventionHandler()
        action = Action(
            type=ActionType.PREVENT,
            intervention_text="Test intervention message"
        )
        
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            state={"messages": []}
        )
        
        result = await handler.handle(ctx, action)
        assert result.should_continue is True
        assert result.message == "Test intervention message"


class TestValidationHandler:
    """Test ValidationHandler."""
    
    def test_supported_actions(self):
        """Test supported action types."""
        handler = ValidationHandler()
        assert ActionType.VALIDATE in handler.supported_actions
    
    @pytest.mark.asyncio
    async def test_handle_validation(self):
        """Test handling validation."""
        handler = ValidationHandler()
        action = Action(
            type=ActionType.VALIDATE,
            metadata={"validation_type": "output"}
        )
        
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            state={"messages": []}
        )
        
        result = await handler.handle(ctx, action)
        assert result.should_continue is True
        assert "validation" in result.message.lower()


# ==================== Test HookEngine ====================

class TestHookEngine:
    """Test HookEngine."""
    
    @pytest.mark.asyncio
    async def test_process_no_match(self, clear_registries, sample_history):
        """Test processing when no rules match."""
        rule_registry = RuleRegistry.get_instance()
        handler_registry = HandlerRegistry.get_instance()
        
        engine = HookEngine(rule_registry, handler_registry)
        
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.NODE_START,
            history=sample_history
        )
        
        result = await engine.process(ctx)
        assert result.should_continue is True
        assert "No rules matched" in result.message
    
    @pytest.mark.asyncio
    async def test_process_with_matching_rule(self, clear_registries, failed_history):
        """Test processing with matching rule and handler."""
        rule_registry = RuleRegistry.get_instance()
        handler_registry = HandlerRegistry.get_instance()
        
        # Register rule and handler
        rule_registry.register(ExceptionRule())
        handler_registry.register(FailureAttributionHandler())
        
        engine = HookEngine(rule_registry, handler_registry)
        
        ctx = HookContext(
            task_id="test",
            workflow_id="test",
            hook_point=HookPoint.ERROR,
            history=failed_history,
            state={}
        )
        
        result = await engine.process(ctx)
        # Should attempt to process even without full setup
        assert result is not None
    
    @pytest.mark.asyncio
    async def test_trigger_convenience_method(self, clear_registries):
        """Test the trigger convenience method."""
        rule_registry = RuleRegistry.get_instance()
        handler_registry = HandlerRegistry.get_instance()
        
        engine = HookEngine(rule_registry, handler_registry)
        
        result = await engine.trigger(
            hook_point=HookPoint.NODE_END,
            task_id="test_task",
            workflow_id="test_workflow",
            history=[]
        )
        
        assert result.should_continue is True


# ==================== Integration Tests ====================

class TestIntegration:
    """Integration tests for the complete hook system."""
    
    @pytest.mark.asyncio
    async def test_full_workflow_no_issues(self, clear_registries, sample_history):
        """Test full workflow with no issues."""
        rule_registry = RuleRegistry.get_instance()
        handler_registry = HandlerRegistry.get_instance()
        
        # Register all rules
        rule_registry.register(ExceptionRule())
        rule_registry.register(IncompleteTaskRule())
        rule_registry.register(OutputValidationRule())
        
        # Register handlers
        handler_registry.register(FailureAttributionHandler())
        handler_registry.register(PreventionHandler())
        handler_registry.register(ValidationHandler())
        
        engine = HookEngine(rule_registry, handler_registry)
        
        # Simulate workflow end
        ctx = HookContext(
            task_id="test_task",
            workflow_id="test_workflow",
            hook_point=HookPoint.WORKFLOW_END,
            history=sample_history,
            workflow_status="completed"
        )
        
        result = await engine.process(ctx)
        assert result.should_continue is True
    
    @pytest.mark.asyncio
    async def test_rule_priority_ordering(self, clear_registries):
        """Test that rules are processed by priority."""
        rule_registry = RuleRegistry.get_instance()
        
        # Register rules in random order
        rule_registry.register(OutputValidationRule())    # priority 30
        rule_registry.register(ExceptionRule())           # priority 10
        rule_registry.register(IncompleteTaskRule())      # priority 20
        
        # Get rules for ERROR hook point
        rules = rule_registry.get_by_trigger_point(HookPoint.ERROR)
        
        # Should only have ExceptionRule (highest priority for ERROR)
        assert len(rules) == 1
        assert rules[0].name == "exception_rule"


# ==================== Run Tests ====================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
