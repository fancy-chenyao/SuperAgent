#!/usr/bin/env python
"""Base class for remote agents with multi-tool support."""

from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod
from contextvars import ContextVar, Token
import logging

from src.contracts.agent_contract import AgentContract
from src.contracts.agent_result import (
    AgentResultEnvelope,
    AgentResultError,
    AgentResultMetadata,
    AgentResultStatus,
)

logger = logging.getLogger(__name__)

_authorized_remote_tools: ContextVar[Optional[frozenset[str]]] = ContextVar(
    "authorized_remote_tools", default=None
)


def bind_authorized_remote_tools(context: Dict[str, Any]) -> Token:
    """Bind a request-scoped platform authorization manifest."""

    raw = context.get("authorized_remote_tools")
    if not isinstance(raw, list) or not raw:
        return _authorized_remote_tools.set(None)
    names = {
        str(item.get("tool_name") or "")
        for item in raw
        if isinstance(item, dict) and item.get("tool_name")
    }
    return _authorized_remote_tools.set(frozenset(names))


def reset_authorized_remote_tools(token: Token) -> None:
    _authorized_remote_tools.reset(token)


class RemoteToolExecutionError(RuntimeError):
    """Tool failure carrying machine-readable side-effect phase metadata."""

    def __init__(self, tool_name: str, result: Dict[str, Any]):
        detail = result.get("error") or result.get("message") or "unknown tool error"
        super().__init__(f"Tool {tool_name} failed: {detail}")
        self.tool_name = tool_name
        self.tool_result = dict(result)


class BaseRemoteAgent(ABC):
    """Base class for all remote agents."""

    def __init__(
        self,
        name: str,
        prompt: str,
        contract: AgentContract | None = None,
    ):
        self.name = name
        self.prompt = prompt
        self.contract = contract

    def result_envelope(
        self,
        *,
        outputs: Dict[str, Any] | None = None,
        error: AgentResultError | None = None,
    ) -> Dict[str, Any]:
        outputs = outputs or {}
        if outputs and error:
            status = AgentResultStatus.PARTIAL
        elif error:
            status = AgentResultStatus.ERROR
        else:
            status = AgentResultStatus.SUCCESS
        contract_version = self.contract.contract_version if self.contract else "1.0"
        envelope = AgentResultEnvelope(
            contract_version=contract_version,
            status=status,
            outputs=outputs,
            error=error,
            metadata=AgentResultMetadata(
                producer_agent=self.name,
                schema_version=contract_version,
            ),
        )
        return envelope.model_dump(mode="json")

    @staticmethod
    def execution_error(
        exc: Exception,
        *,
        tool_name: str,
    ) -> AgentResultError:
        if isinstance(exc, TimeoutError):
            return AgentResultError(
                code="REMOTE_TOOL_TIMEOUT",
                message=str(exc) or f"{tool_name} timed out",
                retryable=True,
                details={"tool": tool_name},
            )
        return AgentResultError(
            code="REMOTE_TOOL_ERROR",
            message=str(exc) or f"{tool_name} failed",
            retryable=False,
            details={"tool": tool_name},
        )

    @abstractmethod
    async def execute(
        self,
        tools: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        context: Dict[str, Any],
        parameter_extractor: Any
    ) -> Dict[str, Any]:
        """
        Execute the agent with given tools and messages.

        Args:
            tools: List of tool definitions to call
            messages: Conversation history
            context: Additional context
            parameter_extractor: LLM parameter extractor instance

        Returns:
            Execution result dictionary
        """
        pass

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_service_url: str = "http://127.0.0.1:8011/tool",
        timeout: int = 10
    ) -> Any:
        """
        Call a single tool.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments
            tool_service_url: URL of the tool service
            timeout: Request timeout in seconds

        Returns:
            Tool execution result
        """
        import httpx

        allowed_tools = _authorized_remote_tools.get()
        if allowed_tools is not None and tool_name not in allowed_tools:
            raise PermissionError(
                f"Remote tool '{tool_name}' is outside the platform-authorized manifest"
            )

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=timeout)) as client:
                resp = await client.post(
                    tool_service_url,
                    json={"tool": tool_name, "arguments": arguments},
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                payload = resp.json()
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(
                        f"Tool {tool_name} returned an invalid result payload"
                    )
                if str(result.get("status") or "").lower() in {"error", "failed"}:
                    raise RemoteToolExecutionError(tool_name, result)
                logger.info(f"Tool {tool_name} executed successfully")
                return result
        except httpx.TimeoutException as exc:
            message = f"Tool {tool_name} timed out after {timeout}s"
            logger.error(message)
            raise TimeoutError(message) from exc
        except Exception as e:
            detail = str(e) or type(e).__name__
            logger.error(f"Tool {tool_name} execution failed: {detail}")
            raise
