"""
All Docker Engine interaction lives here, via the Docker SDK against
/var/run/docker.sock. The hub creates/starts/stops sibling containers on a
dedicated bridge network and talks to them by container name.
"""
import os
import secrets
from typing import Optional

import docker
import httpx
from docker.errors import NotFound

from .catalog import CATALOG, ResourceDef

NETWORK_NAME = os.environ.get("SANDBOXHUB_NETWORK", "sandboxhub-net")
BIND_HOST = os.environ.get("SANDBOXHUB_BIND_HOST", "127.0.0.1")
CONTAINER_PREFIX = "sandboxhub-"
MANAGED_LABEL = "sandboxhub.managed"
RESOURCE_LABEL = "sandboxhub.resource"
ADMIN_TOKEN = os.environ.get("OAUTH_ADMIN_TOKEN", "dev-admin-token")

client = docker.from_env()

# In-memory cache of OAuth client credentials the hub has registered with the
# local oauth-provider, keyed by resource id. Ephemeral by design -- both the
# provider's client store and this cache reset when their containers restart.
_oauth_client_cache: dict[str, dict] = {}


def container_name(resource_id: str) -> str:
    return f"{CONTAINER_PREFIX}{resource_id}"


def ensure_network():
    try:
        client.networks.get(NETWORK_NAME)
    except NotFound:
        client.networks.create(NETWORK_NAME, driver="bridge")


def _get_container(resource_id: str):
    try:
        return client.containers.get(container_name(resource_id))
    except NotFound:
        return None


def _container_env(container) -> dict[str, str]:
    env_list = container.attrs.get("Config", {}).get("Env", []) or []
    out = {}
    for entry in env_list:
        if "=" in entry:
            k, v = entry.split("=", 1)
            out[k] = v
    return out


def status(resource_id: str) -> dict:
    rdef = CATALOG[resource_id]
    container = _get_container(resource_id)
    if container is None:
        return {"id": resource_id, "state": "stopped", "auth": None, "url": None}

    container.reload()
    state = container.status  # "running", "exited", "created", ...
    env = _container_env(container)
    url = f"http://localhost:{rdef.host_port}"
    auth = _describe_auth(rdef, env)
    return {"id": resource_id, "state": state, "auth": auth, "url": url}


def _describe_auth(rdef: ResourceDef, env: dict) -> Optional[dict]:
    if rdef.auth_mode == "none":
        return {"mode": "none"}
    if rdef.auth_mode == "apikey":
        return {"mode": "apikey", "header": "X-API-Key", "api_key": env.get("API_KEY")}
    if rdef.auth_mode == "oauth":
        creds = _oauth_client_cache.get(rdef.id)
        oauth_def = CATALOG["oauth-provider"]
        info = {
            "mode": "oauth",
            "token_endpoint": f"http://localhost:{oauth_def.host_port}/token",
        }
        if creds:
            info.update(creds)
        return info
    return None


def _internal_url(resource_id: str, path: str) -> str:
    """URL to reach another sandbox-hub-managed container over the shared
    Docker network, from inside the hub's own container."""
    rdef = CATALOG[resource_id]
    return f"http://{container_name(resource_id)}:{rdef.container_port}{path}"


def _register_oauth_client(resource_id: str) -> dict:
    """Register (or re-register) an OAuth client for this resource with the
    local oauth-provider and cache the resulting credentials."""
    url = _internal_url("oauth-provider", "/admin/clients")
    resp = httpx.post(
        url,
        json={"name": f"sandbox-hub: {resource_id}"},
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=5,
    )
    resp.raise_for_status()
    creds = resp.json()
    _oauth_client_cache[resource_id] = creds
    return creds


def start_resource(resource_id: str) -> dict:
    rdef = CATALOG[resource_id]
    ensure_network()

    for dep_id in rdef.requires:
        start_resource(dep_id)

    existing = _get_container(resource_id)
    if existing is not None:
        existing.reload()
        if existing.status != "running":
            existing.start()
        if rdef.auth_mode == "oauth" and resource_id not in _oauth_client_cache:
            _register_oauth_client(resource_id)
        return status(resource_id)

    env = {}
    if rdef.auth_mode == "apikey":
        env["AUTH_MODE"] = "apikey"
        env["API_KEY"] = secrets.token_urlsafe(18)
    elif rdef.auth_mode == "oauth":
        env["AUTH_MODE"] = "oauth"
        oauth_def = CATALOG["oauth-provider"]
        env["OAUTH_INTROSPECT_URL"] = f"http://{container_name('oauth-provider')}:{oauth_def.container_port}/introspect"
        env["OAUTH_ISSUER_URL"] = f"http://localhost:{oauth_def.host_port}"
        env["PUBLIC_URL"] = f"http://localhost:{rdef.host_port}"
    elif rdef.auth_mode == "none":
        env["AUTH_MODE"] = "none"

    client.containers.run(
        rdef.image,
        name=container_name(resource_id),
        detach=True,
        network=NETWORK_NAME,
        ports={f"{rdef.container_port}/tcp": (BIND_HOST, rdef.host_port)},
        environment=env,
        labels={MANAGED_LABEL: "true", RESOURCE_LABEL: resource_id},
        restart_policy={"Name": "unless-stopped"},
    )

    if rdef.auth_mode == "oauth":
        _wait_for_http(_internal_url("oauth-provider", "/health"))
        _register_oauth_client(resource_id)

    return status(resource_id)


def _wait_for_http(url: str, timeout: float = 10.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)


def stop_resource(resource_id: str) -> dict:
    container = _get_container(resource_id)
    if container is not None:
        container.stop(timeout=5)
    return status(resource_id)


def remove_resource(resource_id: str) -> dict:
    container = _get_container(resource_id)
    if container is not None:
        container.stop(timeout=5)
        container.remove()
    _oauth_client_cache.pop(resource_id, None)
    return status(resource_id)


def rotate_api_key(resource_id: str) -> dict:
    rdef = CATALOG[resource_id]
    if rdef.auth_mode != "apikey":
        raise ValueError("resource is not in apikey auth mode")
    remove_resource(resource_id)
    return start_resource(resource_id)


def rotate_oauth_client(resource_id: str) -> dict:
    rdef = CATALOG[resource_id]
    if rdef.auth_mode != "oauth":
        raise ValueError("resource is not in oauth auth mode")
    _register_oauth_client(resource_id)
    return status(resource_id)


def logs(resource_id: str, tail: int = 200) -> str:
    container = _get_container(resource_id)
    if container is None:
        return ""
    return container.logs(tail=tail).decode(errors="replace")


def stream_logs(resource_id: str):
    container = _get_container(resource_id)
    if container is None:
        return
    for chunk in container.logs(stream=True, follow=True, tail=50):
        yield chunk.decode(errors="replace")


def list_all_status() -> list[dict]:
    return [status(rid) for rid in CATALOG]
