#!/usr/bin/env python
"""Mock remote agent server for testing RemoteExecutor."""

from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Header
from pydantic import BaseModel

app = FastAPI()


class RemoteRequest(BaseModel):
    agent_name: str
    messages: List[Dict[str, Any]]
    context: Dict[str, Any]
    prompt: Optional[str] = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/agent")
async def agent(req: RemoteRequest, authorization: Optional[str] = Header(default=None)):
    # Simple echo behavior: summarize latest message + metadata
    last = req.messages[-1]["content"] if req.messages else ""
    result = f"[remote:{req.agent_name}] {last}"
    return {
        "status": "success",
        "result": result,
        "metadata": {
            "has_auth": bool(authorization),
            "message_count": len(req.messages),
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)
