import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
except Exception:  # pragma: no cover
    class HumanMessage:  # type: ignore
        pass

    class AIMessage:  # type: ignore
        pass

    class SystemMessage:  # type: ignore
        pass

from .base import AgentExecutor, ExecuteResult, ExecutionContext, ExecutionStatus

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


@dataclass
class RemoteAgentRequest:
    agent_name: str
    messages: List[Dict[str, Any]]
    context: Dict[str, Any]
    tools: Optional[List[Dict[str, Any]]] = None


@dataclass
class RemoteAgentResponse:
    status: str
    result: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_execute_result(self, duration: float) -> ExecuteResult:
        if self.status == "success":
            return ExecuteResult(
                status=ExecutionStatus.SUCCESS,
                result=self.result,
                metadata=self.metadata or {},
            )
        return ExecuteResult(
            status=ExecutionStatus.FAILED,
            error=self.error or "Unknown error",
            metadata=self.metadata or {},
        )


class RemoteExecutor(AgentExecutor):
    def __init__(
        self,
        timeout: int = 120,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        max_concurrency: int = 64,
    ):
        super().__init__()
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._max_concurrency = max_concurrency
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._request_semaphore = asyncio.Semaphore(max_concurrency)

    async def _do_initialize(self):
        # Keep initialization lightweight; create session lazily on first request.
        return

    async def _ensure_session(self):
        if self._session is not None and not self._session.closed:
            return
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                )

    async def cleanup(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def load_tools(self, agent: Any) -> List[Any]:
        return []

    async def execute(self, agent: Any, messages: List[Any], context: ExecutionContext) -> ExecuteResult:
        start_time = time.time()

        if not await self.validate(agent):
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error="Agent validation failed: missing endpoint or agent_name",
            )

        endpoint = getattr(agent, "endpoint", None)
        if not endpoint:
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error="Remote agent missing endpoint",
            )

        try:
            await self.initialize()
            request_data = self._build_request(agent, messages, context)
            headers = await self._build_headers(agent)
            async with self._request_semaphore:
                response_data = await self._send_request(endpoint, request_data, headers)

            duration = time.time() - start_time
            remote_response = RemoteAgentResponse(
                status=response_data.get("status", "failed"),
                result=response_data.get("result"),
                error=response_data.get("error"),
                metadata=response_data.get("metadata", {}),
            )
            result = remote_response.to_execute_result(duration)
            result.metadata["duration"] = duration
            result.metadata["endpoint"] = endpoint
            return result
        except asyncio.TimeoutError as e:
            duration = time.time() - start_time
            return ExecuteResult(
                status=ExecutionStatus.TIMEOUT,
                error=f"Remote request timeout: {e}",
                metadata={
                    "agent_name": getattr(agent, "agent_name", "unknown"),
                    "endpoint": endpoint,
                    "duration": duration,
                },
            )
        except aiohttp.ClientError as e:
            duration = time.time() - start_time
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error=f"Network error: {e}",
                metadata={
                    "agent_name": getattr(agent, "agent_name", "unknown"),
                    "endpoint": endpoint,
                    "duration": duration,
                },
            )
        except Exception as e:
            duration = time.time() - start_time
            return ExecuteResult(
                status=ExecutionStatus.FAILED,
                error=str(e),
                metadata={
                    "agent_name": getattr(agent, "agent_name", "unknown"),
                    "endpoint": endpoint,
                    "duration": duration,
                },
            )

    def _build_request(self, agent: Any, messages: List[Any], context: ExecutionContext) -> Dict[str, Any]:
        serialized_messages = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                serialized_messages.append({"type": "human", "content": msg.content})
            elif isinstance(msg, AIMessage):
                serialized_messages.append({"type": "ai", "content": msg.content})
            elif isinstance(msg, SystemMessage):
                serialized_messages.append({"type": "system", "content": msg.content})
            elif hasattr(msg, "content"):
                serialized_messages.append({"type": "unknown", "content": msg.content})
            else:
                serialized_messages.append({"type": "unknown", "content": str(msg)})

        request = {
            "agent_name": agent.agent_name,
            "messages": serialized_messages,
            "context": {
                "user_id": context.user_id,
                "workflow_id": context.workflow_id,
                "workflow_mode": context.workflow_mode,
                "deep_thinking_mode": context.deep_thinking_mode,
                "debug": context.debug,
            },
        }

        if getattr(agent, "prompt", None):
            request["prompt"] = agent.prompt

        return request

    async def _build_headers(self, agent: Any) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = getattr(agent, "api_key", None)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _send_request(
        self,
        endpoint: str,
        data: Dict[str, Any],
        headers: Dict[str, str],
        retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        if retries is None:
            retries = self._max_retries

        await self.initialize()
        await self._ensure_session()

        last_error: Optional[str] = None

        for attempt in range(retries):
            try:
                assert self._session is not None
                async with asyncio.timeout(self._timeout):
                    async with self._session.post(endpoint, json=data, headers=headers) as response:
                        if response.status == 200:
                            return await response.json()
                        if response.status == 401:
                            raise Exception("Authentication failed: invalid API key")
                        if response.status == 403:
                            raise Exception("Authorization failed: insufficient permissions")
                        if response.status == 404:
                            raise Exception(f"Agent not found: {endpoint}")
                        if response.status >= 500:
                            last_error = f"Server error: {response.status}"
                            if attempt < retries - 1:
                                await asyncio.sleep(self._retry_delay * (attempt + 1))
                                continue
                        else:
                            text = await response.text()
                            raise Exception(f"Request failed with status {response.status}: {text}")
            except TimeoutError as e:
                last_error = f"timeout: {e}"
                if attempt < retries - 1:
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                    continue
                raise asyncio.TimeoutError(last_error)
            except aiohttp.ClientError as e:
                last_error = str(e)
                if attempt < retries - 1:
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                    continue
            except Exception as e:
                last_error = str(e)
                if attempt < retries - 1:
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                    continue

        raise Exception(f"Failed after {retries} retries: {last_error}")

    async def validate(self, agent: Any) -> bool:
        if not getattr(agent, "endpoint", None):
            return False
        if not getattr(agent, "agent_name", None):
            return False
        return True

    async def health_check(self, endpoint: str) -> bool:
        try:
            await self.initialize()
            await self._ensure_session()
            assert self._session is not None
            async with self._session.get(
                f"{endpoint.rstrip('/')}/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                return response.status == 200
        except Exception:
            return False
