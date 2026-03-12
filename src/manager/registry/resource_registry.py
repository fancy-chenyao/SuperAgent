from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class ResourceKey:
    type: str
    name: str
    server_id: str

    def __str__(self) -> str:
        return f"{self.type}:{self.server_id}:{self.name}"


@dataclass
class ResourceSpec:
    """Unified resource descriptor for agent/tool/mcp/skill."""

    type: str
    name: str
    version: str = "1.0.0"
    endpoint: Optional[str] = None
    protocol: Optional[str] = None
    auth: Optional[Dict[str, Any]] = None
    server_id: str = "local"
    tags: List[str] = field(default_factory=list)
    health_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> ResourceKey:
        return ResourceKey(self.type, self.name, self.server_id)


class ResourceRegistry:
    """Unified registry for all resources (agents/tools/mcp/skills)."""

    def __init__(self, base_dir: Optional[Path] = None):
        self._lock = asyncio.Lock()
        self._resources: Dict[ResourceKey, ResourceSpec] = {}
        self._base_dir = Path(base_dir) if base_dir else None
        if self._base_dir:
            self._base_dir.mkdir(parents=True, exist_ok=True)

    async def register(self, spec: ResourceSpec, persist: bool = True) -> ResourceSpec:
        async with self._lock:
            self._resources[spec.key()] = spec
            if persist and self._base_dir:
                await self._save_to_disk(spec)
            return spec

    async def get(self, type: str, name: str, server_id: str = "local") -> Optional[ResourceSpec]:
        async with self._lock:
            return self._resources.get(ResourceKey(type, name, server_id))

    async def list(
        self,
        type: Optional[str] = None,
        server_id: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
    ) -> List[ResourceSpec]:
        async with self._lock:
            items = list(self._resources.values())

        if type:
            items = [r for r in items if r.type == type]
        if server_id:
            items = [r for r in items if r.server_id == server_id]
        if tags:
            tag_set = set(tags)
            items = [r for r in items if tag_set.intersection(set(r.tags))]
        return items

    async def update(self, spec: ResourceSpec, persist: bool = True) -> ResourceSpec:
        async with self._lock:
            key = spec.key()
            if key not in self._resources:
                raise KeyError(f"Resource not found: {key}")
            self._resources[key] = spec
            if persist and self._base_dir:
                await self._save_to_disk(spec)
            return spec

    async def delete(self, type: str, name: str, server_id: str = "local", remove_file: bool = True) -> bool:
        key = ResourceKey(type, name, server_id)
        async with self._lock:
            existed = key in self._resources
            if existed:
                del self._resources[key]

        if existed and remove_file and self._base_dir:
            path = self._resource_path(type, name, server_id)
            if path.exists():
                path.unlink()
        return existed

    async def snapshot(self) -> Dict[ResourceKey, ResourceSpec]:
        async with self._lock:
            return dict(self._resources)

    async def load_from_disk(self) -> int:
        if not self._base_dir:
            return 0
        count = 0
        for path in self._base_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                spec = ResourceSpec(**payload)
                async with self._lock:
                    self._resources[spec.key()] = spec
                count += 1
            except Exception:
                continue
        return count

    async def _save_to_disk(self, spec: ResourceSpec) -> None:
        if not self._base_dir:
            return
        path = self._resource_path(spec.type, spec.name, spec.server_id)
        path.write_text(json.dumps(spec.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")

    def _resource_path(self, type: str, name: str, server_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{type}_{server_id}_{name}")
        return self._base_dir / f"{safe}.json"
