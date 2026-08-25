import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import docker_manager as dm
from .catalog import CATALOG

app = FastAPI(title="sandbox-hub")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/resources")
def list_resources():
    out = []
    for rid, rdef in CATALOG.items():
        s = dm.status(rid)
        out.append(
            {
                "id": rdef.id,
                "category": rdef.category,
                "name": rdef.name,
                "description": rdef.description,
                "auth_mode": rdef.auth_mode,
                "requires": list(rdef.requires),
                **s,
            }
        )
    return out


@app.get("/api/resources/{resource_id}")
def get_resource(resource_id: str):
    if resource_id not in CATALOG:
        raise HTTPException(404, "unknown resource")
    rdef = CATALOG[resource_id]
    s = dm.status(resource_id)
    return {
        "id": rdef.id,
        "category": rdef.category,
        "name": rdef.name,
        "description": rdef.description,
        "auth_mode": rdef.auth_mode,
        "requires": list(rdef.requires),
        **s,
    }


@app.post("/api/resources/{resource_id}/start")
def start_resource(resource_id: str):
    if resource_id not in CATALOG:
        raise HTTPException(404, "unknown resource")
    try:
        return dm.start_resource(resource_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/resources/{resource_id}/stop")
def stop_resource(resource_id: str):
    if resource_id not in CATALOG:
        raise HTTPException(404, "unknown resource")
    dependents = [rid for rid, rdef in CATALOG.items() if resource_id in rdef.requires]
    running_dependents = [rid for rid in dependents if dm.status(rid)["state"] == "running"]
    if running_dependents:
        raise HTTPException(
            409,
            f"cannot stop {resource_id}: still required by running resources {running_dependents}",
        )
    return dm.stop_resource(resource_id)


@app.post("/api/resources/{resource_id}/rotate")
def rotate_resource(resource_id: str):
    if resource_id not in CATALOG:
        raise HTTPException(404, "unknown resource")
    rdef = CATALOG[resource_id]
    try:
        if rdef.auth_mode == "apikey":
            return dm.rotate_api_key(resource_id)
        if rdef.auth_mode == "oauth":
            return dm.rotate_oauth_client(resource_id)
        raise HTTPException(400, "resource has no rotatable credential")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/resources/{resource_id}/logs")
def get_logs(resource_id: str, tail: int = 200):
    if resource_id not in CATALOG:
        raise HTTPException(404, "unknown resource")
    return {"logs": dm.logs(resource_id, tail=tail)}


@app.websocket("/api/resources/{resource_id}/logs/stream")
async def stream_logs_ws(websocket: WebSocket, resource_id: str):
    if resource_id not in CATALOG:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    loop = asyncio.get_event_loop()

    def _generator():
        return dm.stream_logs(resource_id)

    gen = await loop.run_in_executor(None, _generator)
    try:
        while True:
            line = await loop.run_in_executor(None, lambda: next(gen, None))
            if line is None:
                break
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass


@app.get("/api/health")
def health():
    return {"status": "ok"}


_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
