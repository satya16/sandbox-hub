"""
A small sample MCP server (streamable-http transport) whose auth behavior is
controlled by env vars, so the same image serves every auth variant of
sandbox-hub's MCP server kind.

AUTH_MODE=none    -> no auth
AUTH_MODE=apikey  -> requires header  X-API-Key: <API_KEY>
AUTH_MODE=basic   -> requires HTTP Basic auth (BASIC_USERNAME/BASIC_PASSWORD)
AUTH_MODE=jwt     -> requires header  Authorization: Bearer <jwt>, a
                     self-contained HS256 JWT verified locally with JWT_SECRET
                     (no external calls) -- GET /_debug/token mints a fresh one
AUTH_MODE=session -> POST /login with {username,password} (SESSION_USERNAME/
                     SESSION_PASSWORD) sets a session cookie; that cookie then
                     gates the MCP endpoint; POST /logout clears it
AUTH_MODE=oauth   -> requires header  Authorization: Bearer <token>, verified via
                     OAUTH_INTROSPECT_URL (uses mcp's native TokenVerifier support)
AUTH_MODE=hmac    -> requires header  X-Signature: t=<unix_ts>,v1=<hex>, where
                     <hex> is hmac_sha256(HMAC_SECRET, f"{ts}.{raw_body}")
                     hex-encoded; ts must be within 300s of server time
"""
import base64
import hashlib
import hmac
import http.cookies
import os
import re
import secrets
import time

import httpx
import jwt as pyjwt
import uvicorn
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

AUTH_MODE = os.environ.get("AUTH_MODE", "none")
API_KEY = os.environ.get("API_KEY", "")
BASIC_USERNAME = os.environ.get("BASIC_USERNAME", "")
BASIC_PASSWORD = os.environ.get("BASIC_PASSWORD", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "86400"))
SESSION_USERNAME = os.environ.get("SESSION_USERNAME", "")
SESSION_PASSWORD = os.environ.get("SESSION_PASSWORD", "")
OAUTH_INTROSPECT_URL = os.environ.get("OAUTH_INTROSPECT_URL", "")
OAUTH_ISSUER_URL = os.environ.get("OAUTH_ISSUER_URL", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")
HMAC_SECRET = os.environ.get("HMAC_SECRET", "")
HMAC_TOLERANCE_SECONDS = 300
HMAC_SIGNATURE_RE = re.compile(r"^t=(\d+),v1=([0-9a-f]{64})$")

SESSION_COOKIE = "sandboxhub_session"
_active_sessions: set[str] = set()


class IntrospectionTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                resp = await client.post(OAUTH_INTROSPECT_URL, data={"token": token})
            except httpx.HTTPError:
                return None
        data = resp.json()
        if not data.get("active"):
            return None
        return AccessToken(
            token=token,
            client_id=data.get("client_id", "unknown"),
            scopes=(data.get("scope") or "").split(),
            expires_at=data.get("exp"),
        )


mcp_kwargs = {}
if AUTH_MODE == "oauth":
    mcp_kwargs["token_verifier"] = IntrospectionTokenVerifier()
    mcp_kwargs["auth"] = AuthSettings(
        issuer_url=OAUTH_ISSUER_URL,
        resource_server_url=PUBLIC_URL,
    )

mcp = MCPServer(name=f"sandbox-hub sample MCP server ({AUTH_MODE})", **mcp_kwargs)


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the given text."""
    return text


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@mcp.tool()
def current_time() -> str:
    """Return the current server time as a unix timestamp."""
    return str(time.time())


app = mcp.streamable_http_app(host="0.0.0.0")


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "auth_mode": AUTH_MODE})


def _mint_jwt() -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"sub": "sandbox-hub-tester", "iat": now, "exp": now + JWT_TTL_SECONDS},
        JWT_SECRET,
        algorithm="HS256",
    )


async def debug_token(request: Request) -> JSONResponse:
    if AUTH_MODE != "jwt":
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"token": _mint_jwt(), "expires_in": JWT_TTL_SECONDS})


async def login(request: Request) -> JSONResponse:
    if AUTH_MODE != "session":
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = await request.json()
    if not (
        secrets.compare_digest(str(payload.get("username", "")), SESSION_USERNAME)
        and secrets.compare_digest(str(payload.get("password", "")), SESSION_PASSWORD)
    ):
        return JSONResponse({"error": "invalid username/password"}, status_code=401)
    session_id = secrets.token_urlsafe(24)
    _active_sessions.add(session_id)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    return resp


async def logout(request: Request) -> JSONResponse:
    if AUTH_MODE != "session":
        return JSONResponse({"error": "not found"}, status_code=404)
    _active_sessions.discard(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# Added directly to the mcp app's own router (rather than a wrapping
# Starlette app) so the session manager's lifespan -- started by uvicorn
# against this exact app object -- still covers these routes.
app.router.routes[:0] = [
    Route("/health", health),
    Route("/_debug/token", debug_token),
    Route("/login", login, methods=["POST"]),
    Route("/logout", logout, methods=["POST"]),
]

UNAUTHED_PATHS = {"/health", "/_debug/token", "/login", "/logout"}


class AuthMiddleware:
    """Wraps the ASGI app; forwards lifespan events untouched so the MCP
    session manager's task group still gets initialized by uvicorn."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in UNAUTHED_PATHS:
            await self.inner(scope, receive, send)
            return

        headers = {k.decode(): v.decode() for k, v in scope["headers"]}

        # hmac needs the raw body to verify, which an ASGI receive() channel
        # only yields once -- buffer it here and hand the inner app a
        # replacement receive() that replays what was consumed.
        if AUTH_MODE == "hmac":
            body, receive = await self._buffer_body(receive)
            ok, error = self._check_hmac(headers, body)
            if not ok:
                response = JSONResponse({"error": error}, status_code=401)
                await response(scope, receive, send)
                return
            await self.inner(scope, receive, send)
            return

        ok, error, challenge = self._check(headers)
        if not ok:
            resp_headers = {"WWW-Authenticate": challenge} if challenge else {}
            response = JSONResponse({"error": error}, status_code=401, headers=resp_headers)
            await response(scope, receive, send)
            return
        await self.inner(scope, receive, send)

    @staticmethod
    async def _buffer_body(receive):
        body = b""
        messages = []
        more_body = True
        while more_body:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()

        return body, replay

    def _check_hmac(self, headers, body):
        sig_header = headers.get("x-signature", "")
        match = HMAC_SIGNATURE_RE.match(sig_header)
        if not match:
            return False, "missing or malformed X-Signature header"
        ts, sig = match.group(1), match.group(2)
        if abs(time.time() - int(ts)) > HMAC_TOLERANCE_SECONDS:
            return False, "signature timestamp outside tolerance window"
        expected = hmac.new(HMAC_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False, "invalid signature"
        return True, None

    def _check(self, headers):
        if AUTH_MODE == "apikey":
            if headers.get("x-api-key") != API_KEY:
                return False, "missing or invalid X-API-Key header", None
            return True, None, None

        if AUTH_MODE == "basic":
            auth = headers.get("authorization", "")
            if not auth.startswith("Basic "):
                return False, "missing Basic auth", "Basic"
            try:
                decoded = base64.b64decode(auth.removeprefix("Basic ").strip()).decode()
                user, _, pwd = decoded.partition(":")
            except Exception:
                return False, "malformed Basic auth header", "Basic"
            if not (secrets.compare_digest(user, BASIC_USERNAME) and secrets.compare_digest(pwd, BASIC_PASSWORD)):
                return False, "invalid credentials", "Basic"
            return True, None, None

        if AUTH_MODE == "jwt":
            auth = headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return False, "missing Bearer token", None
            token = auth.removeprefix("Bearer ").strip()
            try:
                pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            except pyjwt.ExpiredSignatureError:
                return False, "token expired", None
            except pyjwt.InvalidTokenError as exc:
                return False, f"invalid token: {exc}", None
            return True, None, None

        if AUTH_MODE == "session":
            cookie_header = headers.get("cookie", "")
            jar = http.cookies.SimpleCookie()
            jar.load(cookie_header)
            session_id = jar[SESSION_COOKIE].value if SESSION_COOKIE in jar else None
            if not session_id or session_id not in _active_sessions:
                return False, "not logged in -- POST /login first", None
            return True, None, None

        return True, None, None  # "none" and "oauth" (oauth handled natively by mcp itself)


if AUTH_MODE in ("apikey", "basic", "jwt", "session", "hmac"):
    app = AuthMiddleware(app)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
