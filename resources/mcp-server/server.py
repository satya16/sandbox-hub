"""
A small sample MCP server (streamable-http transport) whose auth behavior is
controlled by env vars, so the same image serves as sandbox-hub's "plain",
"api-key", and "oauth" MCP server resources.

AUTH_MODE=none    -> no auth
AUTH_MODE=apikey  -> requires header  X-API-Key: <API_KEY>  (checked in ASGI middleware)
AUTH_MODE=oauth   -> requires header  Authorization: Bearer <token>, verified via
                     OAUTH_INTROSPECT_URL (uses mcp's native TokenVerifier support)
"""
import os
import time

import httpx
import uvicorn
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

AUTH_MODE = os.environ.get("AUTH_MODE", "none")
API_KEY = os.environ.get("API_KEY", "")
OAUTH_INTROSPECT_URL = os.environ.get("OAUTH_INTROSPECT_URL", "")
OAUTH_ISSUER_URL = os.environ.get("OAUTH_ISSUER_URL", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")


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


# Add directly to the mcp app's own router so the session manager's lifespan
# (started by uvicorn against this exact app object) still covers this route.
app.router.routes.insert(0, Route("/health", health))


class ApiKeyMiddleware:
    """Wraps the ASGI app; forwards lifespan events untouched so the MCP
    session manager's task group still gets initialized by uvicorn."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.inner(scope, receive, send)
            return
        headers = dict(scope["headers"])
        key = headers.get(b"x-api-key", b"").decode()
        if key != API_KEY:
            response = JSONResponse({"error": "missing or invalid X-API-Key header"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.inner(scope, receive, send)


if AUTH_MODE == "apikey":
    app = ApiKeyMiddleware(app)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
