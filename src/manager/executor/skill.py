import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from .base import ExecuteResult, ExecutionStatus

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


class SkillExecutor:
    """Local skill executor using SkillsManager."""

    def __init__(self, skills_manager):
        self.skills_manager = skills_manager

    async def execute(self, skill_name: str, arguments: Dict[str, Any]) -> ExecuteResult:
        try:
            if self.skills_manager is None:
                return ExecuteResult(status=ExecutionStatus.FAILED, error="SkillsManager not available")
            result = await self.skills_manager.execute_skill(skill_name, **arguments)
            return ExecuteResult(status=ExecutionStatus.SUCCESS, result=result)
        except Exception as e:
            return ExecuteResult(status=ExecutionStatus.FAILED, error=str(e))


class RemoteSkillExecutor:
    """Remote skill executor (HTTP)."""

    def __init__(self, timeout: float = 10.0, max_retries: int = 2, retry_delay: float = 0.5):
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self.timeout)
            return self._client

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    async def execute(
        self,
        endpoint: str,
        skill_name: str,
        arguments: Dict[str, Any],
        auth: Optional[Dict[str, Any]] = None,
        protocol: str = "http",
    ) -> ExecuteResult:
        try:
            payload = {"skill": skill_name, "arguments": arguments}
            headers = {"Content-Type": "application/json"}
            if auth and auth.get("api_key"):
                headers["Authorization"] = f"Bearer {auth['api_key']}"

            # For now, route rpc/sse/stdio through HTTP endpoint (protocol-aware metadata only).
            data = await self._send_request(endpoint, payload, headers)
            if data.get("status") == "success":
                return ExecuteResult(status=ExecutionStatus.SUCCESS, result=data.get("result"))
            return ExecuteResult(status=ExecutionStatus.FAILED, error=data.get("error", "remote skill error"))
        except httpx.TimeoutException as e:
            return ExecuteResult(status=ExecutionStatus.TIMEOUT, error=f"remote skill timeout: {e}")
        except httpx.RequestError as e:
            return ExecuteResult(status=ExecutionStatus.FAILED, error=f"remote skill network error: {e}")
        except Exception as e:
            return ExecuteResult(status=ExecutionStatus.FAILED, error=str(e))

    async def _send_request(self, endpoint: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        client = await self._ensure_client()
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException as e:
                last_error = e
            except httpx.RequestError as e:
                last_error = e
            except httpx.HTTPStatusError as e:
                last_error = e
                if 500 <= e.response.status_code < 600 and attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                raise

            if attempt < self.max_retries:
                await asyncio.sleep(self.retry_delay * (attempt + 1))
        raise last_error or RuntimeError("remote skill request failed")
