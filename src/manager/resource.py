import asyncio
import time
from pathlib import Path
from typing import Optional

from src.manager.registry.resource_gateway import RemoteRegistryGateway
from src.manager.registry.resource_registry import ResourceRegistry
from src.manager.registry.remote_registry_config import (
    load_remote_registry_settings,
    load_remote_registry_sources,
)
from src.utils.path_utils import get_project_root

_RESOURCE_REGISTRY = None
_RESOURCE_LOCK = asyncio.Lock()
_REMOTE_REFRESH_TASK: asyncio.Task | None = None
_REMOTE_REFRESH_CACHE: dict | None = None


def _default_resource_dir() -> Path:
    return get_project_root() / "store" / "resources"


def _default_config_path() -> Path:
    return get_project_root() / "config" / "remote_registry.json"


async def get_resource_registry(base_dir: Optional[Path] = None) -> ResourceRegistry:
    global _RESOURCE_REGISTRY
    if _RESOURCE_REGISTRY is not None:
        return _RESOURCE_REGISTRY
    async with _RESOURCE_LOCK:
        if _RESOURCE_REGISTRY is None:
            _RESOURCE_REGISTRY = ResourceRegistry(base_dir=base_dir or _default_resource_dir())
            await _RESOURCE_REGISTRY.load_from_disk()
    return _RESOURCE_REGISTRY


async def refresh_remote_resources(config_path: Optional[Path] = None, force: bool = False) -> dict:
    registry = await get_resource_registry()
    config_path = config_path or _default_config_path()
    settings = load_remote_registry_settings(str(config_path))
    cache_ttl = settings.get("cache_ttl", 30.0)

    global _REMOTE_REFRESH_CACHE
    if not force and _REMOTE_REFRESH_CACHE and cache_ttl > 0:
        age = time.time() - _REMOTE_REFRESH_CACHE.get("timestamp", 0.0)
        if age <= cache_ttl:
            return dict(_REMOTE_REFRESH_CACHE.get("result", {}))

    sources = load_remote_registry_sources(str(config_path))
    if not sources:
        result = {"loaded": 0, "errors": {}, "sources": 0}
        _REMOTE_REFRESH_CACHE = {"timestamp": time.time(), "result": result}
        return result
    gateway = RemoteRegistryGateway(registry, sources)
    result = await gateway.refresh()
    result["sources"] = len(sources)
    _REMOTE_REFRESH_CACHE = {"timestamp": time.time(), "result": result}
    return result


async def start_remote_registry_watch(interval_seconds: float = 30.0) -> None:
    global _REMOTE_REFRESH_TASK
    if _REMOTE_REFRESH_TASK and not _REMOTE_REFRESH_TASK.done():
        return

    probe = await refresh_remote_resources()
    if probe.get("sources", 0) == 0:
        return

    async def _loop():
        while True:
            try:
                await refresh_remote_resources()
            except Exception:
                pass
            await asyncio.sleep(interval_seconds)

    _REMOTE_REFRESH_TASK = asyncio.create_task(_loop())


async def stop_remote_registry_watch() -> None:
    global _REMOTE_REFRESH_TASK
    if _REMOTE_REFRESH_TASK is None:
        return
    _REMOTE_REFRESH_TASK.cancel()
    try:
        await _REMOTE_REFRESH_TASK
    except asyncio.CancelledError:
        pass
    _REMOTE_REFRESH_TASK = None
