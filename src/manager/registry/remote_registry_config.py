import json
from pathlib import Path
from typing import Any, Dict, List

from .resource_gateway import RemoteRegistrySource


def load_remote_registry_sources(config_path: str) -> List[RemoteRegistrySource]:
    path = Path(config_path)
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return []

    sources = []
    for item in data.get("sources", []):
        if not isinstance(item, dict):
            continue
        sources.append(
            RemoteRegistrySource(
                name=item.get("name", "remote"),
                base_url=item.get("base_url", ""),
                server_id=item.get("server_id", item.get("name", "remote")),
                priority=int(item.get("priority", 100)),
                timeout=float(item.get("timeout", 5.0)),
                health_check=bool(item.get("health_check", False)),
            )
        )
    return sources


def load_remote_registry_settings(config_path: str) -> Dict[str, Any]:
    """Load optional settings for remote registry refresh."""
    path = Path(config_path)
    if not path.exists():
        return {"cache_ttl": 30.0}

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {"cache_ttl": 30.0}

    ttl = data.get("cache_ttl", 30.0)
    try:
        ttl = float(ttl)
    except Exception:
        ttl = 30.0
    return {"cache_ttl": max(ttl, 0.0)}
