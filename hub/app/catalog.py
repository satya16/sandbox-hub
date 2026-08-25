"""
Static catalog of everything sandbox-hub can spin up. Each entry describes a
Docker container the hub can create/start/stop, driven entirely off this
table -- there is no per-resource code elsewhere in the hub.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ResourceDef:
    id: str
    category: str  # "infra" | "rest-api" | "mcp-server"
    name: str
    description: str
    image: str
    container_port: int
    host_port: int
    auth_mode: str  # "none" | "apikey" | "oauth" | "n/a" (infra)
    requires: tuple[str, ...] = field(default_factory=tuple)
    health_path: str = "/health"


CATALOG: dict[str, ResourceDef] = {
    d.id: d
    for d in [
        ResourceDef(
            id="oauth-provider",
            category="infra",
            name="Local OAuth2 Provider",
            description=(
                "Minimal OAuth2 authorization server (client_credentials + "
                "authorization_code/PKCE) used to back the OAuth-gated "
                "resources below. Started automatically when needed."
            ),
            image="sandboxhub/oauth-provider:latest",
            container_port=8000,
            host_port=8199,
            auth_mode="n/a",
        ),
        ResourceDef(
            id="rest-none",
            category="rest-api",
            name="REST API",
            description="Sample REST API with no authentication.",
            image="sandboxhub/rest-api:latest",
            container_port=8000,
            host_port=8101,
            auth_mode="none",
        ),
        ResourceDef(
            id="rest-apikey",
            category="rest-api",
            name="REST API (API key)",
            description="Sample REST API gated behind a static X-API-Key header.",
            image="sandboxhub/rest-api:latest",
            container_port=8000,
            host_port=8102,
            auth_mode="apikey",
        ),
        ResourceDef(
            id="rest-oauth",
            category="rest-api",
            name="REST API (OAuth2)",
            description="Sample REST API gated behind an OAuth2 Bearer token.",
            image="sandboxhub/rest-api:latest",
            container_port=8000,
            host_port=8103,
            auth_mode="oauth",
            requires=("oauth-provider",),
        ),
        ResourceDef(
            id="mcp-none",
            category="mcp-server",
            name="MCP Server",
            description="Sample MCP server (streamable-http) with no authentication.",
            image="sandboxhub/mcp-server:latest",
            container_port=8000,
            host_port=8111,
            auth_mode="none",
        ),
        ResourceDef(
            id="mcp-apikey",
            category="mcp-server",
            name="MCP Server (API key)",
            description="Sample MCP server gated behind a static X-API-Key header.",
            image="sandboxhub/mcp-server:latest",
            container_port=8000,
            host_port=8112,
            auth_mode="apikey",
        ),
        ResourceDef(
            id="mcp-oauth",
            category="mcp-server",
            name="MCP Server (OAuth2)",
            description="Sample MCP server gated behind OAuth2 (spec-compliant Bearer + WWW-Authenticate).",
            image="sandboxhub/mcp-server:latest",
            container_port=8000,
            host_port=8113,
            auth_mode="oauth",
            requires=("oauth-provider",),
        ),
    ]
}
