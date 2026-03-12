import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.interface.agent import AgentRequest
from src.manager import agent_manager
from src.manager.registry import ToolRegistry
from src.service.server import Server
from src.utils.path_utils import get_project_root
from src.workflow.cache import workflow_cache


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

    return app


app = create_app()
