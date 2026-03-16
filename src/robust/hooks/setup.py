"""
Hook System Setup - 钩子系统初始化和默认注册。

将规则和处理器的注册逻辑封装在此模块中，
实现与 workflow 模块的解耦。
"""

import logging
from typing import Optional

from src.robust.hooks.registry import RuleRegistry, HandlerRegistry
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

logger = logging.getLogger(__name__)

_initialized = False


def initialize_hook_system(force: bool = False) -> None:
    """
    初始化 Hook 系统，注册默认规则和处理器。
    
    Args:
        force: 是否强制重新初始化（即使已经初始化过）
    """
    global _initialized
    
    if _initialized and not force:
        logger.debug("Hook system already initialized, skipping")
        return
    
    rule_registry = RuleRegistry.get_instance()
    handler_registry = HandlerRegistry.get_instance()
    
    # Register rules (按优先级排序)
    rule_registry.register(ExceptionRule())         # priority 10
    rule_registry.register(IncompleteTaskRule())    # priority 20
    rule_registry.register(OutputValidationRule())  # priority 30
    rule_registry.register(LoopDetectionRule())     # priority 40
    rule_registry.register(LongMessageRule())       # priority 50
    
    # Register handlers
    handler_registry.register(FailureAttributionHandler())
    handler_registry.register(PreventionHandler())
    handler_registry.register(ValidationHandler())
    
    _initialized = True
    logger.info("Hook system initialized with default rules and handlers")


def is_initialized() -> bool:
    """检查 Hook 系统是否已初始化。"""
    return _initialized


def reset_hook_system() -> None:
    """
    重置 Hook 系统。
    
    清空所有已注册的规则和处理器，并重置初始化状态。
    主要用于测试场景。
    """
    global _initialized
    
    RuleRegistry.get_instance().clear()
    HandlerRegistry.get_instance().clear()
    _initialized = False
    logger.info("Hook system reset")
