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


@app.on_event("startup")
def _on_startup():
    # Idempotent and safe to repeat -- also runs on the first instance
    # create, but doing it here too means a hub sitting idle still ends up
    # network-joined (and existing instances' admin calls start working)
    # without needing a create to trigger it.
    dm.ensure_network()


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
                "supports_async_job": k.supports_async_job,
                "supports_auth": k.supports_auth,
                "supports_chaos_config": k.supports_chaos_config,
                "supports_routes": k.supports_routes,
                "supports_graphql_schema": k.supports_graphql_schema,
                "has_own_ui": k.has_own_ui,
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
    async_jobs: bool = False
    async_job_delay_seconds: int = 5


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
    if KINDS[req.kind].supports_async_job:
        config["async_jobs"] = req.async_jobs
        config["async_job_delay_seconds"] = req.async_job_delay_seconds

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


class UpdatePortRequest(BaseModel):
    port: int


@app.put("/api/instances/{instance_id}/port")
def update_port(instance_id: str, req: UpdatePortRequest):
    if dm.instance_detail(instance_id) is None:
        raise HTTPException(404, "unknown instance")
    if not (1 <= req.port <= 65535):
        raise HTTPException(400, "port must be between 1 and 65535")
    try:
        return dm.update_port(instance_id, req.port)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


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


# ------------------------------------------------------------- chaos-api config


class ChaosConfigRequest(BaseModel):
    mode: str
    rate_limit: Optional[dict] = None
    chaos: Optional[dict] = None


@app.put("/api/instances/{instance_id}/chaos-config")
def set_chaos_config(instance_id: str, req: ChaosConfigRequest):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    if detail["kind"] != "chaos-api":
        raise HTTPException(400, "not a chaos-api instance")
    try:
        return dm.configure_chaos(instance_id, req.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(502, str(exc))


# --------------------------------------------------------------- mock-api routes


class MockRouteRequest(BaseModel):
    type: str = "static"  # "static" | "crud"
    method: str = "*"
    path: str = "*"
    status_code: int = 200
    response_body: object = {"ok": True}
    required_fields: list[str] = []
    latency_ms: int = 0
    failure_rate: float = 0.0
    seed: list[dict] = []
    id_field: str = "id"


@app.get("/api/instances/{instance_id}/routes")
def get_routes(instance_id: str):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    if detail["kind"] != "mock-api":
        raise HTTPException(400, "not a mock-api instance")
    return dm.list_routes(instance_id)


@app.post("/api/instances/{instance_id}/routes")
def add_route(instance_id: str, req: MockRouteRequest):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    if detail["kind"] != "mock-api":
        raise HTTPException(400, "not a mock-api instance")
    try:
        return dm.create_route(instance_id, req.model_dump())
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.delete("/api/instances/{instance_id}/routes/{route_id}")
def remove_route(instance_id: str, route_id: str):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    dm.delete_route(instance_id, route_id)
    return {"ok": True}


@app.delete("/api/instances/{instance_id}/routes")
def remove_all_routes(instance_id: str):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    dm.clear_routes(instance_id)
    return {"ok": True}


class ImportOpenApiRequest(BaseModel):
    spec: object = None
    url: Optional[str] = None
    headers: dict[str, str] = {}
    replace: bool = False


@app.post("/api/instances/{instance_id}/routes/import-openapi")
def import_openapi(instance_id: str, req: ImportOpenApiRequest):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    if detail["kind"] != "mock-api":
        raise HTTPException(400, "not a mock-api instance")
    if (req.spec is None) == (req.url is None):
        raise HTTPException(400, "give exactly one of spec or url")
    try:
        return dm.import_openapi_routes(instance_id, req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ----------------------------------------------------------------- graphql-api


def _require_graphql_instance(instance_id: str) -> dict:
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    if detail["kind"] != "graphql-api":
        raise HTTPException(400, "not a graphql-api instance")
    return detail


class GraphQLSchemaRequest(BaseModel):
    sdl: str


@app.get("/api/instances/{instance_id}/graphql/schema")
def get_graphql_schema(instance_id: str):
    _require_graphql_instance(instance_id)
    return dm.get_graphql_schema(instance_id)


@app.put("/api/instances/{instance_id}/graphql/schema")
def set_graphql_schema(instance_id: str, req: GraphQLSchemaRequest):
    _require_graphql_instance(instance_id)
    try:
        return dm.set_graphql_schema(instance_id, req.sdl)
    except Exception as exc:
        raise HTTPException(400, str(exc))


class GraphQLResolverError(BaseModel):
    message: str
    extensions: Optional[dict] = None


class GraphQLResolverRequest(BaseModel):
    type: str
    field: str
    response_body: object = None
    error: Optional[GraphQLResolverError] = None


@app.get("/api/instances/{instance_id}/graphql/resolvers")
def get_graphql_resolvers(instance_id: str):
    _require_graphql_instance(instance_id)
    return dm.list_graphql_resolvers(instance_id)


@app.post("/api/instances/{instance_id}/graphql/resolvers")
def add_graphql_resolver(instance_id: str, req: GraphQLResolverRequest):
    _require_graphql_instance(instance_id)
    try:
        return dm.set_graphql_resolver(instance_id, req.model_dump())
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/instances/{instance_id}/graphql/resolvers/{type_name}/{field_name}")
def remove_graphql_resolver(instance_id: str, type_name: str, field_name: str):
    _require_graphql_instance(instance_id)
    dm.delete_graphql_resolver(instance_id, type_name, field_name)
    return {"ok": True}


# --------------------------------------------------------- webhook-receiver log


@app.get("/api/instances/{instance_id}/webhook-requests")
def get_webhook_requests(instance_id: str):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    if detail["kind"] != "webhook-receiver":
        raise HTTPException(400, "not a webhook-receiver instance")
    return dm.list_webhook_requests(instance_id)


@app.delete("/api/instances/{instance_id}/webhook-requests")
def clear_webhook_requests(instance_id: str):
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise HTTPException(404, "unknown instance")
    dm.clear_webhook_requests(instance_id)
    return {"ok": True}


# ---------------------------------------------------------- scenario export/import


@app.get("/api/instances/{instance_id}/scenario")
def export_instance_scenario(instance_id: str):
    if dm.instance_detail(instance_id) is None:
        raise HTTPException(404, "unknown instance")
    return dm.export_scenario([instance_id])


@app.get("/api/scenario")
def export_all_scenario():
    return dm.export_scenario()


class ScenarioImportRequest(BaseModel):
    sandboxhub_scenario: Optional[int] = None
    instances: list[dict] = []


@app.post("/api/scenario/import")
def import_scenario(req: ScenarioImportRequest):
    try:
        return dm.import_scenario(req.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/health")
def health():
    return {"status": "ok"}


_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
