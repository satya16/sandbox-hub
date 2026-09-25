"""
All Docker Engine interaction lives here, via the Docker SDK against
/var/run/docker.sock. Every user-created thing is an *instance*: a sibling
container the hub creates on demand with whatever config the user picked,
identified by a generated id, freely creatable in any number and combination.
Docker itself is the source of truth for what exists -- instances are
discovered by label filter, not tracked in a separate store. The one piece of
state the hub keeps in memory is the set of OAuth client credentials it has
registered with the (also ephemeral) local oauth-provider.
"""
import json
import os
import secrets
import socket
import time
from typing import Optional

import docker
import httpx
from docker.errors import NotFound

from .catalog import AUTH_MODES, KINDS, OAUTH_PROVIDER_CONTAINER_PORT, OAUTH_PROVIDER_IMAGE

NETWORK_NAME = os.environ.get("SANDBOXHUB_NETWORK", "sandboxhub-net")
BIND_HOST = os.environ.get("SANDBOXHUB_BIND_HOST", "127.0.0.1")
CONTAINER_PREFIX = "sandboxhub-"
OAUTH_PROVIDER_NAME = f"{CONTAINER_PREFIX}oauth-provider"
ADMIN_TOKEN = os.environ.get("OAUTH_ADMIN_TOKEN", "dev-admin-token")
CHAOS_ADMIN_TOKEN = os.environ.get("CHAOS_ADMIN_TOKEN", "dev-admin-token")
MOCK_ADMIN_TOKEN = os.environ.get("MOCK_ADMIN_TOKEN", "dev-admin-token")
GRAPHQL_ADMIN_TOKEN = os.environ.get("GRAPHQL_ADMIN_TOKEN", "dev-admin-token")

LABEL_MANAGED = "sandboxhub.managed"
LABEL_ROLE = "sandboxhub.role"  # "instance" | "infra"
LABEL_KIND = "sandboxhub.kind"
LABEL_NAME = "sandboxhub.name"
LABEL_CONFIG = "sandboxhub.config"

client = docker.from_env()

# instance_id -> {"client_id": ..., "client_secret": ...}; resets when the
# hub or the oauth-provider container restarts, same as the provider's own
# in-memory client store.
_oauth_client_cache: dict[str, dict] = {}

# instance_id -> a currently-valid JWT for "jwt" auth mode instances. The
# resource itself mints these statelessly from JWT_SECRET on request, so this
# is just a convenience cache to avoid an extra round trip on every list call.
_jwt_token_cache: dict[str, str] = {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((BIND_HOST, 0))
        return s.getsockname()[1]


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((BIND_HOST, port))
            return True
        except OSError:
            return False


def _self_container():
    """The hub's own container, identified by hostname -- Docker sets a
    container's hostname to its own short id unless overridden, and the
    hub image never overrides it."""
    try:
        return client.containers.get(socket.gethostname())
    except NotFound:
        return None


def ensure_network():
    try:
        network = client.networks.get(NETWORK_NAME)
    except NotFound:
        network = client.networks.create(NETWORK_NAME, driver="bridge")

    # The hub calls each instance's (and oauth-provider's) admin API by
    # container name -- mock/graphql/chaos config, OAuth client
    # registration, the post-create health check -- which only resolves
    # for containers on the same Docker network. The one-line quickstart
    # `docker run` for the hub itself has no reason to know this network's
    # name, so the hub joins itself here instead of requiring --network.
    # Connecting a running container to a network takes effect immediately,
    # no restart needed, so this also self-heals a hub already running
    # without it.
    self_container = _self_container()
    if self_container is not None:
        self_container.reload()
        if NETWORK_NAME not in self_container.attrs.get("NetworkSettings", {}).get("Networks", {}):
            network.connect(self_container)


def _container_env(container) -> dict[str, str]:
    env_list = container.attrs.get("Config", {}).get("Env", []) or []
    out = {}
    for entry in env_list:
        if "=" in entry:
            k, v = entry.split("=", 1)
            out[k] = v
    return out


def _host_port(container, container_port: int) -> Optional[int]:
    container.reload()
    bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    entries = bindings.get(f"{container_port}/tcp")
    if not entries:
        return None
    return int(entries[0]["HostPort"])


def _internal_url(container_name: str, container_port: int, path: str) -> str:
    return f"http://{container_name}:{container_port}{path}"


def _wait_for_http(url: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)


def _instance_internal_url(instance_id: str, kind: str, path: str) -> str:
    return _internal_url(_container_name(instance_id), KINDS[kind].container_port, path)


def _request(method: str, url: str, timeout: float = 3.0, **kwargs) -> httpx.Response:
    """httpx request with a short retry against connection errors -- calls
    into an instance's own admin endpoints can otherwise race a
    just-created container whose app hasn't finished starting yet."""
    deadline = time.time() + timeout
    while True:
        try:
            resp = httpx.request(method, url, timeout=2, **kwargs)
            resp.raise_for_status()
            return resp
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if time.time() >= deadline:
                raise
            time.sleep(0.2)


# ---------------------------------------------------------------- oauth infra


def oauth_provider_status() -> dict:
    try:
        c = client.containers.get(OAUTH_PROVIDER_NAME)
    except NotFound:
        return {"state": "stopped", "url": None}
    port = _host_port(c, OAUTH_PROVIDER_CONTAINER_PORT)
    return {"state": c.status, "url": f"http://localhost:{port}" if port else None}


def _ensure_oauth_provider() -> int:
    """Start the local oauth-provider if it isn't running; return its host port."""
    ensure_network()
    try:
        c = client.containers.get(OAUTH_PROVIDER_NAME)
        c.reload()
        if c.status != "running":
            c.start()
    except NotFound:
        port = _free_port()
        client.containers.run(
            OAUTH_PROVIDER_IMAGE,
            name=OAUTH_PROVIDER_NAME,
            detach=True,
            network=NETWORK_NAME,
            ports={f"{OAUTH_PROVIDER_CONTAINER_PORT}/tcp": (BIND_HOST, port)},
            environment={"OAUTH_ADMIN_TOKEN": ADMIN_TOKEN},
            labels={LABEL_MANAGED: "true", LABEL_ROLE: "infra"},
            restart_policy={"Name": "unless-stopped"},
        )
        c = client.containers.get(OAUTH_PROVIDER_NAME)

    _wait_for_http(_internal_url(OAUTH_PROVIDER_NAME, OAUTH_PROVIDER_CONTAINER_PORT, "/health"))
    return _host_port(c, OAUTH_PROVIDER_CONTAINER_PORT)


def _register_oauth_client(instance_id: str) -> dict:
    url = _internal_url(OAUTH_PROVIDER_NAME, OAUTH_PROVIDER_CONTAINER_PORT, "/admin/clients")
    resp = httpx.post(
        url,
        json={"name": f"sandbox-hub: {instance_id}"},
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=5,
    )
    resp.raise_for_status()
    creds = resp.json()
    _oauth_client_cache[instance_id] = creds
    return creds


def _maybe_stop_oauth_provider():
    """If no live instance still needs OAuth, tear the provider down too --
    it's disposable test infra, not worth keeping around idle."""
    for detail in list_instances():
        if detail["state"] == "running" and detail.get("auth", {}).get("mode") == "oauth":
            return
    try:
        c = client.containers.get(OAUTH_PROVIDER_NAME)
        c.stop(timeout=5)
        c.remove()
    except NotFound:
        pass
    _oauth_client_cache.clear()


# ------------------------------------------------------------------ instances


def _container_name(instance_id: str) -> str:
    return f"{CONTAINER_PREFIX}{instance_id}"


def _get_container(instance_id: str):
    try:
        return client.containers.get(_container_name(instance_id))
    except NotFound:
        return None


def _build_env(kind: str, config: dict) -> dict:
    auth_mode = config.get("auth_mode", "none")
    env = {"AUTH_MODE": auth_mode}

    if kind == "rest-api":
        env["OPENAPI_VERSION"] = config.get("openapi_version", "3.1")
        if config.get("openapi_protect"):
            env["OPENAPI_PROTECT"] = "true"
            env["OPENAPI_TOKEN"] = secrets.token_urlsafe(18)
        if config.get("async_jobs"):
            env["ASYNC_JOBS"] = "true"
            env["ASYNC_JOB_DELAY_SECONDS"] = str(config.get("async_job_delay_seconds", 5))

    if auth_mode == "apikey":
        env["API_KEY"] = secrets.token_urlsafe(18)
    elif auth_mode == "basic":
        env["BASIC_USERNAME"] = "sandbox"
        env["BASIC_PASSWORD"] = secrets.token_urlsafe(12)
    elif auth_mode == "jwt":
        env["JWT_SECRET"] = secrets.token_urlsafe(32)
    elif auth_mode == "session":
        env["SESSION_USERNAME"] = "sandbox"
        env["SESSION_PASSWORD"] = secrets.token_urlsafe(12)

    if kind == "chaos-api":
        env["CHAOS_ADMIN_TOKEN"] = CHAOS_ADMIN_TOKEN
    if kind == "mock-api":
        env["MOCK_ADMIN_TOKEN"] = MOCK_ADMIN_TOKEN
    if kind == "graphql-api":
        env["GRAPHQL_ADMIN_TOKEN"] = GRAPHQL_ADMIN_TOKEN

    return env


def _fetch_jwt_token(instance_id: str, kind: str) -> Optional[str]:
    try:
        resp = httpx.get(_instance_internal_url(instance_id, kind, "/_debug/token"), timeout=5)
        resp.raise_for_status()
        token = resp.json()["token"]
        _jwt_token_cache[instance_id] = token
        return token
    except httpx.HTTPError:
        return None


def create_instance(kind: str, name: Optional[str], config: dict) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    kdef = KINDS[kind]

    if not kdef.supports_auth:
        config["auth_mode"] = "none"
    auth_mode = config.get("auth_mode", "none")
    if auth_mode not in AUTH_MODES:
        raise ValueError(f"unknown auth_mode {auth_mode!r}")

    ensure_network()

    instance_id = f"{kind}-{secrets.token_hex(3)}"
    env = _build_env(kind, config)
    port = _free_port()

    if auth_mode == "oauth":
        oauth_host_port = _ensure_oauth_provider()
        env["OAUTH_INTROSPECT_URL"] = _internal_url(
            OAUTH_PROVIDER_NAME, OAUTH_PROVIDER_CONTAINER_PORT, "/introspect"
        )
        env["OAUTH_ISSUER_URL"] = f"http://localhost:{oauth_host_port}"
        env["PUBLIC_URL"] = f"http://localhost:{port}"

    client.containers.run(
        kdef.image,
        name=_container_name(instance_id),
        detach=True,
        network=NETWORK_NAME,
        ports={f"{kdef.container_port}/tcp": (BIND_HOST, port)},
        environment=env,
        labels={
            LABEL_MANAGED: "true",
            LABEL_ROLE: "instance",
            LABEL_KIND: kind,
            LABEL_NAME: name or instance_id,
            LABEL_CONFIG: json.dumps(config),
        },
        restart_policy={"Name": "unless-stopped"},
    )

    # Wait for the container's own app to actually accept connections before
    # returning it as "running" -- callers (the UI, or whoever the instance
    # URL gets handed to) shouldn't have to guess how long that takes.
    _wait_for_http(_instance_internal_url(instance_id, kind, "/health"))

    if auth_mode == "oauth":
        _register_oauth_client(instance_id)
    elif auth_mode == "jwt":
        _fetch_jwt_token(instance_id, kind)

    return instance_detail(instance_id)


def _describe_auth(auth_mode: str, instance_id: str, kind: str, env: dict, url: Optional[str]) -> dict:
    if auth_mode == "apikey":
        return {"mode": "apikey", "header": "X-API-Key", "api_key": env.get("API_KEY")}
    if auth_mode == "basic":
        return {"mode": "basic", "username": env.get("BASIC_USERNAME"), "password": env.get("BASIC_PASSWORD")}
    if auth_mode == "jwt":
        return {
            "mode": "jwt",
            "header": "Authorization: Bearer <token>",
            "token": _jwt_token_cache.get(instance_id),
            "debug_token_url": f"{url}/_debug/token" if url else None,
        }
    if auth_mode == "session":
        return {
            "mode": "session",
            "login_url": f"{url}/login" if url else None,
            "logout_url": f"{url}/logout" if url else None,
            "username": env.get("SESSION_USERNAME"),
            "password": env.get("SESSION_PASSWORD"),
        }
    if auth_mode == "oauth":
        creds = _oauth_client_cache.get(instance_id, {})
        provider = oauth_provider_status()
        info = {"mode": "oauth", "token_endpoint": f"{provider['url']}/token" if provider["url"] else None}
        info.update(creds)
        return info
    return {"mode": "none"}


def instance_detail(instance_id: str) -> Optional[dict]:
    container = _get_container(instance_id)
    if container is None:
        return None
    container.reload()

    kind = container.labels.get(LABEL_KIND, "unknown")
    name = container.labels.get(LABEL_NAME, instance_id)
    config = json.loads(container.labels.get(LABEL_CONFIG, "{}"))
    kdef = KINDS.get(kind)
    env = _container_env(container)

    port = _host_port(container, kdef.container_port) if kdef else None
    url = f"http://localhost:{port}" if port else None
    # "localhost" here means the *developer's* machine -- unreachable from
    # inside another sandbox-hub container (e.g. the API Tester), since
    # published ports are bound to 127.0.0.1 on purpose. Containers reach
    # each other over the shared Docker network by container name instead.
    internal_url = _internal_url(_container_name(instance_id), kdef.container_port, "") if kdef else None

    detail = {
        "id": instance_id,
        "kind": kind,
        "name": name,
        "state": container.status,
        "url": url,
        "internal_url": internal_url,
        "created_at": container.attrs.get("Created"),
        "auth": _describe_auth(config.get("auth_mode", "none"), instance_id, kind, env, url),
    }

    if kind == "rest-api":
        openapi = {
            "version": config.get("openapi_version", "3.1"),
            "spec_url": f"{url}/openapi.json" if url else None,
            "docs_url": f"{url}/docs" if url else None,
            "redoc_url": f"{url}/redoc" if url else None,
            "protected": bool(config.get("openapi_protect")),
        }
        if openapi["protected"]:
            openapi["auth"] = {"header": "X-API-Key", "token": env.get("OPENAPI_TOKEN")}
        detail["openapi"] = openapi

        if config.get("async_jobs"):
            detail["async_jobs"] = {
                "delay_seconds": config.get("async_job_delay_seconds", 5),
                "submit_url": f"{url}/jobs" if url else None,
                "poll_url_template": f"{url}/jobs/{{job_id}}" if url else None,
            }

    if kind == "chaos-api" and container.status == "running":
        try:
            resp = httpx.get(_instance_internal_url(instance_id, kind, "/_config"), timeout=3)
            detail["chaos"] = resp.json()
        except httpx.HTTPError:
            detail["chaos"] = None

    if kind == "mock-api" and container.status == "running":
        try:
            resp = httpx.get(_instance_internal_url(instance_id, kind, "/_routes"), timeout=3)
            detail["mock_routes"] = resp.json()
        except httpx.HTTPError:
            detail["mock_routes"] = []

    if kind == "graphql-api":
        detail["graphiql_url"] = f"{url}/graphiql" if url else None
        if container.status == "running":
            try:
                schema_resp = httpx.get(_instance_internal_url(instance_id, kind, "/_schema"), timeout=3)
                resolvers_resp = httpx.get(_instance_internal_url(instance_id, kind, "/_resolvers"), timeout=3)
                detail["graphql_sdl"] = schema_resp.json()["sdl"]
                detail["graphql_resolvers"] = resolvers_resp.json()
            except httpx.HTTPError:
                detail["graphql_sdl"] = None
                detail["graphql_resolvers"] = []

    return detail


def list_instances() -> list[dict]:
    # Docker ANDs multiple values given for the "label" filter key.
    containers = client.containers.list(
        all=True, filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_ROLE}=instance"]}
    )
    out = []
    for c in containers:
        instance_id = c.name.removeprefix(CONTAINER_PREFIX)
        detail = instance_detail(instance_id)
        if detail:
            out.append(detail)
    out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return out


def remove_instance(instance_id: str):
    container = _get_container(instance_id)
    if container is not None:
        container.stop(timeout=5)
        container.remove()
    _oauth_client_cache.pop(instance_id, None)
    _maybe_stop_oauth_provider()


# ------------------------------------------------- live-state snapshot/restore
#
# Mock API routes, the GraphQL schema/resolvers, and the Chaos API config are
# set live through each instance's admin API and live only in that
# container's memory -- they aren't in its env or labels. Anything that
# recreates the container (rotate, port change) has to carry them across
# explicitly or they're silently reset to the image defaults.


def _snapshot_live_state(container, instance_id: str, kind: str) -> Optional[dict]:
    """Read the live-configured state out of a running instance. Returns None
    for kinds with nothing to carry over, or if the container isn't running
    (its in-memory state is already gone). Raises if a running instance's
    state can't be read, so the caller fails before tearing anything down."""
    if container.status != "running":
        return None
    if kind == "mock-api":
        return {"routes": list_routes(instance_id)}
    if kind == "graphql-api":
        return {
            "sdl": get_graphql_schema(instance_id)["sdl"],
            "resolvers": list_graphql_resolvers(instance_id),
        }
    if kind == "chaos-api":
        resp = _request("GET", _instance_internal_url(instance_id, kind, "/_config"))
        return {"config": resp.json()}
    return None


def _restore_live_state(instance_id: str, kind: str, state: Optional[dict]):
    if not state:
        return
    if kind == "mock-api":
        clear_routes(instance_id)
        for route in state["routes"]:
            payload = {k: v for k, v in route.items() if k not in ("id", "items")}
            if route["type"] == "crud":
                # "seed" is only the route's original seed list; a crud
                # route's actual state is whatever's in its live collection
                # by now (items added/edited/deleted since creation), so
                # that's what has to survive the recreate, not the seed.
                payload["seed"] = route["items"]
            create_route(instance_id, payload)
    elif kind == "graphql-api":
        set_graphql_schema(instance_id, state["sdl"])
        # The fresh container seeds default resolvers that may not fit the
        # restored schema -- replace them wholesale rather than merging.
        _request(
            "DELETE",
            _instance_internal_url(instance_id, kind, "/_resolvers"),
            headers={"X-Admin-Token": GRAPHQL_ADMIN_TOKEN},
        )
        for resolver in state["resolvers"]:
            try:
                _request(
                    "POST",
                    _instance_internal_url(instance_id, kind, "/_resolvers"),
                    json=resolver,
                    headers={"X-Admin-Token": GRAPHQL_ADMIN_TOKEN},
                )
            except httpx.HTTPStatusError as exc:
                # Older graphql-api images kept resolvers for fields a
                # schema replacement removed; they're unreachable, and the
                # fresh container rightly rejects them (400), so drop them.
                if exc.response.status_code != 400:
                    raise
    elif kind == "chaos-api":
        configure_chaos(instance_id, state["config"])


# ------------------------------------------------------- scenario export/import
#
# A "scenario" is one instance's kind + creation config + live-configured
# state, or several such entries together -- everything needed to recreate
# the setup elsewhere, or after tearing this one down. Deliberately excludes
# generated secrets (API keys, passwords, JWT secrets, OAuth client
# credentials): those live in the container's env, not in `config`, and a
# scenario file is meant to be saved or shared, same spirit as the
# disposable/ephemeral credentials described in the README's security note.
# Importing mints fresh ones, exactly as a normal create does.

SCENARIO_FORMAT_VERSION = 1


def export_instance(instance_id: str) -> dict:
    container = _get_container(instance_id)
    if container is None:
        raise ValueError(f"instance {instance_id!r} not found")
    container.reload()
    kind = container.labels.get(LABEL_KIND)
    entry = {
        "kind": kind,
        "name": container.labels.get(LABEL_NAME, instance_id),
        "config": json.loads(container.labels.get(LABEL_CONFIG, "{}")),
    }
    live_state = _snapshot_live_state(container, instance_id, kind)
    if live_state is not None:
        entry["live_state"] = live_state
    return entry


def export_scenario(instance_ids: Optional[list[str]] = None) -> dict:
    if instance_ids is None:
        containers = client.containers.list(
            all=True, filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_ROLE}=instance"]}
        )
        instance_ids = [c.name.removeprefix(CONTAINER_PREFIX) for c in containers]
    return {
        "sandboxhub_scenario": SCENARIO_FORMAT_VERSION,
        "instances": [export_instance(i) for i in instance_ids],
    }


def import_scenario(scenario: dict) -> list[dict]:
    if scenario.get("sandboxhub_scenario") != SCENARIO_FORMAT_VERSION:
        raise ValueError(
            f"unrecognized scenario format (expected sandboxhub_scenario: {SCENARIO_FORMAT_VERSION})"
        )
    entries = scenario.get("instances")
    if not isinstance(entries, list) or not entries:
        raise ValueError("scenario has no instances to import")

    # Validate every entry before creating anything, so a mistake further
    # down the file doesn't leave a partial import behind to clean up.
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each entry in \"instances\" must be an object")
        kind = entry.get("kind")
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r} in scenario")
        config = entry.get("config") or {}
        if not isinstance(config, dict):
            raise ValueError(f"{kind!r} entry's config must be an object")
        auth_mode = config.get("auth_mode", "none")
        if KINDS[kind].supports_auth and auth_mode not in AUTH_MODES:
            raise ValueError(f"unknown auth_mode {auth_mode!r} in scenario")

    created = []
    for entry in entries:
        detail = create_instance(entry["kind"], entry.get("name"), dict(entry.get("config") or {}))
        _restore_live_state(detail["id"], entry["kind"], entry.get("live_state"))
        created.append(instance_detail(detail["id"]))
    return created


def rotate_instance(instance_id: str) -> dict:
    container = _get_container(instance_id)
    if container is None:
        raise ValueError("instance not found")
    container.reload()
    kind = container.labels.get(LABEL_KIND)
    name = container.labels.get(LABEL_NAME)
    config = json.loads(container.labels.get(LABEL_CONFIG, "{}"))
    auth_mode = config.get("auth_mode", "none")

    if auth_mode == "oauth":
        _register_oauth_client(instance_id)
        return instance_detail(instance_id)

    if auth_mode not in ("apikey", "basic", "jwt", "session") and not config.get("openapi_protect"):
        raise ValueError("instance has no rotatable credential")

    # apikey/basic/jwt/session secrets (and the openapi-protect token) are
    # baked into the container's env at creation time, so rotating means
    # recreating on the same port.
    port = _host_port(container, KINDS[kind].container_port)
    live_state = _snapshot_live_state(container, instance_id, kind)
    container.stop(timeout=5)
    container.remove()

    env = _build_env(kind, config)
    kdef = KINDS[kind]
    client.containers.run(
        kdef.image,
        name=_container_name(instance_id),
        detach=True,
        network=NETWORK_NAME,
        ports={f"{kdef.container_port}/tcp": (BIND_HOST, port)},
        environment=env,
        labels={
            LABEL_MANAGED: "true",
            LABEL_ROLE: "instance",
            LABEL_KIND: kind,
            LABEL_NAME: name,
            LABEL_CONFIG: json.dumps(config),
        },
        restart_policy={"Name": "unless-stopped"},
    )
    _wait_for_http(_instance_internal_url(instance_id, kind, "/health"))
    _restore_live_state(instance_id, kind, live_state)
    if auth_mode == "jwt":
        _fetch_jwt_token(instance_id, kind)
    return instance_detail(instance_id)


def update_port(instance_id: str, new_port: int) -> dict:
    container = _get_container(instance_id)
    if container is None:
        raise ValueError("instance not found")
    container.reload()
    kind = container.labels.get(LABEL_KIND)
    name = container.labels.get(LABEL_NAME)
    config = json.loads(container.labels.get(LABEL_CONFIG, "{}"))
    kdef = KINDS[kind]

    current_port = _host_port(container, kdef.container_port)
    if new_port == current_port:
        return instance_detail(instance_id)

    # Check before touching the running container -- if the requested port
    # is taken, fail without having torn anything down.
    if not _port_is_free(new_port):
        raise ValueError(f"port {new_port} is already in use")

    env = _container_env(container)
    if "PUBLIC_URL" in env:
        # Baked in at creation for the MCP OAuth resource-server metadata;
        # stale after a port change unless refreshed here.
        env["PUBLIC_URL"] = f"http://localhost:{new_port}"

    live_state = _snapshot_live_state(container, instance_id, kind)
    container.stop(timeout=5)
    container.remove()

    client.containers.run(
        kdef.image,
        name=_container_name(instance_id),
        detach=True,
        network=NETWORK_NAME,
        ports={f"{kdef.container_port}/tcp": (BIND_HOST, new_port)},
        environment=env,
        labels={
            LABEL_MANAGED: "true",
            LABEL_ROLE: "instance",
            LABEL_KIND: kind,
            LABEL_NAME: name,
            LABEL_CONFIG: json.dumps(config),
        },
        restart_policy={"Name": "unless-stopped"},
    )
    _wait_for_http(_instance_internal_url(instance_id, kind, "/health"))
    _restore_live_state(instance_id, kind, live_state)
    if config.get("auth_mode") == "jwt":
        _fetch_jwt_token(instance_id, kind)
    return instance_detail(instance_id)


def logs(instance_id: str, tail: int = 200) -> str:
    container = _get_container(instance_id)
    if container is None:
        return ""
    return container.logs(tail=tail).decode(errors="replace")


def stream_logs(instance_id: str):
    container = _get_container(instance_id)
    if container is None:
        return
    for chunk in container.logs(stream=True, follow=True, tail=50):
        yield chunk.decode(errors="replace")


# ------------------------------------------------------------- chaos-api config


def configure_chaos(instance_id: str, payload: dict) -> dict:
    resp = _request(
        "PUT",
        _instance_internal_url(instance_id, "chaos-api", "/_config"),
        json=payload,
        headers={"X-Admin-Token": CHAOS_ADMIN_TOKEN},
    )
    return resp.json()


# --------------------------------------------------------------- mock-api routes


def list_routes(instance_id: str) -> list[dict]:
    resp = _request("GET", _instance_internal_url(instance_id, "mock-api", "/_routes"))
    return resp.json()


def create_route(instance_id: str, payload: dict) -> dict:
    resp = _request(
        "POST",
        _instance_internal_url(instance_id, "mock-api", "/_routes"),
        json=payload,
        headers={"X-Admin-Token": MOCK_ADMIN_TOKEN},
    )
    return resp.json()


def delete_route(instance_id: str, route_id: str):
    _request(
        "DELETE",
        _instance_internal_url(instance_id, "mock-api", f"/_routes/{route_id}"),
        headers={"X-Admin-Token": MOCK_ADMIN_TOKEN},
    )


def clear_routes(instance_id: str):
    _request(
        "DELETE",
        _instance_internal_url(instance_id, "mock-api", "/_routes"),
        headers={"X-Admin-Token": MOCK_ADMIN_TOKEN},
    )


def import_openapi_routes(instance_id: str, payload: dict) -> dict:
    # Not routed through _request() -- that hardcodes a 2s read timeout
    # (fine for the other admin calls, all local and instant), too short
    # when the "url" form has the mock-api container fetching a spec over
    # the network on our behalf.
    resp = httpx.post(
        _instance_internal_url(instance_id, "mock-api", "/_routes/import-openapi"),
        json=payload,
        headers={"X-Admin-Token": MOCK_ADMIN_TOKEN},
        timeout=20,
    )
    if resp.status_code >= 400:
        raise ValueError(resp.json().get("detail", resp.text))
    return resp.json()


# ----------------------------------------------------------------- graphql-api


def get_graphql_schema(instance_id: str) -> dict:
    resp = _request("GET", _instance_internal_url(instance_id, "graphql-api", "/_schema"))
    return resp.json()


def set_graphql_schema(instance_id: str, sdl: str) -> dict:
    resp = httpx.request(
        "PUT",
        _instance_internal_url(instance_id, "graphql-api", "/_schema"),
        json={"sdl": sdl},
        headers={"X-Admin-Token": GRAPHQL_ADMIN_TOKEN},
        timeout=5,
    )
    if resp.status_code >= 400:
        raise ValueError(resp.json().get("detail", resp.text))
    return resp.json()


def list_graphql_resolvers(instance_id: str) -> list[dict]:
    resp = _request("GET", _instance_internal_url(instance_id, "graphql-api", "/_resolvers"))
    return resp.json()


def set_graphql_resolver(instance_id: str, payload: dict) -> dict:
    resp = httpx.request(
        "POST",
        _instance_internal_url(instance_id, "graphql-api", "/_resolvers"),
        json=payload,
        headers={"X-Admin-Token": GRAPHQL_ADMIN_TOKEN},
        timeout=5,
    )
    if resp.status_code >= 400:
        raise ValueError(resp.json().get("detail", resp.text))
    return resp.json()


def delete_graphql_resolver(instance_id: str, type_name: str, field_name: str):
    _request(
        "DELETE",
        _instance_internal_url(instance_id, "graphql-api", f"/_resolvers/{type_name}/{field_name}"),
        headers={"X-Admin-Token": GRAPHQL_ADMIN_TOKEN},
    )


# --------------------------------------------------------- webhook-receiver log


def list_webhook_requests(instance_id: str) -> list[dict]:
    resp = _request("GET", _instance_internal_url(instance_id, "webhook-receiver", "/_requests"))
    return resp.json()


def clear_webhook_requests(instance_id: str):
    _request("DELETE", _instance_internal_url(instance_id, "webhook-receiver", "/_requests"))
