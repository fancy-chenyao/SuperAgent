from dataclasses import dataclass
from typing import Optional
import re


@dataclass(frozen=True)
class ToolIdentifier:
    """工具全局唯一标识
    
    使用命名空间避免工具冲突：
    - scope: 作用域 ("global" | "agent")
    - server: 来源服务器/服务 ("builtin" | MCP服务器名称)
    - name: 工具名称
    
    例如：
    - global:builtin:bash - 全局内置bash工具
    - global:mcp-filesystem:read_file - 全局MCP文件系统工具
    - agent:coder:mcp-github:create_issue - Agent专属的MCP GitHub工具
    """
    scope: str
    server: str
    name: str
    
    def __str__(self) -> str:
        return f"{self.scope}:{self.server}:{self.name}"
    
    def __hash__(self) -> int:
        return hash((self.scope, self.server, self.name))
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ToolIdentifier):
            return False
        return (self.scope == other.scope and 
                self.server == other.server and 
                self.name == other.name)
    
    @classmethod
    def from_string(cls, identifier: str) -> "ToolIdentifier":
        """从字符串解析ToolIdentifier
        
        Args:
            identifier: 形如 "scope:server:name" 的字符串
            
        Returns:
            ToolIdentifier实例
            
        Raises:
            ValueError: 格式无效
        """
        parts = identifier.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"Invalid tool identifier: '{identifier}'. "
                f"Expected format 'scope:server:name'"
            )
        
        scope, server, name = parts
        
        if scope not in ("global", "agent"):
            raise ValueError(
                f"Invalid scope: '{scope}'. Must be 'global' or 'agent'"
            )
        
        return cls(scope=scope, server=server, name=name)
    
    @classmethod
    def from_tool_name(cls, name: str, scope: str = "global", server: str = "builtin") -> "ToolIdentifier":
        """从工具名称创建标识符（兼容旧接口）
        
        Args:
            name: 工具名称
            scope: 作用域，默认为 "global"
            server: 来源服务器，默认为 "builtin"
            
        Returns:
            ToolIdentifier实例
        """
        return cls(scope=scope, server=server, name=name)
    
    @property
    def is_global(self) -> bool:
        """是否为全局工具"""
        return self.scope == "global"
    
    @property
    def is_agent_specific(self) -> bool:
        """是否为Agent专属工具"""
        return self.scope == "agent"
    
    @property
    def is_builtin(self) -> bool:
        """是否为内置工具"""
        return self.server == "builtin"
    
    @property
    def is_mcp(self) -> bool:
        """是否为MCP工具"""
        return self.server not in ("builtin", "local")


class ToolScope:
    """工具作用域常量"""
    GLOBAL = "global"
    AGENT = "agent"


class ToolServer:
    """工具来源服务器常量"""
    BUILTIN = "builtin"
    LOCAL = "local"
    
    @classmethod
    def is_valid_server(cls, server: str) -> bool:
        """检查服务器名称是否有效"""
        return bool(re.match(r'^[a-zA-Z0-9_-]+$', server))
