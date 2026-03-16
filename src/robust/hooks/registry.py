"""
Registry for rules and handlers.
"""

import logging
from typing import Dict, List, Optional, Type

from src.robust.hooks.base import ActionType, Handler, HookPoint, Rule

logger = logging.getLogger(__name__)


class RuleRegistry:
    """规则注册中心。"""
    
    _instance: Optional["RuleRegistry"] = None
    _rules: Dict[str, Rule] = {}
    
    def __new__(cls) -> "RuleRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @classmethod
    def get_instance(cls) -> "RuleRegistry":
        return cls()
    
    def register(self, rule: Rule) -> None:
        """注册规则。"""
        self._rules[rule.name] = rule
        logger.debug(f"Registered rule: {rule.name}")
    
    def unregister(self, name: str) -> None:
        """注销规则。"""
        if name in self._rules:
            del self._rules[name]
            logger.debug(f"Unregistered rule: {name}")
    
    def get(self, name: str) -> Optional[Rule]:
        """获取规则。"""
        return self._rules.get(name)
    
    def get_all(self) -> List[Rule]:
        """获取所有规则。"""
        return list(self._rules.values())
    
    def get_by_trigger_point(self, point: HookPoint) -> List[Rule]:
        """按触发点获取规则。"""
        return [
            rule for rule in self._rules.values()
            if point in rule.trigger_points
        ]
    
    def clear(self) -> None:
        """清空所有规则。"""
        self._rules.clear()


class HandlerRegistry:
    """处理器注册中心。"""
    
    _instance: Optional["HandlerRegistry"] = None
    _handlers: Dict[str, Handler] = {}
    
    def __new__(cls) -> "HandlerRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @classmethod
    def get_instance(cls) -> "HandlerRegistry":
        return cls()
    
    def register(self, handler: Handler) -> None:
        """注册处理器。"""
        self._handlers[handler.name] = handler
        logger.debug(f"Registered handler: {handler.name}")
    
    def unregister(self, name: str) -> None:
        """注销处理器。"""
        if name in self._handlers:
            del self._handlers[name]
            logger.debug(f"Unregistered handler: {name}")
    
    def get(self, name: str) -> Optional[Handler]:
        """获取处理器。"""
        return self._handlers.get(name)
    
    def get_by_action(self, action_type: ActionType) -> List[Handler]:
        """按动作类型获取处理器。"""
        return [
            handler for handler in self._handlers.values()
            if action_type in handler.supported_actions
        ]
    
    def get_all(self) -> List[Handler]:
        """获取所有处理器。"""
        return list(self._handlers.values())
    
    def clear(self) -> None:
        """清空所有处理器。"""
        self._handlers.clear()


def register_rule(rule: Rule) -> None:
    """便捷函数：注册规则。"""
    RuleRegistry.get_instance().register(rule)


def register_handler(handler: Handler) -> None:
    """便捷函数：注册处理器。"""
    HandlerRegistry.get_instance().register(handler)
