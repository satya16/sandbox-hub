"""
User-defined routes: method + path (or "*" for "any"/catch-all) mapped to a
status code and a templated JSON response. Routes are managed live via an
admin API the hub calls on your behalf -- no restart needed to add, edit, or
remove one.

Template placeholders (used inside string values of response_body):
  {{request.body.<dotted.path>}}   value from the parsed JSON request body
  {{request.query.<param>}}        a query string parameter
  {{request.headers.<name>}}       a request header (lowercase name)
  {{request.method}} {{request.path}}
  {{uuid}}                         a fresh random uuid4
  {{now}}                          current UTC timestamp, ISO 8601
A value that is *exactly* "{{expr}}" is replaced with the real JSON value
(so {{request.body.count}} stays a number); embedded inside a longer string
it's stringified.

A route can require certain fields be present in the JSON request body
(dotted paths) -- if missing, the mock responds 400 instead of the
configured response, letting you test client behavior against a schema you
define rather than a fixed sample one.

Auth (applies to the mock routes only, not /_routes/*, /health) reuses the
same AUTH_MODE options as the REST API resource: none, apikey, basic, jwt,
session, oauth.
"""
import base64
import json
import os
import re
import secrets
import time
import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt as pyjwt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

AUTH_MODE = os.environ.get("AUTH_MODE", "none")
API_KEY = os.environ.get("API_KEY", "")
BASIC_USERNAME = os.environ.get("BASIC_USERNAME", "")
BASIC_PASSWORD = os.environ.get("BASIC_PASSWORD", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "86400"))
SESSION_USERNAME = os.environ.get("SESSION_USERNAME", "")
SESSION_PASSWORD = os.environ.get("SESSION_PASSWORD", "")
OAUTH_INTROSPECT_URL = os.environ.get("OAUTH_INTROSPECT_URL", "")
ADMIN_TOKEN = os.environ.get("MOCK_ADMIN_TOKEN", "dev-admin-token")

SESSION_COOKIE = "sandboxhub_session"
_active_sessions: set[str] = set()

app = FastAPI(title="sandbox-hub mock API")

routes: dict[str, dict] = {}
RESERVED_PATHS = {"/_routes", "/health", "/login", "/logout", "/_debug/token"}


class RouteIn(BaseModel):
    method: str = "*"
    path: str = "*"
    status_code: int = 200
    response_body: Any = {"ok": True}
    required_fields: list[str] = []


def _mint_jwt() -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"sub": "sandbox-hub-tester", "iat": now, "exp": now + JWT_TTL_SECONDS},
        JWT_SECRET,
        algorithm="HS256",
    )


async def require_auth(request: Request):
    if AUTH_MODE == "none":
        return
    if AUTH_MODE == "apikey":
        if request.headers.get("x-api-key") != API_KEY:
            raise HTTPException(401, "missing or invalid X-API-Key header")
        return
    if AUTH_MODE == "basic":
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            raise HTTPException(401, "missing Basic auth", headers={"WWW-Authenticate": "Basic"})
        try:
            decoded = base64.b64decode(auth.removeprefix("Basic ").strip()).decode()
            user, _, pwd = decoded.partition(":")
        except Exception:
            raise HTTPException(401, "malformed Basic auth header", headers={"WWW-Authenticate": "Basic"})
        if not (secrets.compare_digest(user, BASIC_USERNAME) and secrets.compare_digest(pwd, BASIC_PASSWORD)):
            raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Basic"})
        return
    if AUTH_MODE == "jwt":
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "missing Bearer token")
        try:
            pyjwt.decode(auth.removeprefix("Bearer ").strip(), JWT_SECRET, algorithms=["HS256"])
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(401, "token expired")
        except pyjwt.InvalidTokenError as exc:
            raise HTTPException(401, f"invalid token: {exc}")
        return
    if AUTH_MODE == "session":
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id or session_id not in _active_sessions:
            raise HTTPException(401, "not logged in -- POST /login first")
        return
    if AUTH_MODE == "oauth":
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "missing Bearer token")
        token = auth.removeprefix("Bearer ").strip()
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                resp = await client.post(OAUTH_INTROSPECT_URL, data={"token": token})
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"could not reach oauth provider: {exc}")
        if not resp.json().get("active"):
            raise HTTPException(401, "token is not active")
        return


def require_admin(x_admin_token: str | None):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")


@app.get("/health")
def health():
    return {"status": "ok", "auth_mode": AUTH_MODE, "routes": len(routes)}


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


@app.get("/_debug/token", include_in_schema=False)
def debug_token():
    if AUTH_MODE != "jwt":
        raise HTTPException(404, "not found")
    return {"token": _mint_jwt(), "expires_in": JWT_TTL_SECONDS}


@app.get("/_routes")
def list_routes():
    return list(routes.values())


@app.post("/_routes")
def create_route(route: RouteIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    route_id = secrets.token_hex(4)
    entry = {"id": route_id, **route.model_dump()}
    routes[route_id] = entry
    return entry


@app.put("/_routes/{route_id}")
def update_route(route_id: str, route: RouteIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    if route_id not in routes:
        raise HTTPException(404, "unknown route")
    entry = {"id": route_id, **route.model_dump()}
    routes[route_id] = entry
    return entry


@app.delete("/_routes/{route_id}")
def delete_route(route_id: str, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    routes.pop(route_id, None)
    return {"ok": True}


@app.delete("/_routes")
def clear_routes(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    routes.clear()
    return {"ok": True}


TOKEN_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _dig(obj, parts):
    cur = obj
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _resolve_token(expr: str, ctx: dict):
    if expr == "uuid":
        return str(uuidlib.uuid4())
    if expr == "now":
        return datetime.now(timezone.utc).isoformat()
    if expr == "request.method":
        return ctx["method"]
    if expr == "request.path":
        return ctx["path"]
    parts = expr.split(".")
    if parts[0] == "request" and len(parts) >= 2:
        root = ctx.get(parts[1])
        return _dig(root, parts[2:]) if len(parts) > 2 else root
    return None


def render_template(value, ctx):
    if isinstance(value, str):
        full = TOKEN_RE.fullmatch(value.strip())
        if full:
            return _resolve_token(full.group(1), ctx)

        def _sub(m):
            v = _resolve_token(m.group(1), ctx)
            if v is None:
                return ""
            return v if isinstance(v, str) else json.dumps(v)

        return TOKEN_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: render_template(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, ctx) for v in value]
    return value


def _find_route(method: str, path: str):
    for r in routes.values():
        method_ok = r["method"] == "*" or r["method"].upper() == method.upper()
        path_ok = r["path"] == "*" or r["path"] == path
        if method_ok and path_ok:
            return r
    return None


@app.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def mock_dispatch(request: Request, full_path: str = ""):
    path = "/" + full_path
    if path in RESERVED_PATHS:
        raise HTTPException(404, "not found")

    await require_auth(request)

    raw = await request.body()
    try:
        body_json = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body_json = {}

    route = _find_route(request.method, path)
    if route is None:
        return JSONResponse(
            {"error": "no matching mock route", "method": request.method, "path": path}, status_code=404
        )

    missing = [f for f in route["required_fields"] if _dig(body_json, f.split(".")) is None]
    if missing:
        return JSONResponse({"error": "missing required field(s)", "fields": missing}, status_code=400)

    ctx = {
        "method": request.method,
        "path": path,
        "query": dict(request.query_params),
        "headers": {k.lower(): v for k, v in request.headers.items()},
        "body": body_json,
    }
    rendered = render_template(route["response_body"], ctx)
    return JSONResponse(rendered, status_code=route["status_code"])
