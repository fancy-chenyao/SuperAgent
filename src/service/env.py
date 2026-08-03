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
SCHEDULER_AUTO_RECOVERY_MAX_ATTEMPTS = _parse_int(
    "SCHEDULER_AUTO_RECOVERY_MAX_ATTEMPTS", 1
)
SCHEDULER_RETRY_BASE_SECONDS = _parse_float(
    "SCHEDULER_RETRY_BASE_SECONDS", 0.25
)
SCHEDULER_RETRY_MAX_SECONDS = _parse_float(
    "SCHEDULER_RETRY_MAX_SECONDS", 4.0
)
SCHEDULER_RETRY_JITTER_RATIO = _parse_float(
    "SCHEDULER_RETRY_JITTER_RATIO", 0.2
)
S_ABAC_ENABLED = _parse_bool("S_ABAC_ENABLED", False)

# Execution-engine feature flags.
# ARTIFACT_CAPTURE_ENABLED        -> Legacy publisher/while compatibility
#                                    capture. Its code default stays OFF; the
#                                    scheduler persists governed Artifacts
#                                    independently through its runtime.
# ORCHESTRATION_SCHEDULER_ENABLED -> Phase 3: drive the workflow through the
#                                    TaskGraph scheduler when the plan yields a
#                                    fully-classified task graph. The code
#                                    default stays OFF as the no-configuration
#                                    safety baseline; the prototype template
#                                    explicitly opts in. When ON, planning may
#                                    stay on the legacy publisher/while path,
#                                    while production execution FAILS CLOSED for
#                                    a missing/invalid graph or rejected
#                                    snapshot.
# SCHEDULER_REDISPATCH_ENABLED    -> after a retryable read-only step exhausts
#                                    its same-Agent budget, allow one trusted,
#                                    equivalent Agent redispatch. Default OFF.
# SCHEDULER_RETRY_DELAY_SECONDS   -> fixed delay before the bounded same-Agent
#                                    retry. Clamped to zero; no backoff policy.
ARTIFACT_CAPTURE_ENABLED = _parse_bool("ARTIFACT_CAPTURE_ENABLED", False)
ORCHESTRATION_SCHEDULER_ENABLED = _parse_bool(
    "ORCHESTRATION_SCHEDULER_ENABLED", False)
SCHEDULER_REDISPATCH_ENABLED = _parse_bool(
    "SCHEDULER_REDISPATCH_ENABLED", False)
SCHEDULER_RETRY_DELAY_SECONDS = max(
    0.0,
    _parse_float("SCHEDULER_RETRY_DELAY_SECONDS", 0.0),
)

# 意图识别：默认混合模式；Basic LLM 未配置或调用异常时由识别层自动降级为 rule。
INTENT_RECOGNITION_MODE = _parse_choice(
    "INTENT_RECOGNITION_MODE", "hybrid", {"rule", "hybrid", "semantic"}
)
INTENT_RULE_STRONG_THRESHOLD = _parse_float(
    "INTENT_RULE_STRONG_THRESHOLD", 0.82)
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
INTENT_CONTEXT_SEMANTIC_TIMEOUT_SECONDS = _parse_float(
    "INTENT_CONTEXT_SEMANTIC_TIMEOUT_SECONDS", 35.0
)

# Agent memory. The deployment-level switch is authoritative; individual
# requests may opt out but cannot force memory on when it is globally disabled.
MEMORY_ENABLED = _parse_bool("MEMORY_ENABLED", True)
MEMORY_LONG_TERM_ENABLED = _parse_bool("MEMORY_LONG_TERM_ENABLED", True)
MEMORY_AUTO_COMPACT_ENABLED = _parse_bool(
    "MEMORY_AUTO_COMPACT_ENABLED", _parse_bool("MEMORY_AUTO_COMPACT", True)
)
MEMORY_COMPACTION_LLM_ENABLED = _parse_bool(
    "MEMORY_COMPACTION_LLM_ENABLED", _parse_bool(
        "MEMORY_LLM_COMPACTION", False)
)
MEMORY_MAX_CONTEXT_TOKENS = _parse_int(
    "MEMORY_MAX_CONTEXT_TOKENS",
    _parse_int("MEMORY_CONTEXT_TOKEN_BUDGET", 32768),
)
MEMORY_RESERVED_OUTPUT_TOKENS = _parse_int(
    "MEMORY_RESERVED_OUTPUT_TOKENS", 4096)
MEMORY_COMPACTION_TRIGGER_RATIO = _parse_float(
    "MEMORY_COMPACTION_TRIGGER_RATIO", 0.75
)
MEMORY_LONG_TERM_TOP_K = _parse_int("MEMORY_LONG_TERM_TOP_K", 5)
MEMORY_MAX_RECORD_CHARS = _parse_int("MEMORY_MAX_RECORD_CHARS", 8000)
MEMORY_STORE_DIR = os.getenv("MEMORY_STORE_DIR")
MEMORY_ALLOW_REMOTE_LONG_TERM = _parse_bool(
    "MEMORY_ALLOW_REMOTE_LONG_TERM", False)

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

# Reusable workflow skills distilled from successful Production executions.
WORKFLOW_SKILL_ENABLED = _parse_bool("WORKFLOW_SKILL_ENABLED", True)
WORKFLOW_SKILL_REUSE_ENABLED = _parse_bool("WORKFLOW_SKILL_REUSE_ENABLED", True)
WORKFLOW_SKILL_AUTO_DISTILL_ENABLED = _parse_bool("WORKFLOW_SKILL_AUTO_DISTILL_ENABLED", True)
WORKFLOW_SKILL_MATCH_THRESHOLD = _parse_float("WORKFLOW_SKILL_MATCH_THRESHOLD", 0.62)
WORKFLOW_SKILL_MATCH_MARGIN = _parse_float("WORKFLOW_SKILL_MATCH_MARGIN", 0.08)
WORKFLOW_SKILL_PROMOTION_THRESHOLD = _parse_int("WORKFLOW_SKILL_PROMOTION_THRESHOLD", 2)
WORKFLOW_SKILL_FAILURE_THRESHOLD = _parse_int("WORKFLOW_SKILL_FAILURE_THRESHOLD", 2)
WORKFLOW_SKILL_DB_PATH = os.getenv("WORKFLOW_SKILL_DB_PATH")
WORKFLOW_SKILL_ADMIN_API_KEY = os.getenv("WORKFLOW_SKILL_ADMIN_API_KEY")

# Governance mutations are privileged operations. The bearer credential
# authenticates one server-configured principal; request bodies never choose
# the approver/operator identity.
GOVERNANCE_ADMIN_API_KEY = os.getenv("GOVERNANCE_ADMIN_API_KEY")
GOVERNANCE_ADMIN_ACTOR_ID = os.getenv("GOVERNANCE_ADMIN_ACTOR_ID", "admin")

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
