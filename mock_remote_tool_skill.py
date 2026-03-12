from fastapi import FastAPI, Header
from pydantic import BaseModel
from typing import Any, Dict, Optional

app = FastAPI()

class ToolRequest(BaseModel):
    tool: str
    arguments: Dict[str, Any]

class SkillRequest(BaseModel):
    skill: str
    arguments: Dict[str, Any]

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/tool")
async def tool(req: ToolRequest, authorization: Optional[str] = Header(default=None)):
    if req.tool == "remote_weather_tool":
        location = req.arguments.get("location", "")
        result = f"[remote-tool] {location} weather: sunny 20C"
    else:
        result = f"[remote-tool:{req.tool}] ok"
    return {
        "status": "success",
        "result": result,
        "metadata": {"has_auth": bool(authorization)},
    }

@app.post("/skill")
async def skill(req: SkillRequest, authorization: Optional[str] = Header(default=None)):
    if req.skill == "remote_summarize":
        text = req.arguments.get("text", "")
        result = f"[remote-skill] summary: {text[:30]}"
    else:
        result = f"[remote-skill:{req.skill}] ok"
    return {
        "status": "success",
        "result": result,
        "metadata": {"has_auth": bool(authorization)},
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8011)
