"""
Schema for what sandbox-hub can create. Unlike a fixed catalog of toggleable
resources, this only describes the *kinds* of things the hub knows how to
run and the settings each kind accepts -- actual running things are
instances, created on demand with whatever settings the user picks, and any
number of them can exist at once.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KindDef:
    id: str
    label: str
    description: str
    image: str
    container_port: int
    supports_openapi: bool = False


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
    ]
}

AUTH_MODES = ["none", "apikey", "oauth"]
OPENAPI_VERSIONS = ["3.0", "3.1"]

OAUTH_PROVIDER_IMAGE = "sandboxhub/oauth-provider:latest"
OAUTH_PROVIDER_CONTAINER_PORT = 8000
