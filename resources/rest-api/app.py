"""
A small sample REST API whose behavior is controlled entirely by env vars, so
one image backs every "REST API" instance sandbox-hub creates, however it's
configured.

AUTH_MODE=none    -> no auth on /items, /whoami
AUTH_MODE=apikey  -> those require header  X-API-Key: <API_KEY>
AUTH_MODE=oauth   -> those require header  Authorization: Bearer <token>,
                     validated by POSTing to OAUTH_INTROSPECT_URL

OPENAPI_VERSION=3.0|3.1  -> version declared in the served openapi.json (default 3.1)
OPENAPI_PROTECT=true     -> /openapi.json, /docs, /redoc require
                             X-API-Key: <OPENAPI_TOKEN>, independent of AUTH_MODE
"""
import os
import time

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

AUTH_MODE = os.environ.get("AUTH_MODE", "none")
API_KEY = os.environ.get("API_KEY", "")
OAUTH_INTROSPECT_URL = os.environ.get("OAUTH_INTROSPECT_URL", "")

OPENAPI_VERSION = os.environ.get("OPENAPI_VERSION", "3.1")
OPENAPI_VERSION_STRING = "3.0.2" if OPENAPI_VERSION.startswith("3.0") else "3.1.0"
OPENAPI_PROTECT = os.environ.get("OPENAPI_PROTECT", "false").lower() == "true"
OPENAPI_TOKEN = os.environ.get("OPENAPI_TOKEN", "")

# openapi_url/docs_url/redoc_url disabled here and re-implemented manually
# below so the spec endpoint can be independently version-pinned and gated.
app = FastAPI(
    title=f"sandbox-hub sample REST API ({AUTH_MODE})",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

_items = {
    1: {"id": 1, "name": "widget"},
    2: {"id": 2, "name": "gadget"},
}


async def require_auth(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    if AUTH_MODE == "none":
        return {"mode": "none"}

    if AUTH_MODE == "apikey":
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(401, "missing or invalid X-API-Key header")
        return {"mode": "apikey"}

    if AUTH_MODE == "oauth":
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing Bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                resp = await client.post(OAUTH_INTROSPECT_URL, data={"token": token})
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"could not reach oauth provider: {exc}")
        data = resp.json()
        if not data.get("active"):
            raise HTTPException(401, "token is not active")
        return {"mode": "oauth", "client_id": data.get("client_id"), "scope": data.get("scope")}

    raise HTTPException(500, f"unknown AUTH_MODE={AUTH_MODE}")


async def require_openapi_auth(x_api_key: str | None = Header(default=None)):
    if OPENAPI_PROTECT and (not x_api_key or x_api_key != OPENAPI_TOKEN):
        raise HTTPException(401, "missing or invalid X-API-Key header for the OpenAPI spec")


@app.get("/health")
def health():
    return {"status": "ok", "auth_mode": AUTH_MODE, "time": time.time()}


@app.get("/whoami")
def whoami(auth=Depends(require_auth)):
    return {"authenticated_as": auth}


@app.get("/items")
def list_items(auth=Depends(require_auth)):
    return list(_items.values())


@app.get("/items/{item_id}")
def get_item(item_id: int, auth=Depends(require_auth)):
    if item_id not in _items:
        raise HTTPException(404, "not found")
    return _items[item_id]


@app.post("/items")
def create_item(name: str, auth=Depends(require_auth)):
    new_id = max(_items) + 1
    _items[new_id] = {"id": new_id, "name": name}
    return _items[new_id]


@app.get("/openapi.json", include_in_schema=False)
async def openapi_json(_auth=Depends(require_openapi_auth)):
    return JSONResponse(
        get_openapi(
            title=app.title,
            version="1.0.0",
            openapi_version=OPENAPI_VERSION_STRING,
            routes=app.routes,
        )
    )


@app.get("/docs", include_in_schema=False)
async def swagger_ui():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")


@app.get("/redoc", include_in_schema=False)
async def redoc():
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")
