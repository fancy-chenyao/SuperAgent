from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import httpx

from .resource_registry import ResourceRegistry, ResourceSpec


@dataclass
class RemoteRegistrySource:
    name: str
    base_url: str
    server_id: str
    priority: int = 100
    timeout: float = 5.0
    health_check: bool = False


class RemoteRegistryGateway:
    """Fetch resources from multiple remote registries and merge into local registry."""

    def __init__(
        self,
        registry: ResourceRegistry,
        sources: Iterable[RemoteRegistrySource],
        resources_path: str = "/resources",
    ):
        self.registry = registry
        self.sources = list(sources)
        self.resources_path = resources_path
        self._lock = asyncio.Lock()

    async def refresh(self) -> Dict[str, Any]:
        async with self._lock:
            results: Dict[str, Any] = {"loaded": 0, "errors": {}}
            async with httpx.AsyncClient() as client:
                for source in sorted(self.sources, key=lambda s: s.priority):
                    try:
                        resources = await self._fetch_source(source, client)
                        await self._merge_resources(resources, source)
                        results["loaded"] += len(resources)
                    except Exception as e:
                        results["errors"][source.name] = str(e)
            return results

    async def _fetch_source(self, source: RemoteRegistrySource, client: httpx.AsyncClient) -> List[ResourceSpec]:
        url = source.base_url.rstrip("/") + self.resources_path
        resp = await client.get(url, timeout=source.timeout)
        resp.raise_for_status()
        payload = resp.json()

        items = payload.get("resources", payload)
        specs: List[ResourceSpec] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if "server_id" not in item:
                item["server_id"] = source.server_id
            spec = ResourceSpec(**item)
            if source.health_check and spec.health_url:
                spec.metadata = dict(spec.metadata)
                spec.metadata["healthy"] = await self._check_health(spec, source.timeout, client)
            specs.append(spec)
        return specs

    async def _check_health(self, spec: ResourceSpec, timeout: float, client: httpx.AsyncClient) -> bool:
        try:
            resp = await client.get(spec.health_url, timeout=timeout)
            return resp.status_code == 200
        except Exception:
            return False

    async def _merge_resources(self, specs: List[ResourceSpec], source: RemoteRegistrySource) -> None:
        for spec in specs:
            if spec.metadata.get("healthy") is False:
                await self.registry.delete(spec.type, spec.name, spec.server_id, remove_file=False)
                continue
            existing = await self.registry.get(spec.type, spec.name, spec.server_id)
            if existing is None:
                await self.registry.register(spec, persist=False)
                continue

            # Conflict strategy: keep higher priority (lower value), then higher version
            if source.priority <= self._priority_for(existing):
                if self._version_key(spec.version) >= self._version_key(existing.version):
                    await self.registry.update(spec, persist=False)

    def _priority_for(self, _spec: ResourceSpec) -> int:
        # Local registry entries should win by default
        return 0 if _spec.server_id == "local" else 100

    @staticmethod
    def _version_key(version: str) -> List[int]:
        parts: List[int] = []
        for item in version.split("."):
            try:
                parts.append(int(item))
            except Exception:
                parts.append(0)
        return parts
