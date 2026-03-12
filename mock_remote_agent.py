#!/usr/bin/env python
"""Mock remote agent server for testing RemoteExecutor."""

from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Header
from pydantic import BaseModel
import httpx

app = FastAPI()


class RemoteRequest(BaseModel):
    agent_name: str
    messages: List[Dict[str, Any]]
    context: Dict[str, Any]
    prompt: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/agent")
async def agent(req: RemoteRequest, authorization: Optional[str] = Header(default=None)):
    last = req.messages[-1]["content"] if req.messages else ""
    tool_result = None

    if req.tools:
        tool = req.tools[0].get("name")
        if tool:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    "http://127.0.0.1:8011/tool",
                    json={"tool": tool, "arguments": {"location": "东太行"}},
                    headers={"Content-Type": "application/json"},
                )
                tool_result = resp.json().get("result")

    result = f"[remote:{req.agent_name}] {last}"
    if tool_result:
        result += f" | tool:{tool_result}"

    return {
        "status": "success",
        "result": result,
        "metadata": {
            "has_auth": bool(authorization),
            "message_count": len(req.messages),
            "tool_called": bool(tool_result),
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)
