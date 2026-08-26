"""
A small sample REST API whose behavior is controlled entirely by env vars, so
one image backs every "REST API" instance sandbox-hub creates, however it's
configured.

AUTH_MODE=none    -> no auth on /items, /whoami
AUTH_MODE=apikey  -> those require header  X-API-Key: <API_KEY>
AUTH_MODE=basic   -> those require HTTP Basic auth (BASIC_USERNAME/BASIC_PASSWORD)
AUTH_MODE=jwt     -> those require header  Authorization: Bearer <jwt>, a
                     self-contained HS256 JWT verified locally with JWT_SECRET
                     (no external calls) -- GET /_debug/token mints a fresh one
AUTH_MODE=session -> POST /login with {username,password} (SESSION_USERNAME/
                     SESSION_PASSWORD) sets a session cookie; that cookie then
                     gates /items, /whoami; POST /logout clears it
AUTH_MODE=oauth   -> those require header  Authorization: Bearer <token>,
                     validated by POSTing to OAUTH_INTROSPECT_URL

OPENAPI_VERSION=3.0|3.1  -> version declared in the served openapi.json (default 3.1)
OPENAPI_PROTECT=true     -> /openapi.json, /docs, /redoc require
                             X-API-Key: <OPENAPI_TOKEN>, independent of AUTH_MODE

ASYNC_JOBS=true              -> enables POST /jobs (submit, 202 + job_id) and
                                 GET /jobs/{id} (polls "pending" -> "done"),
                                 gated by the same AUTH_MODE as /items
ASYNC_JOB_DELAY_SECONDS=N    -> how long a job stays "pending" before it
                                 resolves to "done" (default 5)
"""
import base64
import os
import secrets
import time
import uuid

import httpx
import jwt as pyjwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

AUTH_MODE = os.environ.get("AUTH_MODE", "none")
API_KEY = os.environ.get("API_KEY", "")
BASIC_USERNAME = os.environ.get("BASIC_USERNAME", "")
BASIC_PASSWORD = os.environ.get("BASIC_PASSWORD", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "86400"))
SESSION_USERNAME = os.environ.get("SESSION_USERNAME", "")
SESSION_PASSWORD = os.environ.get("SESSION_PASSWORD", "")
OAUTH_INTROSPECT_URL = os.environ.get("OAUTH_INTROSPECT_URL", "")

OPENAPI_VERSION = os.environ.get("OPENAPI_VERSION", "3.1")
OPENAPI_VERSION_STRING = "3.0.2" if OPENAPI_VERSION.startswith("3.0") else "3.1.0"
OPENAPI_PROTECT = os.environ.get("OPENAPI_PROTECT", "false").lower() == "true"
OPENAPI_TOKEN = os.environ.get("OPENAPI_TOKEN", "")

ASYNC_JOBS = os.environ.get("ASYNC_JOBS", "false").lower() == "true"
ASYNC_JOB_DELAY_SECONDS = float(os.environ.get("ASYNC_JOB_DELAY_SECONDS", "5"))

SESSION_COOKIE = "sandboxhub_session"
_active_sessions: set[str] = set()

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

# job_id -> {submitted_at, payload} -- status is derived from elapsed time at
# read time rather than stored, so no background worker is needed.
_jobs: dict[str, dict] = {}


def _mint_jwt() -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"sub": "sandbox-hub-tester", "iat": now, "exp": now + JWT_TTL_SECONDS},
        JWT_SECRET,
        algorithm="HS256",
    )


async def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    if AUTH_MODE == "none":
        return {"mode": "none"}

    if AUTH_MODE == "apikey":
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(401, "missing or invalid X-API-Key header")
        return {"mode": "apikey"}

    if AUTH_MODE == "basic":
        if not authorization or not authorization.startswith("Basic "):
            raise HTTPException(401, "missing Basic auth", headers={"WWW-Authenticate": "Basic"})
        try:
            decoded = base64.b64decode(authorization.removeprefix("Basic ").strip()).decode()
            user, _, pwd = decoded.partition(":")
        except Exception:
            raise HTTPException(401, "malformed Basic auth header", headers={"WWW-Authenticate": "Basic"})
        if not (secrets.compare_digest(user, BASIC_USERNAME) and secrets.compare_digest(pwd, BASIC_PASSWORD)):
            raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Basic"})
        return {"mode": "basic", "user": user}

    if AUTH_MODE == "jwt":
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing Bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            claims = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(401, "token expired")
        except pyjwt.InvalidTokenError as exc:
            raise HTTPException(401, f"invalid token: {exc}")
        return {"mode": "jwt", "claims": claims}

    if AUTH_MODE == "session":
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id or session_id not in _active_sessions:
            raise HTTPException(401, "not logged in -- POST /login first")
        return {"mode": "session"}

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


@app.post("/login", include_in_schema=False)
async def login(payload: dict):
    if AUTH_MODE != "session":
        raise HTTPException(404, "not found")
    if not (
        secrets.compare_digest(str(payload.get("username", "")), SESSION_USERNAME)
        and secrets.compare_digest(str(payload.get("password", "")), SESSION_PASSWORD)
    ):
        raise HTTPException(401, "invalid username/password")
    session_id = secrets.token_urlsafe(24)
    _active_sessions.add(session_id)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    return resp


@app.post("/logout", include_in_schema=False)
async def logout(request: Request):
    if AUTH_MODE != "session":
        raise HTTPException(404, "not found")
    _active_sessions.discard(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/_debug/token", include_in_schema=False)
def debug_token():
    if AUTH_MODE != "jwt":
        raise HTTPException(404, "not found")
    return {"token": _mint_jwt(), "expires_in": JWT_TTL_SECONDS}


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


@app.post("/jobs", status_code=202)
def submit_job(payload: dict, auth=Depends(require_auth)):
    if not ASYNC_JOBS:
        raise HTTPException(404, "not found")
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {"submitted_at": time.time(), "payload": payload}
    return {"job_id": job_id, "status": "pending", "poll_url": f"/jobs/{job_id}"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, auth=Depends(require_auth)):
    if not ASYNC_JOBS:
        raise HTTPException(404, "not found")
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if time.time() - job["submitted_at"] < ASYNC_JOB_DELAY_SECONDS:
        return {"job_id": job_id, "status": "pending"}
    return {"job_id": job_id, "status": "done", "result": {"echoed": job["payload"]}}


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
