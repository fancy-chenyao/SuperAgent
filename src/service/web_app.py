import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel

from src.interface.agent import AgentRequest
from src.manager import agent_manager
from src.manager.registry import ToolRegistry
from src.service.server import Server
from src.utils.path_utils import get_project_root
from src.workflow.cache import workflow_cache
from src.robust.checkpoint import CheckpointManager
from src.robust.task_logger import TaskLogger


def _sse_format(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    lines = payload.splitlines() or [""]
    message = f"event: {event}\n"
    for line in lines:
        message += f"data: {line}\n"
    message += "\n"
    return message


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
        return datetime.fromisoformat(text)
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
    return latest


def _format_datetime(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return None


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

    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/")
    async def index():
        index_path = web_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="index.html not found")
        return FileResponse(index_path)

    @app.post("/api/workflows/run")
    async def run_workflow(request: Request, body: AgentRequest):
        server = Server()

        async def event_stream() -> AsyncGenerator[str, None]:
            try:
                async for event in server._run_agent_workflow(body):
                    if await request.is_disconnected():
                        break
                    event_type = event.get("event", "message")
                    yield _sse_format(event_type, event)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

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
        return [
            {
                "name": getattr(meta.tool, "name", ""),
                "description": meta.description or getattr(meta.tool, "description", ""),
            }
            for meta in tools
            if getattr(meta.tool, "name", "")
        ]

    @app.get("/api/workflows")
    async def list_workflows(user_id: Optional[str] = None, match: Optional[str] = None):
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        workflows = await Server._list_workflow_json(user_id=user_id, match=match)
        return workflows

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
    async def list_tasks(workflow_id: Optional[str] = None):
        """List all task execution instances, optionally filtered by workflow_id."""
        return TaskLogger.list_tasks(workflow_id=workflow_id)

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
        Resume a task execution from a specific checkpoint step.
        Streams SSE events like the normal run endpoint.
        """
        from src.robust.task_logger import TaskLogger as TL
        from src.interface.agent import AgentMessage

        # Load original messages from task log to reconstruct context
        task_log = TL.load(body.task_id)
        messages = []
        if task_log:
            # Extract initial user message from history
            for entry in task_log.history:
                if entry.get("event") == "workflow_start":
                    q = task_log.user_query
                    if q:
                        messages = [{"role": "user", "content": q}]
                    break
        if not messages:
            messages = [{"role": "user", "content": "(resume)"}]

        agent_request = AgentRequest(
            user_id=body.user_id,
            task_type=body.task_type,
            workmode=body.workmode,
            messages=[AgentMessage(role=m["role"], content=m["content"]) for m in messages],
            debug=body.debug,
            deep_thinking_mode=body.deep_thinking_mode,
            search_before_planning=body.search_before_planning,
            coor_agents=body.coor_agents,
            workflow_id=body.workflow_id,
        )

        server = Server()

        async def event_stream() -> AsyncGenerator[str, None]:
            try:
                async for event in server._run_agent_workflow_with_resume(
                    agent_request, resume_step=body.resume_step
                ):
                    if await request.is_disconnected():
                        break
                    event_type = event.get("event", "message")
                    yield _sse_format(event_type, event)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return app


# ---- Tasks API request models ----
class ResumeRequest(BaseModel):
    task_id: str
    resume_step: int
    workflow_id: Optional[str] = None
    user_id: str = "test"
    task_type: str = "agent_workflow"
    workmode: str = "launch"
    debug: bool = False
    deep_thinking_mode: bool = True
    search_before_planning: bool = False
    coor_agents: Optional[list] = None


app = create_app()
