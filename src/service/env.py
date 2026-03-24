import os
from dotenv import load_dotenv
import logging

# Load environment variables
load_dotenv()

# Reasoning LLM configuration (for complex reasoning tasks)
REASONING_MODEL = os.getenv("REASONING_MODEL", "o1-mini")
REASONING_BASE_URL = os.getenv("REASONING_BASE_URL")
REASONING_API_KEY = os.getenv("REASONING_API_KEY")

# Non-reasoning LLM configuration (for straightforward tasks)
BASIC_MODEL = os.getenv("BASIC_MODEL", "gpt-4o")
BASIC_BASE_URL = os.getenv("BASIC_BASE_URL")
BASIC_API_KEY = os.getenv("BASIC_API_KEY")

# Vision-language LLM configuration (for tasks requiring visual understanding)
VL_MODEL = os.getenv("VL_MODEL", "gpt-4o")
VL_BASE_URL = os.getenv("VL_BASE_URL")
VL_API_KEY = os.getenv("VL_API_KEY")

# Chrome Instance configuration
CHROME_INSTANCE_PATH = os.getenv("CHROME_INSTANCE_PATH")

CODE_API_KEY = os.getenv("CODE_API_KEY")
CODE_BASE_URL = os.getenv("CODE_BASE_URL")
CODE_MODEL = os.getenv("CODE_MODEL")

def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    logging.getLogger(__name__).warning(
        "Invalid boolean env value for %s=%r, fallback to default=%s",
        name,
        raw,
        default,
    )
    return default


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        logging.getLogger(__name__).warning(
            "Invalid integer env value for %s=%r, fallback to default=%s",
            name,
            raw,
            default,
        )
        return default


USR_AGENT = _parse_bool("USR_AGENT", True)
MCP_AGENT = _parse_bool("MCP_AGENT", False)
USE_MCP_TOOLS = _parse_bool("USE_MCP_TOOLS", True)
USE_BROWSER = _parse_bool("USE_BROWSER", False)
DISABLE_DEFAULT_AGENTS = _parse_bool("DISABLE_DEFAULT_AGENTS", False)
DEBUG = _parse_bool("DEBUG", False)
BROWSER_BACKEND = os.getenv("BROWSER_BACKEND")
MAX_STEPS = _parse_int("MAX_STEPS", 25)
AUTO_RECOVERY_ENABLED = _parse_bool("AUTO_RECOVERY_ENABLED", False)

if not DEBUG:
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
else:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
