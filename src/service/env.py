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


def _parse_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except Exception:
        logging.getLogger(__name__).warning(
            "Invalid float env value for %s=%r, fallback to default=%s",
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
S_ABAC_ENABLED = _parse_bool("S_ABAC_ENABLED", False)

# Agent memory. The deployment-level switch is authoritative; individual
# requests may opt out but cannot force memory on when it is globally disabled.
MEMORY_ENABLED = _parse_bool("MEMORY_ENABLED", True)
MEMORY_LONG_TERM_ENABLED = _parse_bool("MEMORY_LONG_TERM_ENABLED", True)
MEMORY_AUTO_COMPACT_ENABLED = _parse_bool(
    "MEMORY_AUTO_COMPACT_ENABLED", _parse_bool("MEMORY_AUTO_COMPACT", True)
)
MEMORY_COMPACTION_LLM_ENABLED = _parse_bool(
    "MEMORY_COMPACTION_LLM_ENABLED", _parse_bool("MEMORY_LLM_COMPACTION", False)
)
MEMORY_MAX_CONTEXT_TOKENS = _parse_int(
    "MEMORY_MAX_CONTEXT_TOKENS",
    _parse_int("MEMORY_CONTEXT_TOKEN_BUDGET", 32768),
)
MEMORY_RESERVED_OUTPUT_TOKENS = _parse_int("MEMORY_RESERVED_OUTPUT_TOKENS", 4096)
MEMORY_COMPACTION_TRIGGER_RATIO = _parse_float(
    "MEMORY_COMPACTION_TRIGGER_RATIO", 0.75
)
MEMORY_LONG_TERM_TOP_K = _parse_int("MEMORY_LONG_TERM_TOP_K", 5)
MEMORY_MAX_RECORD_CHARS = _parse_int("MEMORY_MAX_RECORD_CHARS", 8000)
MEMORY_STORE_DIR = os.getenv("MEMORY_STORE_DIR")
MEMORY_ALLOW_REMOTE_LONG_TERM = _parse_bool("MEMORY_ALLOW_REMOTE_LONG_TERM", False)

# Compatibility aliases for early memory prototypes.
MEMORY_AUTO_COMPACT = MEMORY_AUTO_COMPACT_ENABLED
MEMORY_LLM_COMPACTION = MEMORY_COMPACTION_LLM_ENABLED
MEMORY_CONTEXT_TOKEN_BUDGET = MEMORY_MAX_CONTEXT_TOKENS
MEMORY_COMPACTION_TRIGGER_TOKENS = int(
    (MEMORY_MAX_CONTEXT_TOKENS - MEMORY_RESERVED_OUTPUT_TOKENS)
    * MEMORY_COMPACTION_TRIGGER_RATIO
)
MEMORY_COMPACTION_TARGET_TOKENS = max(1, MEMORY_COMPACTION_TRIGGER_TOKENS // 2)
MEMORY_DB_PATH = os.getenv("MEMORY_DB_PATH")

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
