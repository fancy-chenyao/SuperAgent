import asyncio
import hmac
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import math

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from src.interface.agent import AgentRequest
from src.manager import agent_manager
from src.manager.registry import ToolRegistry
from src.manager.mcp import mcp_client_config, mcp_config_fingerprint
from src.service.server import Server
from src.utils.path_utils import get_project_root
from src.workflow.cache import workflow_cache
from src.robust.checkpoint import CheckpointManager
from src.robust.task_logger import TaskLogger
from src.orchestration.governance import (
    get_governance_event_store,
    record_governance_event,
)
from src.orchestration.artifact_payload_store import ArtifactPayloadStore
from src.orchestration.completion import (
    PersistentReceiptStore,
    ReceiptClaimMismatch,
    ReceiptStoreCorruption,
)
from src.orchestration.reconciliation import get_reconciliation_store
from src.security.approval import get_approval_store
from src.security.cleanup_capabilities import (
    CleanupCapabilityError,
    get_cleanup_capability_store,
)
from config.s_abac_demo_users import get_demo_user, list_demo_users, get_user_available_agents
from config.s_abac_config import (
    AGENT_SECURITY_ATTRIBUTES,
    RESOURCE_SECURITY_ATTRIBUTES,
    S_ABAC_POLICIES,
    SENSITIVITY_LEVELS,
)
from src.service.env import (
    AUTO_RECOVERY_ENABLED,
    ORCHESTRATION_SCHEDULER_ENABLED,
    S_ABAC_ENABLED,
    SCHEDULER_AUTO_RECOVERY_MAX_ATTEMPTS,
    SCHEDULER_RETRY_BASE_SECONDS,
    SCHEDULER_RETRY_MAX_SECONDS,
    SCHEDULER_RETRY_JITTER_RATIO,
    USE_MCP_TOOLS,
    WORKFLOW_SKILL_ADMIN_API_KEY,
    GOVERNANCE_ADMIN_API_KEY,
    GOVERNANCE_ADMIN_ACTOR_ID,
)
from src.memory import get_memory_manager
from src.memory.store import SecretDetectedError
from src.skills.workflow_skill import get_workflow_skill_manager
from src.skills.execution_evidence import (
    evaluate_distillation_evidence,
    load_execution_evidence,
)


class NoStoreStaticFiles(StaticFiles):
    """Serve the local demo UI without stale JavaScript/CSS caches."""

    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


def _enrich_governance_queue_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach human-readable conversation and step context to queue records."""

    run_cache: dict[str, list[dict[str, Any]]] = {}
    enriched: list[dict[str, Any]] = []
    for raw_item in items:
        item = dict(raw_item)
        task_id = str(item.get("task_id") or "")
        workflow_id = str(item.get("workflow_id") or "")
        try:
            task = TaskLogger.load(task_id) if task_id else None
        except Exception:
            # Queue records must remain operable even if an old task log is
            # missing or malformed. The technical identifiers below still let
            # an operator investigate that record.
            task = None
        if task is not None:
            item["user_query"] = str(task.user_query or "")
            item["task_created_at"] = str(task.created_at or "")
            item["execution_phase"] = str(task.execution_phase or "")
            step_id = str(item.get("step_id") or "")
            step = next(
                (
                    value
                    for value in (task.planning_steps or [])
                    if isinstance(value, dict)
                    and str(value.get("step_id") or "") == step_id
                ),
                None,
            )
            if step:
                item["step_title"] = str(
                    step.get("title") or step.get("description") or ""
                )
                item["step_intents"] = list(step.get("intents") or [])

        if workflow_id:
            if workflow_id not in run_cache:
                try:
                    runs = TaskLogger.list_tasks(
                        workflow_id=workflow_id,
                        execution_phase="execution",
                    )
                except Exception:
                    runs = []
                run_cache[workflow_id] = sorted(
                    runs,
                    key=lambda value: str(value.get("created_at") or ""),
                )
            run_ids = [
                str(value.get("task_id") or "")
                for value in run_cache[workflow_id]
            ]
            if task_id in run_ids:
                item["execution_round"] = run_ids.index(task_id) + 1
                item["execution_round_total"] = len(run_ids)

        enriched.append(item)
    return enriched


class MemoryWriteRequest(BaseModel):
    user_id: str
    content: str
    kind: str = "fact"
    memory_key: Optional[str] = None
    scope: str = "user"
    confidence: float = 1.0
    workflow_id: Optional[str] = None
    session_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryCompactRequest(BaseModel):
    user_id: str
    session_id: Optional[str] = None
    attachments: dict[str, Any] = Field(default_factory=dict)
    hook_results: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowSkillUserRequest(BaseModel):
    user_id: str


class WorkflowSkillDistillRequest(BaseModel):
    user_id: str
    task_id: str
    workflow_id: Optional[str] = None


class ApprovalDecisionRequest(BaseModel):
    comment: str = ""


class ReconciliationDecisionRequest(BaseModel):
    comment: str = ""
    external_operation_id: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)


def _governance_actor_profile(actor_id: str) -> dict[str, Any]:
    """Resolve a declared demo actor and fail closed for unknown identities."""

    profile = get_demo_user(str(actor_id or "").strip())
    if not profile:
        raise HTTPException(
            status_code=403,
            detail="unknown governance operator",
        )
    return profile


def _is_governance_reviewer(profile: dict[str, Any]) -> bool:
    grants = {str(item).lower() for item in (profile.get("grants") or [])}
    return bool(
        "all" in grants
        or "governance_review" in grants
        or str(profile.get("job_role") or "").lower() == "system_orchestrator"
    )


def _require_governance_reviewer(actor_id: str) -> dict[str, Any]:
    profile = _governance_actor_profile(actor_id)
    if not _is_governance_reviewer(profile):
        raise HTTPException(
            status_code=403,
            detail="governance reviewer permission required",
        )
    return profile


def _authenticate_governance_operator(
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Return the server-owned governance principal for a valid credential."""

    configured = str(GOVERNANCE_ADMIN_API_KEY or "")
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="governance mutation credential is not configured",
        )
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not hmac.compare_digest(supplied, configured)
    ):
        raise HTTPException(
            status_code=401,
            detail="governance mutation authentication failed",
        )
    actor_id = str(GOVERNANCE_ADMIN_ACTOR_ID or "").strip()
    _require_governance_reviewer(actor_id)
    return actor_id


def _authorize_runtime_cleanup(
    *,
    task_id: str = "",
    workflow_id: str = "",
    owner_token: str = "",
    authorization: str = "",
) -> str:
    """Accept a resource-owner capability or a governance-admin credential."""

    capabilities = get_cleanup_capability_store()
    if task_id and capabilities.authorize_task(task_id, owner_token):
        return "task_owner"
    if workflow_id and capabilities.authorize_workflow(workflow_id, owner_token):
        return "workflow_owner"
    if authorization:
        return _authenticate_governance_operator(authorization)
    raise HTTPException(
        status_code=401,
        detail="runtime cleanup authentication failed",
    )


def _bind_runtime_cleanup_capability(
    *,
    token: str,
    user_id: str,
    workflow_id: str,
    task_id: str = "",
    trusted_new_task_id: str = "",
) -> None:
    if not token or not workflow_id:
        return
    try:
        get_cleanup_capability_store().bind(
            token=token,
            user_id=user_id,
            workflow_id=workflow_id,
            task_id=task_id,
            allow_new_workflow=not _workflow_has_persisted_records(
                workflow_id,
                excluding_task_id=trusted_new_task_id,
            ),
        )
    except CleanupCapabilityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _workflow_has_persisted_records(
    workflow_id: str, *, excluding_task_id: str = ""
) -> bool:
    """Whether a workflow predates the requesting cleanup capability."""

    excluded = str(excluding_task_id or "")
    for task in TaskLogger.list_tasks(workflow_id=workflow_id):
        if str(task.get("task_id") or "") != excluded:
            return True
    for approval in get_approval_store().list(workflow_id=workflow_id):
        if str(approval.get("task_id") or "") != excluded:
            return True
    for reconciliation in get_reconciliation_store().list():
        if (
            str(reconciliation.get("workflow_id") or "") == workflow_id
            and str(reconciliation.get("task_id") or "") != excluded
        ):
            return True
    return False


def _task_record_owner_ids(task_id: str) -> set[str]:
    """Collect trusted owners from persisted task and governance records."""

    owners: set[str] = set()
    task = TaskLogger.load(task_id)
    if task is not None:
        owner, _ = _parse_workflow_id(str(task.workflow_id or ""))
        if owner:
            owners.add(owner)
    for item in get_approval_store().list(task_id=task_id):
        if item.get("user_id"):
            owners.add(str(item["user_id"]))
    for item in get_reconciliation_store().list(task_id=task_id):
        if item.get("user_id"):
            owners.add(str(item["user_id"]))
    return owners


def _authorize_task_cleanup(task_id: str, actor_id: str) -> None:
    profile = _governance_actor_profile(actor_id)
    if _is_governance_reviewer(profile):
        return
    owners = _task_record_owner_ids(task_id)
    if not owners or str(actor_id) not in owners:
        raise HTTPException(
            status_code=403,
            detail="task does not belong to the selected user",
        )


def _static_resource_precheck(
    profile: dict[str, Any],
    attrs: Optional[dict[str, Any]],
    *,
    resource_name: str,
    operation_mode: str = "",
) -> dict[str, Any]:
    """Evaluate the static subset of S-ABAC without claiming runtime approval.

    Scenario fit and concrete invocation arguments remain runtime decisions;
    this helper makes the dashboard honest about roles, job roles, grants,
    clearance, operation modes and mandatory review.
    """

    if not isinstance(attrs, dict) or not attrs:
        return {
            "allowed": False,
            "can_access": False,
            "eligible": False,
            "decision": "DENY",
            "review_required": False,
            "blocked_reason": f"Unregistered resource: {resource_name}",
            "resource_registered": False,
        }

    user_role = str(profile.get("role") or "")
    user_job_role = str(profile.get("job_role") or "")
    user_clearance = int(profile.get("clearance_level") or 0)
    user_grants = {str(item) for item in (profile.get("grants") or [])}
    allowed_roles = [str(item) for item in (attrs.get("allowed_roles") or [])]
    allowed_job_roles = [
        str(item) for item in (attrs.get("allowed_job_roles") or [])
    ]
    required_grants = [
        str(item) for item in (attrs.get("grants_required") or [])
    ]
    sensitivity = str(attrs.get("sensitivity") or "LOW").upper()
    allowed_modes = {
        str(item).lower()
        for item in (attrs.get("allowed_operation_modes") or [])
        if str(item).lower() != "delegate"
    }

    role_match = not allowed_roles or user_role in allowed_roles
    job_role_match = not allowed_job_roles or user_job_role in allowed_job_roles
    grants_match = "all" in user_grants or set(required_grants).issubset(user_grants)
    clearance_match = user_clearance >= SENSITIVITY_LEVELS.get(sensitivity, 1)
    mode_match = (
        not operation_mode
        or not allowed_modes
        or str(operation_mode).lower() in allowed_modes
    )

    blockers: list[str] = []
    if not role_match:
        blockers.append(f"Role {user_role} not in {allowed_roles}")
    if not job_role_match:
        blockers.append(f"Job role {user_job_role} not in {allowed_job_roles}")
    if not grants_match:
        blockers.append(f"Missing grants {required_grants}")
    if not clearance_match:
        blockers.append(
            f"Clearance L{user_clearance} below {sensitivity} "
            f"(needs L{SENSITIVITY_LEVELS.get(sensitivity, 1)})"
        )
    if not mode_match:
        blockers.append(
            f"Operation mode {operation_mode} not in {sorted(allowed_modes)}"
        )

    eligible = not blockers
    bypass_review = _is_governance_reviewer(profile)
    review_required = eligible and not bypass_review and bool(
        attrs.get("requires_approval") or attrs.get("irreversible")
    )
    decision = (
        "DENY"
        if not eligible
        else "REVIEW_REQUIRED"
        if review_required
        else "ALLOW"
    )
    return {
        "allowed": decision == "ALLOW",
        "can_access": decision == "ALLOW",
        "eligible": eligible,
        "decision": decision,
        "review_required": review_required,
        "blocked_reason": "; ".join(blockers),
        "resource_registered": True,
        "sensitivity": sensitivity,
        "allowed_roles": allowed_roles,
        "allowed_job_roles": allowed_job_roles,
        "required_grants": required_grants,
        "allowed_operation_modes": sorted(allowed_modes),
        "role_match": role_match,
        "job_role_match": job_role_match,
        "grants_match": grants_match,
        "clearance_match": clearance_match,
        "operation_mode_match": mode_match,
        "scenario_dependent": True,
    }


def _authorize_workflow_skill_api(
    authorization: Optional[str] = Header(default=None),
) -> None:
    configured = WORKFLOW_SKILL_ADMIN_API_KEY or ""
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="工作流技能管理 API 尚未配置管理员凭据",
        )
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not hmac.compare_digest(supplied, configured)
    ):
        raise HTTPException(status_code=401, detail="工作流技能管理 API 认证失败")


def _normalize_workflow_steps(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = raw.get("steps") or raw.get("planning_steps") or []
    if not isinstance(raw, list):
        return []
    return [step for step in raw if isinstance(step, dict)]


def _sse_format(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    lines = payload.splitlines() or [""]
    message = f"event: {event}\n"
    for line in lines:
        message += f"data: {line}\n"
    message += "\n"
    return message


def _finalize_disconnected_task(task_id: Optional[str], reason: str) -> None:
    """Best-effort close a task whose SSE consumer disappeared."""
    if not task_id:
        return
    task_log = TaskLogger.load(task_id)
    if task_log is None or task_log.status != "running":
        return
    task_log.log_workflow_terminal("FAILED", error=reason)


def _delete_task_runtime_records(task_id: str) -> dict[str, int]:
    """Delete one task's operational history without deleting business outputs.

    Generated documents and simulated external-system records are deliberately
    preserved. This cleanup covers only the conversation/task execution state
    that would otherwise leave orphan cards in Task History or Security.
    """

    normalized = str(task_id or "").strip()
    if not normalized:
        raise ValueError("task_id is required")
    counts = {
        "task_logs": 0,
        "checkpoints": 0,
        "receipts": 0,
        "artifacts": 0,
        "governance_events": 0,
        "approvals": 0,
        "reconciliations": 0,
    }

    task_log = TaskLogger.load(normalized)
    if task_log is not None:
        try:
            task_log._log_file.unlink()
            counts["task_logs"] = 1
        except FileNotFoundError:
            pass

    checkpoint_manager = CheckpointManager()
    checkpoint_root = checkpoint_manager.base_dir.resolve()
    checkpoint_dir = (checkpoint_root / normalized).resolve()
    if checkpoint_dir.parent != checkpoint_root:
        raise ValueError("task_id resolves outside checkpoint store")
    if checkpoint_dir.exists():
        shutil.rmtree(checkpoint_dir)
        counts["checkpoints"] = 1

    receipt_store = PersistentReceiptStore(normalized)
    if receipt_store._path.exists():
        receipt_store._path.unlink()
        counts["receipts"] = 1

    artifact_store = ArtifactPayloadStore(normalized)
    artifact_dir_existed = artifact_store._dir.exists() and any(
        artifact_store._dir.iterdir()
    )
    artifact_store.clear()
    counts["artifacts"] = int(artifact_dir_existed)

    counts["governance_events"] = int(
        get_governance_event_store().delete(normalized)
    )
    counts["approvals"] = get_approval_store().delete(task_id=normalized)
    counts["reconciliations"] = get_reconciliation_store().delete(
        task_id=normalized
    )
    return counts


def _parse_workflow_id(workflow_id: str) -> tuple[str, str]:
    if ":" not in workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id must be in 'user:polish' format")
    user_id, polish_id = workflow_id.split(":", 1)
    if not user_id or not polish_id:
        raise HTTPException(status_code=400, detail="workflow_id must be in 'user:polish' format")
    return user_id, polish_id


def _read_mermaid_from_md(md_text: str) -> Optional[str]:
    start_tag = "```mermaid"
    end_tag = "```"
    start_idx = md_text.find(start_tag)
    if start_idx == -1:
        return None
    start_idx += len(start_tag)
    end_idx = md_text.find(end_tag, start_idx)
    if end_idx == -1:
        return None
    return md_text[start_idx:end_idx].strip()


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _collect_workflow_files(base_dir: Path, user_id: str) -> list[Path]:
    user_dir = base_dir / user_id
    if not user_dir.exists():
        return []
    return [p for p in user_dir.glob("*.json") if p.is_file()]


def _workflow_last_used(workflow: dict) -> Optional[datetime]:
    messages = workflow.get("user_input_messages", []) or []
    latest = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        ts = _parse_timestamp(msg.get("timestamp"))
        if ts and (latest is None or ts > latest):
            latest = ts
    for field in ("updated_at", "created_at", "file_updated_at"):
        ts = _parse_timestamp(workflow.get(field))
        if ts and (latest is None or ts > latest):
            latest = ts
    return latest


def _workflow_last_used_score(workflow: dict) -> float:
    dt = _workflow_last_used(workflow)
    if dt is None:
        return float("-inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp()
    except Exception:
        return float("-inf")


class PlanningStepsRequest(BaseModel):
    workflow_id: str
    planning_steps: list[dict[str, Any]]


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return None


def _extract_tool_name(tool_entry: Any) -> str:
    if isinstance(tool_entry, str):
        return tool_entry
    if isinstance(tool_entry, dict):
        name = tool_entry.get("name") or tool_entry.get("tool") or tool_entry.get("tool_name")
        if name:
            return str(name)
        config = tool_entry.get("config") or {}
        if isinstance(config, dict):
            name = config.get("name") or config.get("tool") or config.get("tool_name")
            if name:
                return str(name)
    return ""


def _extract_tools_from_node(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    config = node.get("config") or {}
    tools = None
    if isinstance(config, dict):
        tools = config.get("tools")
    if tools is None:
        tools = node.get("tools")
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for tool in tools:
        name = _extract_tool_name(tool)
        if name:
            names.append(name)
    return names


def _extract_tools_from_workflow(workflow: dict) -> list[str]:
    names: list[str] = []
    graph = workflow.get("graph")
    if isinstance(graph, list):
        for node in graph:
            names.extend(_extract_tools_from_node(node))
    nodes = workflow.get("nodes")
    if isinstance(nodes, dict):
        for node in nodes.values():
            names.extend(_extract_tools_from_node(node))
    return names


def _get_args_schema(tool: Any) -> Optional[dict[str, Any]]:
    args_schema = getattr(tool, "args_schema", None)
    if args_schema is None:
        return None
    try:
        if hasattr(args_schema, "model_json_schema"):
            return args_schema.model_json_schema()
        if hasattr(args_schema, "schema"):
            return args_schema.schema()
    except Exception:
        return None
    return None


def _count_schema_params(schema: Optional[dict[str, Any]]) -> dict[str, int]:
    """Count required and total parameters from schema."""
    if not schema or not isinstance(schema, dict):
        return {"total": 0, "required": 0}

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    return {
        "total": len(properties),
        "required": len(required)
    }


def _build_health_fallback(endpoint: str) -> Optional[str]:
    try:
        url = httpx.URL(endpoint)
    except Exception:
        return None
    if not url.scheme or not url.host:
        return None
    base = f"{url.scheme}://{url.host}"
    if url.port:
        base = f"{base}:{url.port}"
    return f"{base}/health"


def create_app() -> FastAPI:
    app = FastAPI(title="CoorAgent Web", version="0.1.0")

    project_root = get_project_root()
    web_dir = project_root / "web"
    if not web_dir.exists():
        web_dir.mkdir(parents=True, exist_ok=True)

    app.mount("/static", NoStoreStaticFiles(directory=str(web_dir)), name="static")

    @app.get("/")
    async def index():
        index_path = web_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="index.html not found")
        return FileResponse(index_path)

    @app.get("/api/health/ready")
    async def readiness():
        """Return secret-free readiness details without making optional services fatal."""
        from src.llm.llm import get_llm_configuration_status
        from src.tools.search import get_search_status

        model_status = get_llm_configuration_status()
        search_status = get_search_status()

        try:
            await asyncio.wait_for(agent_manager.ensure_initialized(), timeout=15.0)
            agent_status = {
                "ready": True,
                "status": "ready",
                "count": len(agent_manager.available_agents),
                "error": None,
            }
        except asyncio.TimeoutError:
            agent_status = {
                "ready": False,
                "status": "timeout",
                "count": len(agent_manager.available_agents),
                "error": "Agent initialization exceeded 15 seconds",
            }
        except Exception as exc:
            agent_status = {
                "ready": False,
                "status": "error",
                "count": len(agent_manager.available_agents),
                "error": str(exc),
            }

        try:
            mcp_servers = (
                mcp_client_config()
                if USE_MCP_TOOLS and mcp_client_config
                else {}
            )
            mcp_status = {
                "enabled": USE_MCP_TOOLS,
                "configured": bool(mcp_servers),
                "server_count": len(mcp_servers),
                "status": (
                    "configured"
                    if mcp_servers
                    else "not_configured"
                    if USE_MCP_TOOLS
                    else "disabled"
                ),
            }
        except Exception as exc:
            mcp_status = {
                "enabled": False,
                "configured": False,
                "server_count": 0,
                "status": "error",
                "error": str(exc),
            }

        ready = bool(model_status["configured"] and agent_status["ready"])
        return {
            "ready": ready,
            "status": "ready" if ready else "degraded",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "components": {
                "web": {"ready": True, "status": "ready"},
                "models": model_status,
                "agents": agent_status,
                "search": search_status,
                "mcp": mcp_status,
            },
        }

    @app.post("/api/workflows/run")
    async def run_workflow(request: Request, body: AgentRequest):
        server = Server()
        cleanup_token = str(request.headers.get("X-Task-Owner-Token") or "")
        if body.workflow_id:
            _bind_runtime_cleanup_capability(
                token=cleanup_token,
                user_id=body.user_id,
                workflow_id=body.workflow_id,
            )

        async def event_stream() -> AsyncGenerator[str, None]:
            active_task_id: Optional[str] = None
            bound_records: set[tuple[str, str]] = set()
            disconnected = False
            try:
                async for event in server._run_agent_workflow(body):
                    event_data = event.get("data") or {}
                    active_task_id = event_data.get("task_id") or active_task_id
                    active_workflow_id = str(
                        event_data.get("workflow_id") or body.workflow_id or ""
                    )
                    binding = (active_workflow_id, str(active_task_id or ""))
                    if (
                        cleanup_token
                        and active_workflow_id
                        and (body.workflow_id or active_task_id)
                        and binding not in bound_records
                    ):
                        _bind_runtime_cleanup_capability(
                            token=cleanup_token,
                            user_id=body.user_id,
                            workflow_id=active_workflow_id,
                            task_id=str(active_task_id or ""),
                            trusted_new_task_id=str(active_task_id or ""),
                        )
                        bound_records.add(binding)
                    if await request.is_disconnected():
                        disconnected = True
                        break
                    event_type = event.get("event", "message")
                    yield _sse_format(event_type, event)
            except asyncio.CancelledError:
                disconnected = True
            finally:
                if disconnected:
                    _finalize_disconnected_task(
                        active_task_id,
                        "client disconnected before workflow completion",
                    )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.get("/api/memory/long-term")
    async def list_long_term_memory(
        user_id: str,
        query: Optional[str] = None,
    ):
        manager = get_memory_manager()
        results = await manager.list_long_term(user_id, query=query)
        return [item.to_dict() for item in results]

    @app.post("/api/memory/long-term")
    async def write_long_term_memory(body: MemoryWriteRequest):
        manager = get_memory_manager()
        try:
            record = await manager.remember(
                user_id=body.user_id,
                content=body.content,
                kind=body.kind,
                memory_key=body.memory_key,
                scope=body.scope,
                confidence=body.confidence,
                workflow_id=body.workflow_id,
                session_id=body.session_id,
                expires_at=body.expires_at,
                provenance={"source": "memory_api", "actor": body.user_id},
                metadata=body.metadata,
            )
            return record.to_dict()
        except SecretDetectedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/memory/long-term/{memory_id}")
    async def delete_long_term_memory(memory_id: str, user_id: str):
        deleted = await get_memory_manager().forget(user_id, memory_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"result": "deleted", "memory_id": memory_id}

    @app.get("/api/memory/session")
    async def list_session_memory(user_id: str, session_id: Optional[str] = None):
        messages = await get_memory_manager().list_session_messages(
            user_id, session_id=session_id
        )
        return [message.to_dict() for message in messages]

    @app.post("/api/memory/compact")
    async def compact_session_memory(body: MemoryCompactRequest):
        try:
            record = await get_memory_manager().compact_session(
                user_id=body.user_id,
                session_id=body.session_id,
                attachments=body.attachments,
                hook_results=body.hook_results,
            )
            return record.to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/agents")
    async def list_agents(user_id: Optional[str] = None, match: Optional[str] = None):
        await agent_manager.ensure_initialized()
        agents = await agent_manager.agent_registry.list(user_id=user_id, match=match)
        return [agent.model_dump() for agent in agents]

    @app.get("/api/agents/default")
    async def list_default_agents():
        await agent_manager.ensure_initialized()
        agents = await agent_manager.agent_registry.list(user_id="share")
        return [agent.model_dump() for agent in agents]

    @app.get("/api/agents/health")
    async def get_agents_health(
        user_id: Optional[str] = None,
        include_share: bool = True,
        agent_names: Optional[str] = None,
    ):
        await agent_manager.ensure_initialized()
        users = []
        if user_id:
            users.append(user_id)
        if include_share or not users:
            if "share" not in users:
                users.append("share")

        agents = []
        for uid in users:
            agents.extend(await agent_manager.agent_registry.list(user_id=uid))
        if agent_names:
            wanted = {name.strip() for name in agent_names.split(",") if name.strip()}
            if wanted:
                agents = [agent for agent in agents if agent.agent_name in wanted]

        async def _probe(agent, client: httpx.AsyncClient):
            if getattr(agent, "source", None) != "remote":
                return agent.agent_name, {"status": "local", "latency_ms": None, "error": None}
            endpoint = getattr(agent, "endpoint", None)
            if not endpoint:
                return agent.agent_name, {"status": "unknown", "latency_ms": None, "error": "missing endpoint"}

            async def _check_url(url: str):
                check_start = time.perf_counter()
                resp = await client.get(url)
                latency = int((time.perf_counter() - check_start) * 1000)
                return resp, latency

            start = time.perf_counter()
            try:
                resp, latency = await _check_url(endpoint)
                if 200 <= resp.status_code < 300:
                    return agent.agent_name, {"status": "ok", "latency_ms": latency, "error": None}

                if resp.status_code in {404, 405}:
                    fallback = _build_health_fallback(endpoint)
                    if fallback:
                        try:
                            fallback_resp, fallback_latency = await _check_url(fallback)
                            if 200 <= fallback_resp.status_code < 300:
                                return agent.agent_name, {
                                    "status": "ok",
                                    "latency_ms": fallback_latency,
                                    "error": None,
                                }
                            return agent.agent_name, {
                                "status": "fail",
                                "latency_ms": fallback_latency,
                                "error": f"HTTP {resp.status_code} @ endpoint, HTTP {fallback_resp.status_code} @ /health",
                            }
                        except Exception as exc:
                            latency = int((time.perf_counter() - start) * 1000)
                            return agent.agent_name, {
                                "status": "fail",
                                "latency_ms": latency,
                                "error": f"HTTP {resp.status_code} @ endpoint, fallback error: {exc}",
                            }

                return agent.agent_name, {
                    "status": "fail",
                    "latency_ms": latency,
                    "error": f"HTTP {resp.status_code}",
                }
            except Exception as exc:
                fallback = _build_health_fallback(endpoint)
                if fallback:
                    try:
                        fallback_resp, fallback_latency = await _check_url(fallback)
                        if 200 <= fallback_resp.status_code < 300:
                            return agent.agent_name, {
                                "status": "ok",
                                "latency_ms": fallback_latency,
                                "error": None,
                            }
                        return agent.agent_name, {
                            "status": "fail",
                            "latency_ms": fallback_latency,
                            "error": f"endpoint error: {exc}, HTTP {fallback_resp.status_code} @ /health",
                        }
                    except Exception:
                        pass
                latency = int((time.perf_counter() - start) * 1000)
                return agent.agent_name, {"status": "fail", "latency_ms": latency, "error": str(exc)}

        async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
            results = await asyncio.gather(*[_probe(agent, client) for agent in agents])
        payload = {name: info for name, info in results}
        return {"agents": payload, "scope": {"users": users}}

    @app.get("/api/agents/stats")
    async def get_agents_stats(user_id: Optional[str] = None, include_share: bool = True):
        project_root = get_project_root()
        workflows_dir = project_root / "store" / "workflows"
        users = []
        if user_id:
            users.append(user_id)
        if include_share or not users:
            if "share" not in users:
                users.append("share")

        stats: dict[str, dict[str, Any]] = {}
        workflows_scanned = 0
        for uid in users:
            for workflow_path in _collect_workflow_files(workflows_dir, uid):
                try:
                    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                workflows_scanned += 1
                workflow_last = _workflow_last_used(workflow)
                graph = workflow.get("graph", []) or []
                for node in graph:
                    if not isinstance(node, dict):
                        continue
                    config = node.get("config") or {}
                    if config.get("node_type") != "execution_agent":
                        continue
                    agent_name = node.get("name") or config.get("node_name")
                    if not agent_name:
                        continue
                    slot = stats.setdefault(agent_name, {"runs": 0, "last_used": None})
                    slot["runs"] += 1
                    if workflow_last:
                        prev = _parse_timestamp(slot["last_used"])
                        if prev is None or workflow_last > prev:
                            slot["last_used"] = _format_datetime(workflow_last)

        return {
            "agents": stats,
            "scope": {"users": users, "workflows_scanned": workflows_scanned},
        }

    @app.get("/api/tools")
    async def list_tools():
        await agent_manager.ensure_initialized()
        registry = await ToolRegistry.get_instance()
        tools = await registry.list_global_tools()
        result = []
        for meta in tools:
            name = meta.identifier.name or getattr(meta.tool, "name", "")
            if not name:
                continue
            schema = _get_args_schema(meta.tool)
            params_count = _count_schema_params(schema)
            result.append({
                "name": name,
                "description": meta.description or getattr(meta.tool, "description", ""),
                "scope": meta.identifier.scope,
                "server": meta.identifier.server,
                "version": meta.version,
                "tags": meta.tags,
                "is_mcp": meta.identifier.is_mcp,
                "params_count": params_count,
            })
        return result

    @app.get("/api/tools/{tool_name}")
    async def get_tool_detail(tool_name: str):
        await agent_manager.ensure_initialized()
        registry = await ToolRegistry.get_instance()
        tools = await registry.list_all_tools()
        matches = []
        for meta in tools:
            identifier_name = meta.identifier.name
            runtime_name = getattr(meta.tool, "name", "")
            if identifier_name == tool_name or runtime_name == tool_name:
                matches.append(meta)
        if not matches:
            raise HTTPException(status_code=404, detail="tool not found")

        matches.sort(
            key=lambda m: (
                m.identifier.scope != "global",
                m.identifier.server != "builtin",
            )
        )
        meta = matches[0]
        tool_obj = meta.tool
        return {
            "name": meta.identifier.name or getattr(tool_obj, "name", ""),
            "description": meta.description or getattr(tool_obj, "description", ""),
            "identifier": {
                "scope": meta.identifier.scope,
                "server": meta.identifier.server,
                "name": meta.identifier.name,
                "is_mcp": meta.identifier.is_mcp,
            },
            "scope": meta.identifier.scope,
            "server": meta.identifier.server,
            "version": meta.version,
            "tags": meta.tags,
            "is_mcp": meta.identifier.is_mcp,
            "args_schema": _get_args_schema(tool_obj),
        }

    @app.get("/api/tools/stats")
    async def get_tools_stats(user_id: Optional[str] = None, include_share: bool = True):
        project_root = get_project_root()
        workflows_dir = project_root / "store" / "workflows"
        users: list[str] = []
        if user_id:
            users.append(user_id)
        if include_share or not users:
            if "share" not in users:
                users.append("share")

        stats: dict[str, dict[str, Any]] = {}
        workflows_scanned = 0
        for uid in users:
            for workflow_path in _collect_workflow_files(workflows_dir, uid):
                try:
                    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                workflows_scanned += 1
                workflow_last = _workflow_last_used(workflow)
                tool_names = set(_extract_tools_from_workflow(workflow))
                for tool_name in tool_names:
                    slot = stats.setdefault(tool_name, {"workflows": 0, "last_used": None})
                    slot["workflows"] += 1
                    if workflow_last:
                        prev = slot["last_used"]
                        if prev is None or workflow_last > prev:
                            slot["last_used"] = workflow_last

        payload = {
            name: {
                "workflows": slot["workflows"],
                "last_used": _format_datetime(slot["last_used"]),
            }
            for name, slot in stats.items()
        }
        return {"tools": payload, "scope": {"users": users, "workflows_scanned": workflows_scanned}}

    @app.get("/api/tools/mcp")
    async def get_mcp_tools_config():
        config = mcp_client_config()
        fingerprint = mcp_config_fingerprint()
        servers = []
        for name, cfg in config.items():
            if not isinstance(cfg, dict):
                continue
            servers.append(
                {
                    "name": name,
                    "transport": cfg.get("transport"),
                    "url": cfg.get("url"),
                    "command": cfg.get("command"),
                    "args": cfg.get("args"),
                }
            )
        return {"servers": servers, "fingerprint": fingerprint}

    @app.get("/api/workflows")
    async def list_workflows(
        response: Response,
        user_id: Optional[str] = None,
        match: Optional[str] = None,
        page: int = 1,
        page_size: int = 5,
    ):
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        if page < 1:
            raise HTTPException(status_code=400, detail="page must be >= 1")
        allowed_page_sizes = {5, 10, 20}
        if page_size not in allowed_page_sizes:
            raise HTTPException(status_code=400, detail="page_size must be one of 5, 10, 20")

        workflows = await Server._list_workflow_json(user_id=user_id, match=match)

        workflows.sort(key=lambda wf: str(wf.get("workflow_id") or ""))
        workflows.sort(key=_workflow_last_used_score, reverse=True)

        total = len(workflows)
        total_pages = math.ceil(total / page_size) if total else 0

        start = (page - 1) * page_size
        end = start + page_size
        paged = workflows[start:end] if start < total else []

        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Page"] = str(page)
        response.headers["X-Page-Size"] = str(page_size)
        response.headers["X-Total-Pages"] = str(total_pages)

        return paged

    @app.get("/api/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str):
        user_id, polish_id = _parse_workflow_id(workflow_id)
        workflow_path = get_project_root() / "store" / "workflows" / user_id / f"{polish_id}.json"
        if not workflow_path.exists():
            raise HTTPException(status_code=404, detail="workflow not found")
        return json.loads(workflow_path.read_text(encoding="utf-8"))

    @app.get("/api/workflows/{workflow_id}/mermaid")
    async def get_workflow_mermaid(workflow_id: str):
        user_id, polish_id = _parse_workflow_id(workflow_id)
        workflows_dir = get_project_root() / "store" / "workflows" / user_id
        md_path = workflows_dir / f"{polish_id}_visualization.md"

        if not md_path.exists():
            workflow_cache._load_workflow(user_id)
            if workflow_id in workflow_cache.cache:
                workflow_cache._save_mermaid(workflow_id)

        if not md_path.exists():
            raise HTTPException(status_code=404, detail="mermaid visualization not found")

        md_text = md_path.read_text(encoding="utf-8")
        mermaid = _read_mermaid_from_md(md_text)
        if not mermaid:
            raise HTTPException(status_code=404, detail="mermaid block not found")
        return PlainTextResponse(mermaid, media_type="text/plain")

    # ---- Tasks (task execution instances) API ----

    @app.get("/api/tasks")
    async def list_tasks(
        workflow_id: Optional[str] = None,
        execution_phase: Optional[str] = None
    ):
        """
        List all task execution instances, optionally filtered by workflow_id and execution_phase.
        
        Args:
            workflow_id: Filter by workflow ID
            execution_phase: Filter by execution phase ("initial_planning" | "re_planning" | "execution")
        """
        return TaskLogger.list_tasks(workflow_id=workflow_id, execution_phase=execution_phase)

    @app.get("/api/tasks/{task_id}/log")
    async def get_task_log(task_id: str):
        """Get the full structured log for a task execution."""
        task_log = TaskLogger.load(task_id)
        if task_log is None:
            raise HTTPException(status_code=404, detail="Task log not found")
        return task_log.to_dict()

    @app.get("/api/tasks/{task_id}/checkpoints")
    async def list_task_checkpoints(task_id: str):
        """List all checkpoints saved for a task execution."""
        checkpoint_manager = CheckpointManager()
        checkpoints = checkpoint_manager.list_checkpoints(task_id=task_id)
        return checkpoints

    @app.get("/api/tasks/{task_id}/governance")
    async def list_task_governance_events(
        task_id: str,
        event_type: Optional[str] = None,
        step_id: Optional[str] = None,
        _operator: str = Depends(_authenticate_governance_operator),
    ):
        """Return the append-only governance timeline for one task."""
        return get_governance_event_store().list(
            task_id,
            event_type=event_type.upper() if event_type else None,
            step_id=step_id,
        )

    @app.get("/api/tasks/{task_id}/checkpoints/{step}")
    async def get_checkpoint_detail(task_id: str, step: int):
        """Get the full checkpoint data for a specific step."""
        checkpoint_manager = CheckpointManager()
        try:
            checkpoint = checkpoint_manager.load_checkpoint(task_id=task_id, step=step)
            return checkpoint.to_dict()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Checkpoint not found for step {step}")

    @app.post("/api/tasks/resume")
    async def resume_task(request: Request, body: "ResumeRequest"):
        """
        Resume a task execution from a specific step.
        The resume_step indicates the step to START executing (not the checkpoint step).
        So resume_step=5 means: load checkpoint from step 4, then execute step 5.
        resume_step must be >= 1.
        Streams SSE events like the normal run endpoint.
        Configuration is restored from checkpoint.
        """
        from src.robust.checkpoint import CheckpointManager
        from src.interface.agent import AgentMessage

        # resume_step indicates the step to START executing
        # We need to load checkpoint from (resume_step - 1)
        if body.resume_step < 1:
            raise HTTPException(
                status_code=400,
                detail="resume_step must be >= 1. Step 0 is the initial state, use step 1 to resume from the beginning."
            )

        checkpoint_manager = CheckpointManager()

        # Load checkpoint from (resume_step - 1) to get the state before the target step
        checkpoint_step = body.resume_step - 1
        checkpoint = checkpoint_manager.load_checkpoint(
            task_id=body.task_id, step=checkpoint_step
        )

        # Load step=0 checkpoint to get initial messages
        checkpoint_0 = checkpoint_manager.load_checkpoint(
            task_id=body.task_id, step=0
        )
        initial_messages = checkpoint_0.state.get("messages", [])

        if not initial_messages:
            initial_messages = [{"role": "user", "content": "(resume)"}]

        # Resume should use production mode to ensure cache.queue is initialized
        # Or restore from checkpoint if available
        workmode = checkpoint.state.get("workflow_mode", "production")
        if workmode not in ["production", "launch"]:
            workmode = "production"

        agent_request = AgentRequest(
            user_id=body.user_id,
            lang=body.lang,
            workmode=workmode,
            messages=[AgentMessage(role=m["role"], content=m["content"]) for m in initial_messages],
            debug=checkpoint.state.get("debug", False),
            deep_thinking_mode=checkpoint.state.get("deep_thinking_mode", True),
            search_before_planning=checkpoint.state.get("search_before_planning", False),
            coor_agents=checkpoint.state.get("coor_agents", []),
            workflow_id=checkpoint.workflow_id,
        )

        try:
            resumed_reconciliation = get_reconciliation_store().claim_for_resume(
                task_id=body.task_id,
                resume_step=body.resume_step,
                operator=body.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        server = Server()

        async def event_stream() -> AsyncGenerator[str, None]:
            active_task_id: Optional[str] = body.task_id
            disconnected = False
            reconciled_step_succeeded = bool(
                resumed_reconciliation
                and resumed_reconciliation.resolution.get("resume_from_status")
                == "confirmed_succeeded"
            )
            try:
                async for event in server._run_agent_workflow_with_resume(
                    agent_request, resume_step=body.resume_step, task_id=body.task_id
                ):
                    event_data = event.get("data") or {}
                    active_task_id = event_data.get("task_id") or active_task_id
                    if (
                        resumed_reconciliation
                        and event.get("event") == "step_result"
                        and event_data.get("step_id") == resumed_reconciliation.step_id
                        and str(event_data.get("status") or "").upper() == "SUCCEEDED"
                    ):
                        reconciled_step_succeeded = True
                    if await request.is_disconnected():
                        disconnected = True
                        break
                    event_type = event.get("event", "message")
                    yield _sse_format(event_type, event)
            except asyncio.CancelledError:
                disconnected = True
            finally:
                if resumed_reconciliation is not None:
                    try:
                        get_reconciliation_store().finish_resume(
                            resumed_reconciliation.reconciliation_id,
                            succeeded=reconciled_step_succeeded,
                        )
                    except Exception:
                        logging.exception(
                            "failed to finalize reconciliation resume %s",
                            resumed_reconciliation.reconciliation_id,
                        )
                if disconnected:
                    _finalize_disconnected_task(
                        active_task_id,
                        "client disconnected before resumed workflow completion",
                    )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(
        task_id: str,
        authorization: Optional[str] = Header(default=None),
        x_task_owner_token: Optional[str] = Header(default=None),
    ):
        """
        Delete a task's operational history and Security queue records.
        """
        _authorize_runtime_cleanup(
            task_id=task_id,
            owner_token=str(x_task_owner_token or ""),
            authorization=str(authorization or ""),
        )
        try:
            deleted = _delete_task_runtime_records(task_id)
            if not any(deleted.values()):
                raise HTTPException(
                    status_code=404,
                    detail=f"Task runtime records not found: {task_id}",
                )
            get_cleanup_capability_store().delete_task_binding(task_id)
            return {
                "result": "success",
                "task_id": task_id,
                "deleted": deleted,
                "business_outputs_preserved": True,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete task: {str(e)}")

    @app.delete("/api/conversation-history")
    async def delete_conversation_history(
        workflow_id: str,
        authorization: Optional[str] = Header(default=None),
        x_task_owner_token: Optional[str] = Header(default=None),
    ):
        """Remove orphan Security records for a deleted browser conversation.

        A workflow id can be reused by many executions, so task runtime records
        are deleted only through the exact ``/api/tasks/{task_id}`` endpoint.
        """

        owner_id, _ = _parse_workflow_id(workflow_id)
        if not owner_id:
            raise HTTPException(status_code=400, detail="invalid workflow id")
        _authorize_runtime_cleanup(
            workflow_id=workflow_id,
            owner_token=str(x_task_owner_token or ""),
            authorization=str(authorization or ""),
        )

        # Older browser history entries did not persist exact task ids. Remove
        # their workflow-scoped queue cards, but do not guess which task logs
        # the user intended to delete.
        totals = {
            "reconciliations": get_reconciliation_store().delete(
                workflow_id=workflow_id,
            ),
            "approvals": get_approval_store().delete(
                workflow_id=workflow_id,
            ),
        }
        get_cleanup_capability_store().delete_workflow_binding(workflow_id)

        return {
            "result": "success",
            "workflow_id": workflow_id,
            "deleted_tasks": 0,
            "deleted": totals,
            "business_outputs_preserved": True,
        }

    # ---- Workflow skill administration API ----

    @app.get("/api/workflow-skills")
    def list_workflow_skills(
        user_id: str,
        include_shared: bool = True,
        _authorized: None = Depends(_authorize_workflow_skill_api),
    ):
        cards = get_workflow_skill_manager().store.list(
            user_id,
            include_shared=include_shared,
        )
        return [card.model_dump(mode="json") for card in cards]

    @app.get("/api/workflow-skills/evidence")
    def list_workflow_skill_evidence(
        user_id: str,
        bucket_signature: Optional[str] = None,
        control_flow_signature: Optional[str] = None,
        _authorized: None = Depends(_authorize_workflow_skill_api),
    ):
        evidence = get_workflow_skill_manager().store.list_evidence(
            user_id,
            bucket_signature=bucket_signature,
            control_flow_signature=control_flow_signature,
        )
        return [item.model_dump(mode="json") for item in evidence]

    @app.get("/api/workflow-skills/{skill_id}")
    def get_workflow_skill(
        skill_id: str,
        user_id: str,
        _authorized: None = Depends(_authorize_workflow_skill_api),
    ):
        manager = get_workflow_skill_manager()
        card = manager.store.get(user_id, skill_id)
        if card is None and user_id != "share":
            card = manager.store.get("share", skill_id)
        if card is None:
            raise HTTPException(status_code=404, detail="未找到工作流技能")
        return card.model_dump(mode="json")

    @app.post("/api/workflow-skills/distill")
    def distill_workflow_skill(
        body: WorkflowSkillDistillRequest,
        _authorized: None = Depends(_authorize_workflow_skill_api),
    ):
        task = TaskLogger.load(body.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="未找到任务日志")
        if task.status != "completed" or task.execution_phase != "execution":
            raise HTTPException(status_code=400, detail="只能蒸馏已经完成的 Production 任务")
        if body.workflow_id and task.workflow_id != body.workflow_id:
            raise HTTPException(status_code=400, detail="Task/workflow identity mismatch")
        owner_prefix = str(task.workflow_id or "").partition(":")[0]
        if owner_prefix and owner_prefix != body.user_id:
            raise HTTPException(status_code=403, detail="Task does not belong to this user")
        planning_steps = _normalize_workflow_steps(getattr(task, "planning_steps", []))
        task_profile = getattr(task, "task_profile", {})
        if not planning_steps:
            raise HTTPException(status_code=400, detail="任务日志中没有可蒸馏的规划步骤")
        raw_evidence = getattr(task, "skill_execution_evidence", {})
        if not isinstance(raw_evidence, dict) or not raw_evidence:
            raise HTTPException(
                status_code=400,
                detail="Task log has no structured skill execution evidence",
            )
        try:
            execution_evidence = load_execution_evidence(
                raw_evidence,
                planning_steps=planning_steps,
            )
            if execution_evidence.task_id != body.task_id:
                raise ValueError("Task/evidence identity mismatch")
            decision = evaluate_distillation_evidence(execution_evidence)
            if not decision.eligible:
                raise ValueError(
                    "Execution is not eligible for skill distillation: "
                    + ", ".join(decision.reasons)
                )
            card = get_workflow_skill_manager().distill(
                user_id=body.user_id,
                task_id=body.task_id,
                user_query=task.user_query,
                planning_steps=planning_steps,
                task_profile=task_profile if isinstance(task_profile, dict) else {},
                agent_contracts=getattr(task, "agent_contract_fingerprints", {}),
                agent_capabilities=getattr(task, "agent_capability_bindings", {}),
                outcome_summary=execution_evidence.outcome_summary(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"skill": card.model_dump(mode="json")}

    @app.post("/api/workflow-skills/{skill_id}/activate")
    def activate_workflow_skill(
        skill_id: str,
        body: WorkflowSkillUserRequest,
        _authorized: None = Depends(_authorize_workflow_skill_api),
    ):
        try:
            card = get_workflow_skill_manager().store.activate(body.user_id, skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"event": "skill_activated", "skill": card.model_dump(mode="json")}

    @app.post("/api/workflow-skills/{skill_id}/disable")
    def disable_workflow_skill(
        skill_id: str,
        body: WorkflowSkillUserRequest,
        _authorized: None = Depends(_authorize_workflow_skill_api),
    ):
        try:
            card = get_workflow_skill_manager().store.disable(body.user_id, skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"event": "skill_disabled", "skill": card.model_dump(mode="json")}

    # ---- S-ABAC Security & Demo API ----

    @app.get("/api/security/approvals")
    async def list_security_approvals(
        status: Optional[str] = None,
        workflow_id: Optional[str] = None,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
        _operator: str = Depends(_authenticate_governance_operator),
    ):
        return _enrich_governance_queue_items(
            get_approval_store().list(
                status=status,
                workflow_id=workflow_id,
                task_id=task_id,
                user_id=user_id,
            )
        )

    @app.post("/api/security/approvals/{approval_id}/approve")
    async def approve_security_request(
        approval_id: str,
        body: ApprovalDecisionRequest,
        operator: str = Depends(_authenticate_governance_operator),
    ):
        try:
            approval = get_approval_store().approve(
                approval_id,
                approver=operator,
                comment=body.comment,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record_governance_event(
            "APPROVAL_GRANTED",
            task_id=approval.task_id,
            workflow_id=approval.workflow_id,
            step_id=approval.step_id or None,
            subject=operator,
            agent=approval.node_name,
            decision="APPROVED",
            details={
                "approval_id": approval.approval_id,
                "approver": operator,
                "request_user_id": approval.user_id,
                "comment": body.comment,
                "resume_step": approval.resume_step,
            },
        )
        return {
            **approval.__dict__,
            "resume_endpoint": "/api/tasks/resume",
            "resume_request": {
                "task_id": approval.task_id,
                "resume_step": approval.resume_step,
                "user_id": approval.user_id,
                "workmode": "production",
            },
        }

    @app.post("/api/security/approvals/{approval_id}/reject")
    async def reject_security_request(
        approval_id: str,
        body: ApprovalDecisionRequest,
        operator: str = Depends(_authenticate_governance_operator),
    ):
        try:
            approval = get_approval_store().reject(
                approval_id,
                approver=operator,
                comment=body.comment,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record_governance_event(
            "APPROVAL_REJECTED",
            task_id=approval.task_id,
            workflow_id=approval.workflow_id,
            step_id=approval.step_id or None,
            subject=operator,
            agent=approval.node_name,
            decision="REJECTED",
            details={
                "approval_id": approval.approval_id,
                "approver": operator,
                "request_user_id": approval.user_id,
                "comment": body.comment,
            },
        )
        return approval.__dict__

    @app.get("/api/security/reconciliations")
    async def list_security_reconciliations(
        status: Optional[str] = None,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
        _operator: str = Depends(_authenticate_governance_operator),
    ):
        """List uncertain side effects that require an explicit human verdict."""
        return _enrich_governance_queue_items(
            get_reconciliation_store().list(
                status=status,
                task_id=task_id,
                user_id=user_id,
            )
        )

    def _reconciliation_resume_response(reconciliation: Any) -> dict[str, Any]:
        return {
            **reconciliation.__dict__,
            "resume_endpoint": "/api/tasks/resume",
            "resume_request": {
                "task_id": reconciliation.task_id,
                "resume_step": reconciliation.resume_step,
                "user_id": reconciliation.user_id,
                "workmode": "production",
            },
        }

    def _load_reconciliation(reconciliation_id: str) -> Any:
        reconciliation = get_reconciliation_store().get(reconciliation_id)
        if reconciliation is None:
            raise HTTPException(
                status_code=404,
                detail=f"reconciliation not found: {reconciliation_id}",
            )
        return reconciliation

    def _raise_reconciliation_conflict(exc: Exception) -> None:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/security/reconciliations/{reconciliation_id}/retry")
    async def retry_reconciliation(
        reconciliation_id: str,
        body: ReconciliationDecisionRequest,
        operator: str = Depends(_authenticate_governance_operator),
    ):
        """Confirm no external effect occurred and release the receipt for retry."""
        reconciliation = _load_reconciliation(reconciliation_id)
        if reconciliation.status not in {"pending", "frozen"}:
            raise HTTPException(
                status_code=409,
                detail=(
                    "reconciliation is not retryable in "
                    f"status={reconciliation.status}"
                ),
            )
        if not reconciliation.idempotency_key:
            raise HTTPException(
                status_code=409,
                detail="missing idempotency key; automatic receipt release is unsafe",
            )
        try:
            reconciliation = get_reconciliation_store().resolve_with_receipt(
                reconciliation_id,
                receipt_store=PersistentReceiptStore(reconciliation.task_id),
                decision="retry",
                operator=operator,
                comment=body.comment,
            )
        except (
            KeyError,
            ValueError,
            ReceiptClaimMismatch,
            ReceiptStoreCorruption,
            OSError,
        ) as exc:
            _raise_reconciliation_conflict(exc)
        record_governance_event(
            "RECONCILIATION_RESOLVED",
            task_id=reconciliation.task_id,
            workflow_id=reconciliation.workflow_id,
            step_id=reconciliation.step_id or None,
            subject=operator,
            agent=reconciliation.agent_name,
            decision="SAFE_TO_RETRY",
            details={
                "reconciliation_id": reconciliation.reconciliation_id,
                "operator": operator,
                "request_user_id": reconciliation.user_id,
                "comment": body.comment,
                "receipt_released": True,
                "resume_step": reconciliation.resume_step,
            },
        )
        return _reconciliation_resume_response(reconciliation)

    @app.post("/api/security/reconciliations/{reconciliation_id}/succeeded")
    async def confirm_reconciliation_succeeded(
        reconciliation_id: str,
        body: ReconciliationDecisionRequest,
        operator: str = Depends(_authenticate_governance_operator),
    ):
        """Confirm the external operation succeeded and complete its receipt."""
        reconciliation = _load_reconciliation(reconciliation_id)
        if reconciliation.status not in {"pending", "frozen"}:
            raise HTTPException(
                status_code=409,
                detail=(
                    "reconciliation is not confirmable in "
                    f"status={reconciliation.status}"
                ),
            )
        if not reconciliation.idempotency_key:
            raise HTTPException(status_code=409, detail="missing idempotency key")
        if not body.external_operation_id.strip():
            raise HTTPException(
                status_code=422,
                detail="external_operation_id is required",
            )
        try:
            reconciliation = get_reconciliation_store().resolve_with_receipt(
                reconciliation_id,
                receipt_store=PersistentReceiptStore(reconciliation.task_id),
                decision="succeeded",
                operator=operator,
                comment=body.comment,
                external_operation_id=body.external_operation_id.strip(),
                outputs=body.outputs,
            )
        except (
            KeyError,
            ValueError,
            ReceiptClaimMismatch,
            ReceiptStoreCorruption,
            OSError,
        ) as exc:
            _raise_reconciliation_conflict(exc)
        record_governance_event(
            "RECONCILIATION_RESOLVED",
            task_id=reconciliation.task_id,
            workflow_id=reconciliation.workflow_id,
            step_id=reconciliation.step_id or None,
            subject=operator,
            agent=reconciliation.agent_name,
            decision="CONFIRMED_SUCCEEDED",
            details={
                "reconciliation_id": reconciliation.reconciliation_id,
                "operator": operator,
                "request_user_id": reconciliation.user_id,
                "comment": body.comment,
                "external_operation_id": body.external_operation_id.strip(),
                "resume_step": reconciliation.resume_step,
            },
        )
        return _reconciliation_resume_response(reconciliation)

    @app.post("/api/security/reconciliations/{reconciliation_id}/freeze")
    async def freeze_reconciliation(
        reconciliation_id: str,
        body: ReconciliationDecisionRequest,
        operator: str = Depends(_authenticate_governance_operator),
    ):
        try:
            reconciliation = get_reconciliation_store().freeze(
                reconciliation_id,
                operator=operator,
                comment=body.comment,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            _raise_reconciliation_conflict(exc)
        record_governance_event(
            "RECONCILIATION_FROZEN",
            task_id=reconciliation.task_id,
            workflow_id=reconciliation.workflow_id,
            step_id=reconciliation.step_id or None,
            subject=operator,
            agent=reconciliation.agent_name,
            decision="FROZEN",
            details={
                "reconciliation_id": reconciliation.reconciliation_id,
                "operator": operator,
                "request_user_id": reconciliation.user_id,
                "comment": body.comment,
            },
        )
        return reconciliation.__dict__

    @app.post("/api/security/reconciliations/{reconciliation_id}/terminate")
    async def terminate_reconciliation(
        reconciliation_id: str,
        body: ReconciliationDecisionRequest,
        operator: str = Depends(_authenticate_governance_operator),
    ):
        try:
            reconciliation = get_reconciliation_store().resolve(
                reconciliation_id,
                status="terminated",
                operator=operator,
                comment=body.comment,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            _raise_reconciliation_conflict(exc)
        record_governance_event(
            "RECONCILIATION_TERMINATED",
            task_id=reconciliation.task_id,
            workflow_id=reconciliation.workflow_id,
            step_id=reconciliation.step_id or None,
            subject=operator,
            agent=reconciliation.agent_name,
            decision="TERMINATED",
            details={
                "reconciliation_id": reconciliation.reconciliation_id,
                "operator": operator,
                "request_user_id": reconciliation.user_id,
                "comment": body.comment,
                "receipt_preserved": True,
            },
        )
        return reconciliation.__dict__

    @app.get("/api/security/status")
    async def get_security_status():
        """Get the current S-ABAC status."""
        return {
            "s_abac_enabled": S_ABAC_ENABLED,
            "orchestration_scheduler_enabled": ORCHESTRATION_SCHEDULER_ENABLED,
            "auto_recovery_enabled": AUTO_RECOVERY_ENABLED,
            "auto_recovery_max_attempts": SCHEDULER_AUTO_RECOVERY_MAX_ATTEMPTS,
            "retry_base_seconds": SCHEDULER_RETRY_BASE_SECONDS,
            "retry_max_seconds": SCHEDULER_RETRY_MAX_SECONDS,
            "retry_jitter_ratio": SCHEDULER_RETRY_JITTER_RATIO,
            "policies_count": len(S_ABAC_POLICIES),
            "agent_attributes_count": len(AGENT_SECURITY_ATTRIBUTES),
            "resource_attributes_count": len(RESOURCE_SECURITY_ATTRIBUTES),
        }

    @app.get("/api/security/users")
    async def list_security_users():
        """List all demo users for S-ABAC simulation."""
        return list_demo_users()

    @app.get("/api/security/users/{user_id}")
    async def get_security_user(user_id: str):
        """Get demo user security profile including available agents."""
        profile = get_demo_user(user_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Demo user not found: {user_id}")

        # Build available agents with security context
        available = get_user_available_agents(user_id)
        agents_info = []
        if available == ["*"]:
            # Admin can access all agents
            agents_info = [
                {
                    "agent_name": name,
                    "role": attrs.get("role", ""),
                    "department": attrs.get("department", ""),
                    "clearance_level": attrs.get("clearance_level", 0),
                    "trust_level": attrs.get("trust_level", ""),
                }
                for name, attrs in AGENT_SECURITY_ATTRIBUTES.items()
            ]
        else:
            agents_info = [
                {
                    "agent_name": name,
                    "role": AGENT_SECURITY_ATTRIBUTES.get(name, {}).get("role", ""),
                    "department": AGENT_SECURITY_ATTRIBUTES.get(name, {}).get("department", ""),
                    "clearance_level": AGENT_SECURITY_ATTRIBUTES.get(name, {}).get("clearance_level", 0),
                    "trust_level": AGENT_SECURITY_ATTRIBUTES.get(name, {}).get("trust_level", ""),
                }
                for name in available
            ]

        # Build tool access predictions based on user's role/clearance
        tool_access = {}
        user_role = profile.get("role", "")
        user_clearance = profile.get("clearance_level", 0)
        for tool_name, attrs in RESOURCE_SECURITY_ATTRIBUTES.items():
            allowed_roles = attrs.get("allowed_roles", [])
            sensitivity = attrs.get("sensitivity", "LOW")
            can_access = (
                (not allowed_roles or user_role in allowed_roles)
                and user_clearance >= SENSITIVITY_LEVELS.get(sensitivity, 1)
            )
            tool_access[tool_name] = {
                "can_access": can_access,
                "sensitivity": sensitivity,
                "allowed_roles": allowed_roles,
            }

        return {
            "user_id": user_id,
            "profile": profile,
            "available_agents": agents_info,
            "tool_access": tool_access,
        }

    @app.get("/api/security/policies")
    async def list_security_policies():
        """List all S-ABAC policies."""
        return [
            {
                "policy_id": p.get("policy_id", ""),
                "description": p.get("description", ""),
                "rules": [
                    {
                        "condition": r.get("condition", {}),
                        "effect": r.get("effect", "DENY"),
                        "constraints": r.get("constraints", {}),
                    }
                    for r in p.get("rules", [])
                ],
            }
            for p in S_ABAC_POLICIES
        ]

    @app.get("/api/security/agents-attributes")
    async def list_agent_security_attributes():
        """List all agent security attributes."""
        return AGENT_SECURITY_ATTRIBUTES

    @app.get("/api/security/resources-attributes")
    async def list_resource_security_attributes():
        """List all resource security attributes."""
        return RESOURCE_SECURITY_ATTRIBUTES

    @app.get("/api/security/check")
    async def check_permission(
        user_id: str,
        agent_name: str = "",
        tool_name: str = "",
        action: str = "execute",
    ):
        """Pre-check whether a user/agent can access a tool or dispatch an agent."""
        if not S_ABAC_ENABLED:
            return {"allowed": True, "reason": "S-ABAC is disabled"}

        profile = get_demo_user(user_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"User not found: {user_id}")
        if bool(tool_name) == bool(agent_name):
            raise HTTPException(
                status_code=422,
                detail="exactly one of tool_name or agent_name is required",
            )

        resource_name = tool_name or agent_name
        result = _static_resource_precheck(
            profile,
            RESOURCE_SECURITY_ATTRIBUTES.get(resource_name),
            resource_name=resource_name,
            operation_mode=action,
        )
        if agent_name:
            available = get_user_available_agents(user_id)
            available_to_user = available == ["*"] or agent_name in available
            result["available_to_user"] = available_to_user
            if not available_to_user:
                result.update(
                    {
                        "allowed": False,
                        "can_access": False,
                        "eligible": False,
                        "decision": "DENY",
                        "blocked_reason": "Agent is not available to this user",
                    }
                )
        return {
            **result,
            "reason": result.get("blocked_reason") or result.get("decision"),
            "details": dict(result),
            "user_id": user_id,
            "resource_name": resource_name,
        }

    @app.get("/api/security/users/{user_id}/precheck")
    async def precheck_user_permissions(user_id: str):
        profile = get_demo_user(user_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Demo user not found: {user_id}")

        available_agents = get_user_available_agents(user_id)

        tool_access = {}
        for tool_name, attrs in RESOURCE_SECURITY_ATTRIBUTES.items():
            if attrs.get("type") == "agent":
                continue
            tool_access[tool_name] = _static_resource_precheck(
                profile, attrs, resource_name=tool_name
            )

        agent_access = {}
        for agent_name, subject_attrs in AGENT_SECURITY_ATTRIBUTES.items():
            is_available = available_agents == ["*"] or agent_name in available_agents
            access = _static_resource_precheck(
                profile,
                RESOURCE_SECURITY_ATTRIBUTES.get(agent_name),
                resource_name=agent_name,
            )
            if not is_available:
                access.update(
                    {
                        "allowed": False,
                        "can_access": False,
                        "eligible": False,
                        "decision": "DENY",
                        "blocked_reason": "Agent is not available to this user",
                    }
                )
            agent_access[agent_name] = {
                **access,
                "agent_name": agent_name,
                "agent_role": subject_attrs.get("role", ""),
                "agent_clearance": subject_attrs.get("clearance_level", 0),
                "available_to_user": is_available,
            }

        return {
            "user_id": user_id,
            "profile": profile,
            "tool_access": tool_access,
            "agent_access": agent_access,
        }

    @app.get("/api/security/tool-check")
    async def check_tool_permission(
        user_id: str,
        tool_name: str,
    ):
        if not S_ABAC_ENABLED:
            return {"allowed": True, "reason": "S-ABAC is disabled", "user_id": user_id, "tool_name": tool_name}

        profile = get_demo_user(user_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Demo user not found: {user_id}")

        result = _static_resource_precheck(
            profile,
            RESOURCE_SECURITY_ATTRIBUTES.get(tool_name),
            resource_name=tool_name,
        )
        return {
            **result,
            "tool_name": tool_name,
            "user_id": user_id,
        }

    return app


# ---- Tasks API request models ----
class ResumeRequest(BaseModel):
    """Request model for resuming a task from checkpoint.
    Configuration (debug, deep_thinking_mode, etc.) is restored from checkpoint.
    """
    task_id: str
    resume_step: int
    user_id: str = "test"
    lang: str = "en"
    workmode: str = "launch"

app = create_app()
