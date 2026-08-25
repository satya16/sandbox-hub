"""
Accepts any HTTP request at any path, logs it (so it shows up live in
sandbox-hub's log stream), records it in a small in-memory buffer, and
always responds 200 -- point a webhook sender at this and watch payloads
arrive.

Auth behavior (applies to the catch-all only, not /_requests or /health) is
controlled by the same AUTH_MODE env vars as the REST API resource: none,
apikey, basic, jwt, session, oauth.
"""
import base64
import collections
import json
import os
import secrets
import time
from datetime import datetime, timezone

import httpx
import jwt as pyjwt
from fastapi import FastAPI, HTTPException, Request
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

SESSION_COOKIE = "sandboxhub_session"
_active_sessions: set[str] = set()

app = FastAPI(title="sandbox-hub webhook receiver")

_requests: collections.deque = collections.deque(maxlen=200)


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


@app.get("/health")
def health():
    return {"status": "ok", "auth_mode": AUTH_MODE, "captured": len(_requests)}


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


@app.get("/_requests")
def list_requests():
    return list(_requests)


@app.get("/_requests/last")
def last_request():
    if not _requests:
        raise HTTPException(404, "no requests received yet")
    return _requests[-1]


@app.delete("/_requests")
def clear_requests():
    _requests.clear()
    return {"ok": True}


@app.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def catch_all(request: Request, full_path: str = ""):
    await require_auth(request)

    raw = await request.body()
    body_text = raw.decode(errors="replace")
    try:
        body_json = json.loads(body_text) if body_text else None
    except json.JSONDecodeError:
        body_json = None

    entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "method": request.method,
        "path": "/" + full_path,
        "query": dict(request.query_params),
        "headers": {k: v for k, v in request.headers.items() if k.lower() not in ("authorization", "cookie")},
        "body_json": body_json,
        "body_text": None if body_json is not None else (body_text or None),
    }
    _requests.append(entry)
    print(f"[webhook] {entry['method']} {entry['path']} query={entry['query']} body={body_json if body_json is not None else body_text!r}", flush=True)

    return JSONResponse({"received": True, "id": len(_requests)})
