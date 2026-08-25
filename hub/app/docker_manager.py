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

from .catalog import KINDS, OAUTH_PROVIDER_CONTAINER_PORT, OAUTH_PROVIDER_IMAGE

NETWORK_NAME = os.environ.get("SANDBOXHUB_NETWORK", "sandboxhub-net")
BIND_HOST = os.environ.get("SANDBOXHUB_BIND_HOST", "127.0.0.1")
CONTAINER_PREFIX = "sandboxhub-"
OAUTH_PROVIDER_NAME = f"{CONTAINER_PREFIX}oauth-provider"
ADMIN_TOKEN = os.environ.get("OAUTH_ADMIN_TOKEN", "dev-admin-token")

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((BIND_HOST, 0))
        return s.getsockname()[1]


def ensure_network():
    try:
        client.networks.get(NETWORK_NAME)
    except NotFound:
        client.networks.create(NETWORK_NAME, driver="bridge")


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

    if auth_mode == "apikey":
        env["API_KEY"] = secrets.token_urlsafe(18)

    return env


def create_instance(kind: str, name: Optional[str], config: dict) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    kdef = KINDS[kind]
    auth_mode = config.get("auth_mode", "none")
    if auth_mode not in ("none", "apikey", "oauth"):
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

    if auth_mode == "oauth":
        _register_oauth_client(instance_id)

    return instance_detail(instance_id)


def _describe_auth(auth_mode: str, instance_id: str, env: dict) -> dict:
    if auth_mode == "apikey":
        return {"mode": "apikey", "header": "X-API-Key", "api_key": env.get("API_KEY")}
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

    detail = {
        "id": instance_id,
        "kind": kind,
        "name": name,
        "state": container.status,
        "url": url,
        "created_at": container.attrs.get("Created"),
        "auth": _describe_auth(config.get("auth_mode", "none"), instance_id, env),
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

    if auth_mode != "apikey" and not config.get("openapi_protect"):
        raise ValueError("instance has no rotatable credential")

    # apikey / openapi-token secrets are baked into the container's env at
    # creation time, so rotating means recreating on the same port.
    port = _host_port(container, KINDS[kind].container_port)
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
