"""
Workflow Hook System - 工作流钩子系统

提供可扩展的规则引擎和处理器机制，用于在工作流执行过程中进行
故障归因、主动预防、验证校验等操作。
"""

from src.robust.hooks.base import (
    HookPoint,
    HookContext,
    HookResult,
    Action,
    ActionType,
    Rule,
    Handler,
)
from src.robust.hooks.engine import HookEngine
from src.robust.hooks.registry import RuleRegistry, HandlerRegistry
from src.robust.hooks.setup import (
    initialize_hook_system,
    is_initialized,
    reset_hook_system,
)

__all__ = [
    # Base types
    "HookPoint",
    "HookContext",
    "HookResult",
    "Action",
    "ActionType",
    "Rule",
    "Handler",
    # Engine
    "HookEngine",
    # Registry
    "RuleRegistry",
    "HandlerRegistry",
    # Setup
    "initialize_hook_system",
    "is_initialized",
    "reset_hook_system",
]
