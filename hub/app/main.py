import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import docker_manager as dm
from .catalog import AUTH_MODES, KINDS, OPENAPI_VERSIONS

app = FastAPI(title="sandbox-hub")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/kinds")
def list_kinds():
    return {
        "kinds": [
            {
                "id": k.id,
                "label": k.label,
                "description": k.description,
                "supports_openapi": k.supports_openapi,
            }
            for k in KINDS.values()
        ],
        "auth_modes": AUTH_MODES,
        "openapi_versions": OPENAPI_VERSIONS,
    }


@app.get("/api/instances")
def list_instances():
    return dm.list_instances()


class CreateInstanceRequest(BaseModel):
    kind: str
    name: Optional[str] = None
    auth_mode: str = "none"
    openapi_version: Optional[str] = None
    openapi_protect: bool = False


@app.post("/api/instances")
def create_instance(req: CreateInstanceRequest):
    if req.kind not in KINDS:
        raise HTTPException(404, f"unknown kind {req.kind!r}")
    if req.auth_mode not in AUTH_MODES:
        raise HTTPException(400, f"unknown auth_mode {req.auth_mode!r}")

    config = {"auth_mode": req.auth_mode}
    if KINDS[req.kind].supports_openapi:
        config["openapi_version"] = req.openapi_version or "3.1"
        config["openapi_protect"] = req.openapi_protect

    try:
        return dm.create_instance(req.kind, req.name, config)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/instances/{instance_id}")
def get_instance(instance_id: str):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    return detail


@app.delete("/api/instances/{instance_id}")
def delete_instance(instance_id: str):
    if dm.instance_detail(instance_id) is None:
        raise HTTPException(404, "unknown instance")
    dm.remove_instance(instance_id)
    return {"ok": True}


@app.post("/api/instances/{instance_id}/rotate")
def rotate_instance(instance_id: str):
    if dm.instance_detail(instance_id) is None:
        raise HTTPException(404, "unknown instance")
    try:
        return dm.rotate_instance(instance_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/instances/{instance_id}/logs")
def get_logs(instance_id: str, tail: int = 200):
    if dm.instance_detail(instance_id) is None:
        raise HTTPException(404, "unknown instance")
    return {"logs": dm.logs(instance_id, tail=tail)}


@app.websocket("/api/instances/{instance_id}/logs/stream")
async def stream_logs_ws(websocket: WebSocket, instance_id: str):
    if dm.instance_detail(instance_id) is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    loop = asyncio.get_event_loop()

    def _generator():
        return dm.stream_logs(instance_id)

    gen = await loop.run_in_executor(None, _generator)
    try:
        while True:
            line = await loop.run_in_executor(None, lambda: next(gen, None))
            if line is None:
                break
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass


@app.get("/api/oauth-provider")
def oauth_provider_status():
    return dm.oauth_provider_status()


@app.get("/api/health")
def health():
    return {"status": "ok"}


_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
