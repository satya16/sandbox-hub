"""
Integration tests against a real, throwaway hub container -- not FastAPI's
in-process TestClient. docker_manager.py resolves every instance's admin
API by Docker DNS name (Mock API routes, GraphQL schema/resolvers, Chaos
config, even the plain health check after any create), which only
resolves from inside a container that's actually on sandboxhub-net. An
in-process TestClient call runs in the test process itself, not a
container, so it would hit exactly the DNS-resolution wall the network
self-join fix (docker_manager.ensure_network) exists to solve. These
tests exercise the real thing instead: build the image, run it, talk to
it over HTTP, same as a person following the README's own quickstart.

A session-scoped fixture builds the image once and starts one throwaway
container on a random port, never touching a container actually named
"sandboxhub-hub" (or any other real one). Every test that creates an
instance must register it with the `hub` fixture's `.track()` so it gets
removed in teardown even if the test fails -- there's no broad
"sweep everything" cleanup here, specifically so a real hub's own
instances, on the same shared sandboxhub-net, are never at risk.
"""
import secrets
import time
from pathlib import Path

import docker
import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_TAG = "sandboxhub-hub:pytest"


def _wait_for_ok(url: str, timeout: float = 30.0):
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=2)
            if resp.status_code < 500:
                return
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.5)
    raise RuntimeError(f"{url} never came up: {last_exc}")


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="session")
def hub_image(docker_client):
    image, _logs = docker_client.images.build(
        path=str(REPO_ROOT), dockerfile="hub/Dockerfile", tag=IMAGE_TAG, rm=True
    )
    return image.tags[0]


@pytest.fixture(scope="session")
def hub_url(docker_client, hub_image):
    name = f"sandboxhub-hub-pytest-{secrets.token_hex(3)}"
    container = docker_client.containers.run(
        hub_image,
        name=name,
        detach=True,
        ports={"8090/tcp": None},  # let Docker pick a free host port
        volumes={"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}},
    )
    try:
        container.reload()
        port = container.attrs["NetworkSettings"]["Ports"]["8090/tcp"][0]["HostPort"]
        base_url = f"http://127.0.0.1:{port}/api"
        _wait_for_ok(f"{base_url}/health")
        yield base_url
    finally:
        container.stop(timeout=5)
        container.remove()


@pytest.fixture
def hub(hub_url):
    """An httpx.Client bound to the throwaway hub. Call .track(instance_id)
    for every instance a test creates -- it gets deleted in teardown even
    if the test fails, so a crash can't leak a container."""
    created = []
    client = httpx.Client(base_url=hub_url, timeout=30)
    client.track = created.append
    try:
        yield client
    finally:
        errors = []
        for instance_id in created:
            try:
                resp = client.delete(f"/instances/{instance_id}")
                if resp.status_code not in (200, 404):
                    errors.append(f"{instance_id}: {resp.status_code} {resp.text}")
            except httpx.HTTPError as exc:
                errors.append(f"{instance_id}: {exc!r}")
        client.close()
        if errors:
            raise RuntimeError("failed to clean up instance(s): " + "; ".join(errors))
