"""
Schema for what sandbox-hub can create. Unlike a fixed catalog of toggleable
resources, this only describes the *kinds* of things the hub knows how to
run and the settings each kind accepts -- actual running things are
instances, created on demand with whatever settings the user picks, and any
number of them can exist at once.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class KindDef:
    id: str
    label: str
    description: str
    image: str
    container_port: int
    supports_openapi: bool = False
    supports_auth: bool = True
    supports_chaos_config: bool = False
    supports_routes: bool = False


KINDS: dict[str, KindDef] = {
    d.id: d
    for d in [
        KindDef(
            id="rest-api",
            label="REST API",
            description="A small sample REST API (/items).",
            image="sandboxhub/rest-api:latest",
            container_port=8000,
            supports_openapi=True,
        ),
        KindDef(
            id="mcp-server",
            label="MCP Server",
            description="A small sample MCP server (streamable-http) with echo/add/current_time tools.",
            image="sandboxhub/mcp-server:latest",
            container_port=8000,
        ),
        KindDef(
            id="mock-api",
            label="Mock API",
            description="Define your own routes: method + path (or * for catch-all), status, and a templated JSON response.",
            image="sandboxhub/mock-api:latest",
            container_port=8000,
            supports_routes=True,
        ),
        KindDef(
            id="webhook-receiver",
            label="Webhook Receiver",
            description="Accepts any request at any path, logs it live, and always responds 200 -- point a webhook sender at it and watch payloads arrive.",
            image="sandboxhub/webhook-receiver:latest",
            container_port=8000,
        ),
        KindDef(
            id="chaos-api",
            label="Rate Limit / Chaos API",
            description="A single test endpoint you can put into rate-limiting mode (429s) or chaos mode (pick the status/body/latency/failure rate).",
            image="sandboxhub/chaos-api:latest",
            container_port=8000,
            supports_auth=False,
            supports_chaos_config=True,
        ),
    ]
}

AUTH_MODES = ["none", "apikey", "basic", "jwt", "session", "oauth"]
OPENAPI_VERSIONS = ["3.0", "3.1"]

OAUTH_PROVIDER_IMAGE = "sandboxhub/oauth-provider:latest"
OAUTH_PROVIDER_CONTAINER_PORT = 8000

# Shared secret the hub uses to call the private admin endpoints it creates
# on oauth-provider / chaos-api / mock-api containers. Not meant to protect
# against anything beyond "don't let the resource's own test traffic hit
# these by accident" -- everything here binds to 127.0.0.1 anyway.
ADMIN_TOKEN_DEFAULT = "dev-admin-token"
