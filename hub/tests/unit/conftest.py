"""
Unit tests in this directory exercise hub/app's own code in-process, with
Docker and every instance's admin HTTP API replaced by fakes -- unlike
hub/tests/test_api.py (see its module docstring), which deliberately drives
a real container because some behavior (Docker-DNS-name admin calls) can
only be observed that way. These tests cover the logic docker_manager.py,
main.py and mcp_server.py apply *around* those calls: validation, error
mapping, env/label construction, state bookkeeping -- none of which needs a
real daemon to verify.

docker_manager.py does `client = docker.from_env()` and talks to instance
admin APIs via the `httpx` module directly, both at call time, not via
dependency injection -- so faking them out means monkeypatching those two
names on the already-imported module (`fake_docker`, `fake_httpx` below)
rather than passing fakes in as arguments.
"""
import sys
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parents[2]
if str(HUB_DIR) not in sys.path:
    sys.path.insert(0, str(HUB_DIR))

import httpx as real_httpx
import pytest
from docker.errors import NotFound

from app import docker_manager as dm


class FakeResponse:
    """Just enough of httpx.Response for docker_manager.py's own usage:
    .status_code, .json(), .text, .content, .raise_for_status()."""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.text = text or ""
        self.content = self.text.encode() if self.text else (b"{}" if json_data is not None else b"")

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            request = real_httpx.Request("GET", "http://fake.invalid")
            raise real_httpx.HTTPStatusError(
                f"status {self.status_code}", request=request, response=self
            )


class FakeHttpx:
    """Stands in for the whole `httpx` module as docker_manager.py uses it:
    .get/.post/.request. A response can be queued per exact (method, url) or
    per url (any method); anything not queued gets a plain 200 {}. Queuing
    an exception instance instead of a FakeResponse makes that call raise
    it -- e.g. httpx.ConnectError to exercise a retry/degrade path."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.default_response = FakeResponse(200, {})

    def _resolve(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if (method, url) in self.responses:
            resp = self.responses[(method, url)]
        elif url in self.responses:
            resp = self.responses[url]
        else:
            resp = self.default_response
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def get(self, url, **kwargs):
        return self._resolve("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._resolve("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._resolve(method, url, **kwargs)


class FakeContainer:
    def __init__(
        self,
        name,
        image=None,
        environment=None,
        labels=None,
        container_port=8000,
        host_port=None,
        status="running",
        created="2024-01-01T00:00:00.000000000Z",
        networks=None,
        store=None,
    ):
        self.name = name
        self.image = image
        self._env = dict(environment or {})
        self.labels = dict(labels or {})
        self.status = status
        self.container_port = container_port
        self.host_port = host_port
        self.created = created
        self.networks = dict(networks or {})
        self.stopped = False
        self.removed = False
        self._store = store

    def reload(self):
        pass

    @property
    def attrs(self):
        ports = {}
        if self.host_port is not None:
            ports[f"{self.container_port}/tcp"] = [{"HostPort": str(self.host_port)}]
        return {
            "Config": {"Env": [f"{k}={v}" for k, v in self._env.items()]},
            "NetworkSettings": {"Ports": ports, "Networks": self.networks},
            "Created": self.created,
        }

    def start(self):
        self.status = "running"

    def stop(self, timeout=5):
        self.stopped = True
        self.status = "exited"

    def remove(self, force=False):
        self.removed = True
        if self._store is not None:
            self._store.pop(self.name, None)


class FakeContainers:
    def __init__(self, store):
        self._store = store
        self.run_calls = []

    def get(self, name):
        try:
            return self._store[name]
        except KeyError:
            raise NotFound(f"no such container: {name}")

    def list(self, all=True, filters=None):
        label_filters = (filters or {}).get("label", [])

        def matches(c):
            for lf in label_filters:
                k, _, v = lf.partition("=")
                if c.labels.get(k) != v:
                    return False
            return True

        return [c for c in self._store.values() if matches(c)]

    def run(self, image, name=None, detach=True, network=None, ports=None, environment=None, labels=None, restart_policy=None):
        self.run_calls.append(
            {"image": image, "name": name, "ports": ports, "environment": environment, "labels": labels}
        )
        container_port, host_port = 8000, None
        if ports:
            container_port = int(next(iter(ports)).split("/")[0])
            binding = next(iter(ports.values()))
            host_port = binding[1] if isinstance(binding, tuple) else binding
        c = FakeContainer(
            name=name, image=image, environment=environment, labels=labels,
            container_port=container_port, host_port=host_port, store=self._store,
        )
        self._store[name] = c
        return c


class FakeNetwork:
    def __init__(self, name):
        self.name = name
        self.connected = []

    def connect(self, container):
        self.connected.append(container)


class FakeNetworks:
    def __init__(self):
        self._store = {}

    def get(self, name):
        try:
            return self._store[name]
        except KeyError:
            raise NotFound(f"network {name} not found")

    def create(self, name, driver=None):
        net = FakeNetwork(name)
        self._store[name] = net
        return net


class FakeDockerClient:
    def __init__(self):
        self._container_store = {}
        self.containers = FakeContainers(self._container_store)
        self.networks = FakeNetworks()


@pytest.fixture
def fake_docker(monkeypatch):
    fake = FakeDockerClient()
    monkeypatch.setattr(dm, "client", fake)
    return fake


@pytest.fixture
def fake_httpx(monkeypatch):
    fake = FakeHttpx()
    monkeypatch.setattr(dm.httpx, "get", fake.get)
    monkeypatch.setattr(dm.httpx, "post", fake.post)
    monkeypatch.setattr(dm.httpx, "request", fake.request)
    return fake


@pytest.fixture(autouse=True)
def _isolated_module_caches():
    """The two bits of real state docker_manager.py keeps in memory --
    reset around every test so none of them leak into the next one."""
    dm._oauth_client_cache.clear()
    dm._jwt_token_cache.clear()
    yield
    dm._oauth_client_cache.clear()
    dm._jwt_token_cache.clear()


def register_container(fake_docker, instance_id, kind, config=None, env=None, host_port=12345, status="running", name=None, created="2024-01-01T00:00:00.000000000Z"):
    """Drop a fake container for `instance_id` straight into the fake
    Docker client's store, shaped the way docker_manager.py's own
    create_instance() would have left it -- for tests that want to start
    from an existing instance rather than creating one through the full
    create_instance() flow."""
    import json as _json

    container_name = dm._container_name(instance_id)
    labels = {
        dm.LABEL_MANAGED: "true",
        dm.LABEL_ROLE: "instance",
        dm.LABEL_KIND: kind,
        dm.LABEL_NAME: name or instance_id,
        dm.LABEL_CONFIG: _json.dumps(config or {}),
    }
    c = FakeContainer(
        name=container_name, environment=env or {}, labels=labels,
        container_port=8000, host_port=host_port, status=status,
        store=fake_docker._container_store, created=created,
    )
    fake_docker._container_store[container_name] = c
    return c
