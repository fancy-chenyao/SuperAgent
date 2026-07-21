import os
from dotenv import load_dotenv
import logging

# 本地开发以项目 .env 为统一配置源，避免 IDE/系统同名变量（如 DEBUG）覆盖团队配置。
# 生产环境仍保留平台注入变量的优先级。
_app_env = os.getenv("APP_ENV", "development").strip().lower()
load_dotenv(override=_app_env not in {"production", "prod"})

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


def _parse_choice(name: str, default: str, choices: set[str]) -> str:
    raw = str(os.getenv(name, default)).strip().lower()
    if raw in choices:
        return raw
    logging.getLogger(__name__).warning(
        "Invalid choice env value for %s=%r, fallback to default=%s",
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

# 意图识别：默认混合模式；Basic LLM 未配置或调用异常时由识别层自动降级为 rule。
INTENT_RECOGNITION_MODE = _parse_choice(
    "INTENT_RECOGNITION_MODE", "hybrid", {"rule", "hybrid", "semantic"}
)
INTENT_RULE_STRONG_THRESHOLD = _parse_float("INTENT_RULE_STRONG_THRESHOLD", 0.82)
INTENT_SEMANTIC_ACCEPT_THRESHOLD = _parse_float(
    "INTENT_SEMANTIC_ACCEPT_THRESHOLD", 0.72
)
INTENT_SEMANTIC_HIGH_RISK_THRESHOLD = _parse_float(
    "INTENT_SEMANTIC_HIGH_RISK_THRESHOLD", 0.85
)
INTENT_AGREEMENT_BONUS = _parse_float("INTENT_AGREEMENT_BONUS", 0.06)
INTENT_CONFLICT_THRESHOLD = _parse_float("INTENT_CONFLICT_THRESHOLD", 0.75)
INTENT_SEMANTIC_TIMEOUT_SECONDS = _parse_float(
    "INTENT_SEMANTIC_TIMEOUT_SECONDS", 20.0
)

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
