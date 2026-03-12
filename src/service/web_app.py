import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
