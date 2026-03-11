import hashlib
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from src.manager.registry import ToolRegistry
from src.utils import get_project_root

load_dotenv()

logger = logging.getLogger(__name__)

CONFIG_FILE_PATH = str(get_project_root() / "config" / "mcp.json")
_HOT_RELOAD_MANAGER = None
_HOT_RELOAD_MANAGER_LOCK = asyncio.Lock()


def _append_key_to_url(url: str, key: Optional[str]) -> str:
    if not key:
        return url
    if "key=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}key={key}"


def normalize_mcp_servers(mcp_servers: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize raw mcpServers into MultiServerMCPClient config."""
    client_config: Dict[str, Any] = {}

    for key, value in mcp_servers.items():
        if not isinstance(value, dict):
            logger.error("Invalid config for MCP server %s: expected dict", key)
            continue

        config = value.copy()
        env_config = config.get("env", {})
        key_value = None

        if isinstance(env_config, dict):
            for env_key, env_value in env_config.items():
                if env_value is not None:
                    os.environ[env_key] = str(env_value)
                if key_value is None and env_value:
                    key_value = str(env_value)

        if "url" in config:
            config["transport"] = "sse"
            config["url"] = _append_key_to_url(str(config["url"]), key_value)
            client_config[key] = config
            continue

        if "command" in config:
            config["transport"] = "stdio"
            config.setdefault("args", [])
            client_config[key] = config
            continue

        logger.error("Cannot determine transport type for MCP server %s", key)

    return client_config


def load_mcp_servers_from_file(config_path: str = CONFIG_FILE_PATH) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        logger.warning("MCP config file not found: %s", config_path)
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.error("Error decoding MCP JSON from %s", config_path)
        return {}

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        logger.warning("Invalid MCP config structure in %s", config_path)
        return {}

    return servers


def mcp_client_config(config_path: str = CONFIG_FILE_PATH) -> Dict[str, Any]:
    servers = load_mcp_servers_from_file(config_path)
    return normalize_mcp_servers(servers)


def mcp_config_fingerprint(config_path: str = CONFIG_FILE_PATH) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        return {"hash": "", "mtime": 0.0}
    raw = path.read_bytes()
    return {
        "hash": hashlib.sha256(raw).hexdigest(),
        "mtime": path.stat().st_mtime,
    }


async def get_mcp_hot_reload_manager(config_path: str = CONFIG_FILE_PATH):
    global _HOT_RELOAD_MANAGER
    if _HOT_RELOAD_MANAGER is not None:
        return _HOT_RELOAD_MANAGER
    async with _HOT_RELOAD_MANAGER_LOCK:
        if _HOT_RELOAD_MANAGER is None:
            from src.manager.hot_reload import MCPHotReloadManager

            registry = await ToolRegistry.get_instance()
            _HOT_RELOAD_MANAGER = MCPHotReloadManager(registry=registry, config_path=config_path)
    return _HOT_RELOAD_MANAGER
