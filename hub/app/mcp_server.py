"""
Exposes sandbox-hub's own control plane as MCP tools, mounted at /mcp on
the hub itself -- so an agent can create and configure test resources
directly over MCP instead of shelling out to curl against the REST API in
main.py. Every tool here calls straight into docker_manager, the same
functions the REST API calls, so behavior is identical between the two;
this is just a second, agent-shaped front door onto the same control
plane, for the same audience the rest of sandbox-hub is built for --
"building a client (an app, an agent, an MCP client)" per the README.

Deliberately a subset of the REST API for a first pass: covers instance
lifecycle, scenarios, and the three "define your own behavior live" kinds
(Mock API, GraphQL API, Chaos API). Not yet covered: the webhook-receiver
request log, deleting a single GraphQL resolver (clear + re-add both
covers it and is simpler to expose), and oauth-provider status.

No auth of its own -- same as the rest of the hub's API, this is a local
control plane for disposable test infra, not something to expose beyond
that.
"""
import functools
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import docker_manager as dm
from .catalog import AUTH_MODES, KINDS


def _tool(fn):
    """@mcp.tool() masks any exception that isn't its own ToolError into a
    generic "Error executing tool <name>", withholding the real message
    from the caller -- reasonable for a tool wrapping code you don't
    trust, but here it would hide exactly the detail an agent needs
    (a validation message, "no instance ...", a kind mismatch). Every tool
    below raises plain ValueError for an anticipated, safe-to-show failure
    (mirroring the REST API's own except ValueError: 400 convention, same
    source in docker_manager either way); this reclassifies those as
    ToolError so their message actually reaches the caller."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper

mcp = MCPServer(
    name="sandbox-hub",
    instructions=(
        "Control plane for sandbox-hub, a local host for disposable test "
        "resources (REST/GraphQL/mock APIs, an MCP server, a webhook "
        "receiver, a chaos/rate-limit endpoint). Call list_kinds first to "
        "see what can be created and with which auth modes, then "
        "create_instance. Each created instance's response carries its "
        "own URL and any generated credentials -- read those from the "
        "response rather than guessing at them."
    ),
)


def _require_instance(instance_id: str) -> dict:
    detail = dm.instance_detail(instance_id)
    if detail is None:
        raise ValueError(f"no instance {instance_id!r} -- call list_instances to see what exists")
    return detail


def _require_kind(instance_id: str, kind: str) -> dict:
    detail = _require_instance(instance_id)
    if detail["kind"] != kind:
        raise ValueError(f"{instance_id!r} is a {detail['kind']!r} instance, not {kind!r}")
    return detail


@mcp.tool()
@_tool
def list_kinds() -> dict:
    """List the resource kinds sandbox-hub can create (rest-api, mock-api,
    graphql-api, ...), what each one supports, and the valid auth modes."""
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
    }


@mcp.tool()
@_tool
def list_instances() -> list[dict]:
    """List every sandbox-hub instance -- kind, state, URL, and auth details."""
    return dm.list_instances()


@mcp.tool()
@_tool
def get_instance(instance_id: str) -> dict:
    """Get one instance's full detail: URL, auth, and kind-specific state
    (its Mock API routes, GraphQL schema/resolvers, or Chaos config)."""
    return _require_instance(instance_id)


@mcp.tool()
@_tool
def create_instance(
    kind: str,
    name: Optional[str] = None,
    auth_mode: str = "none",
    openapi_version: Optional[str] = None,
    openapi_protect: bool = False,
    async_jobs: bool = False,
    async_job_delay_seconds: int = 5,
) -> dict:
    """Create and start a new instance of the given kind (see list_kinds
    for valid kinds and auth modes). Returns its URL and auth details --
    any credentials are generated fresh, read them from the response."""
    kdef = KINDS.get(kind)
    if kdef is None:
        raise ValueError(f"unknown kind {kind!r} -- call list_kinds for the valid ones")
    config: dict[str, Any] = {"auth_mode": auth_mode}
    if kdef.supports_openapi:
        config["openapi_version"] = openapi_version or "3.1"
        config["openapi_protect"] = openapi_protect
    if kdef.supports_async_job:
        config["async_jobs"] = async_jobs
        config["async_job_delay_seconds"] = async_job_delay_seconds
    return dm.create_instance(kind, name, config)


@mcp.tool()
@_tool
def delete_instance(instance_id: str) -> dict:
    """Stop and remove an instance. Not reversible -- export_scenario
    first if you might want it back."""
    _require_instance(instance_id)
    dm.remove_instance(instance_id)
    return {"ok": True}


@mcp.tool()
@_tool
def rotate_instance_credentials(instance_id: str) -> dict:
    """Rotate an instance's generated credential (API key, password, JWT
    secret, or OAuth client) by recreating its container on the same port.
    Live-configured state (Mock API routes, GraphQL schema, Chaos config)
    survives the recreate. Fails if the instance has nothing rotatable."""
    _require_instance(instance_id)
    return dm.rotate_instance(instance_id)


@mcp.tool()
@_tool
def update_instance_port(instance_id: str, port: int) -> dict:
    """Move an instance to a different host port, recreating its
    container. Live-configured state survives the recreate."""
    _require_instance(instance_id)
    if not (1 <= port <= 65535):
        raise ValueError("port must be between 1 and 65535")
    return dm.update_port(instance_id, port)


@mcp.tool()
@_tool
def get_instance_logs(instance_id: str, tail: int = 200) -> str:
    """Get an instance's recent container log output."""
    _require_instance(instance_id)
    return dm.logs(instance_id, tail=tail)


@mcp.tool()
@_tool
def export_scenario(instance_ids: Optional[list[str]] = None) -> dict:
    """Export instances (every one, if instance_ids is omitted) as a
    scenario: each one's kind, creation config, and live-configured state,
    with no secrets -- credentials are never included. Feed the result to
    import_scenario to recreate it, elsewhere or after deleting these."""
    return dm.export_scenario(instance_ids)


@mcp.tool()
@_tool
def import_scenario(scenario: dict[str, Any]) -> list[dict]:
    """Recreate every instance in a scenario (as produced by
    export_scenario), each with freshly generated credentials. Validates
    the whole scenario before creating anything, so a bad entry can't
    leave a partial import behind."""
    return dm.import_scenario(scenario)


@mcp.tool()
@_tool
def add_mock_route(
    instance_id: str,
    path: str,
    method: str = "*",
    type: str = "static",
    status_code: int = 200,
    response_body: Any = None,
    required_fields: Optional[list[str]] = None,
    latency_ms: int = 0,
    failure_rate: float = 0.0,
    seed: Optional[list[dict]] = None,
    id_field: str = "id",
) -> dict:
    """Add a route to a Mock API instance. path can use {name} segments
    for path parameters ("/users/{id}"), readable in templates as
    {{request.params.id}}. type="static" answers every matching request
    with response_body, a JSON value whose strings support
    {{request.body.x}}, {{request.query.x}}, {{request.params.x}},
    {{uuid}}, {{now}} templates. type="crud" instead serves a real
    in-memory collection seeded from seed: list/create at path, and
    get/replace/merge/delete at path/{id} (id_field names the id
    property)."""
    _require_kind(instance_id, "mock-api")
    payload = {
        "type": type,
        "method": method,
        "path": path,
        "status_code": status_code,
        "response_body": response_body if response_body is not None else {"ok": True},
        "required_fields": required_fields or [],
        "latency_ms": latency_ms,
        "failure_rate": failure_rate,
        "seed": seed or [],
        "id_field": id_field,
    }
    return dm.create_route(instance_id, payload)


@mcp.tool()
@_tool
def delete_mock_route(instance_id: str, route_id: str) -> dict:
    """Delete one route from a Mock API instance."""
    _require_kind(instance_id, "mock-api")
    dm.delete_route(instance_id, route_id)
    return {"ok": True}


@mcp.tool()
@_tool
def import_openapi_to_mock(
    instance_id: str,
    spec: Any = None,
    url: Optional[str] = None,
    replace: bool = False,
) -> dict:
    """Generate Mock API routes from an OpenAPI 3.x document -- one static
    route per operation, answering with its documented example or a
    sample built from its response schema. Give exactly one of spec (the
    parsed document, or its JSON/YAML text) or url (fetched from the
    instance's own container). Set replace to clear existing routes
    first."""
    _require_kind(instance_id, "mock-api")
    if (spec is None) == (url is None):
        raise ValueError("give exactly one of spec or url")
    payload: dict[str, Any] = {"replace": replace}
    if url is not None:
        payload["url"] = url
    else:
        payload["spec"] = spec
    return dm.import_openapi_routes(instance_id, payload)


@mcp.tool()
@_tool
def set_graphql_schema(instance_id: str, sdl: str) -> dict:
    """Replace a GraphQL API instance's schema (SDL text). Resolvers for
    Query/Mutation fields the new schema no longer has are dropped."""
    _require_kind(instance_id, "graphql-api")
    return dm.set_graphql_schema(instance_id, sdl)


@mcp.tool()
@_tool
def set_graphql_resolver(
    instance_id: str,
    type: str,
    field: str,
    response_body: Any = None,
    error_message: Optional[str] = None,
    error_extensions: Optional[dict] = None,
) -> dict:
    """Set (or replace) a Query/Mutation field's mock resolver on a
    GraphQL API instance. Give response_body for a normal reply -- a JSON
    value whose strings support {{request.args.x}},
    {{request.variables.x}}, {{uuid}}, {{now}} templates -- or
    error_message to make that field always raise a GraphQL error
    instead."""
    _require_kind(instance_id, "graphql-api")
    payload: dict[str, Any] = {"type": type, "field": field, "response_body": response_body}
    if error_message is not None:
        payload["error"] = {"message": error_message, "extensions": error_extensions}
    return dm.set_graphql_resolver(instance_id, payload)


@mcp.tool()
@_tool
def set_chaos_config(
    instance_id: str,
    mode: str,
    rate_limit_requests: Optional[int] = None,
    rate_limit_window_seconds: Optional[int] = None,
    chaos_status_code: Optional[int] = None,
    chaos_body: Optional[dict] = None,
    chaos_latency_ms: Optional[int] = None,
    chaos_failure_rate: Optional[float] = None,
) -> dict:
    """Configure a Chaos/Rate-Limit API instance's /test endpoint. mode is
    "normal" (always 200), "rate_limit" (rate_limit_requests per
    rate_limit_window_seconds, then 429 + Retry-After), or "chaos" (always
    chaos_status_code + chaos_body, with chaos_latency_ms of injected
    delay and a chaos_failure_rate fraction of requests instead getting a
    random 500/502/503/504)."""
    _require_kind(instance_id, "chaos-api")
    payload: dict[str, Any] = {"mode": mode}
    if rate_limit_requests is not None or rate_limit_window_seconds is not None:
        payload["rate_limit"] = {
            "limit": rate_limit_requests if rate_limit_requests is not None else 5,
            "window_seconds": rate_limit_window_seconds if rate_limit_window_seconds is not None else 10,
        }
    if any(v is not None for v in (chaos_status_code, chaos_body, chaos_latency_ms, chaos_failure_rate)):
        payload["chaos"] = {
            "status_code": chaos_status_code if chaos_status_code is not None else 200,
            "body": chaos_body if chaos_body is not None else {"ok": True},
            "latency_ms": chaos_latency_ms if chaos_latency_ms is not None else 0,
            "failure_rate": chaos_failure_rate if chaos_failure_rate is not None else 0.0,
        }
    return dm.configure_chaos(instance_id, payload)


# Endpoint ends up at /mcp -- main.py splices this app's routes directly
# into the hub's own router rather than app.mount()-ing it under a "/mcp"
# prefix, which would make the actual path /mcp/mcp (the default here).
mcp_app = mcp.streamable_http_app()
