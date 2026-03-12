from fastapi import FastAPI
import json
from pathlib import Path

app = FastAPI()

_ROOT = Path(__file__).resolve().parent
_REGISTRY_PATH = _ROOT / "mock_remote_registry.json"

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/resources")
async def resources():
    payload = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8-sig"))
    return payload

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8012)
