"""
Integration tests against a real hub container (see conftest.py for why).
Not exhaustive -- covers the load-bearing paths from recent work: instance
lifecycle, Mock API's static/path-param/CRUD routes and OpenAPI import,
GraphQL schema/resolvers, Chaos config, scenario export/import (including
the no-secrets guarantee), and that live-configured state survives a
port-change recreate.
"""
import hashlib
import hmac
import socket
import time

import httpx


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_list_kinds(hub):
    kinds = hub.get("/kinds").json()["kinds"]
    ids = {k["id"] for k in kinds}
    assert {"rest-api", "mock-api", "graphql-api", "chaos-api"} <= ids


def test_create_rest_api_with_apikey(hub):
    inst = hub.post("/instances", json={"kind": "rest-api", "auth_mode": "apikey"}).json()
    hub.track(inst["id"])
    assert inst["state"] == "running"

    key = inst["auth"]["api_key"]
    assert httpx.get(f"{inst['url']}/items", headers={"X-API-Key": key}).status_code == 200
    assert httpx.get(f"{inst['url']}/items").status_code == 401


def _hmac_signature(secret: str, ts: int, body: bytes = b"") -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def test_hmac_auth(hub):
    inst = hub.post("/instances", json={"kind": "rest-api", "auth_mode": "hmac"}).json()
    hub.track(inst["id"])
    secret = inst["auth"]["secret"]
    url = f"{inst['url']}/items"

    now = int(time.time())
    assert httpx.get(url, headers={"X-Signature": _hmac_signature(secret, now)}).status_code == 200

    assert httpx.get(url).status_code == 401
    assert httpx.get(url, headers={"X-Signature": "not-even-the-right-shape"}).status_code == 401
    assert httpx.get(url, headers={"X-Signature": _hmac_signature("wrong-secret", now)}).status_code == 401
    assert httpx.get(url, headers={"X-Signature": _hmac_signature(secret, now - 3600)}).status_code == 401


def test_mock_api_static_path_param_and_crud(hub):
    inst = hub.post("/instances", json={"kind": "mock-api", "auth_mode": "none"}).json()
    hub.track(inst["id"])

    hub.post(
        f"/instances/{inst['id']}/routes",
        json={"method": "GET", "path": "/users/{id}", "response_body": {"id": "{{request.params.id}}"}},
    )
    assert httpx.get(f"{inst['url']}/users/42").json() == {"id": "42"}

    crud = hub.post(
        f"/instances/{inst['id']}/routes",
        json={"type": "crud", "path": "/items", "seed": [{"id": 1, "name": "a"}]},
    ).json()
    assert crud["items"] == [{"id": 1, "name": "a"}]

    created = httpx.post(f"{inst['url']}/items", json={"name": "b"}).json()
    assert created == {"id": 2, "name": "b"}
    assert httpx.get(f"{inst['url']}/items").json() == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_mock_api_openapi_import(hub):
    inst = hub.post("/instances", json={"kind": "mock-api", "auth_mode": "none"}).json()
    hub.track(inst["id"])

    spec = {
        "openapi": "3.0.3",
        "paths": {
            "/things": {
                "get": {
                    "responses": {
                        "200": {"description": "ok", "content": {"application/json": {"example": [{"id": 1}]}}}
                    }
                }
            }
        },
    }
    result = hub.post(f"/instances/{inst['id']}/routes/import-openapi", json={"spec": spec}).json()
    assert len(result["created"]) == 1
    assert httpx.get(f"{inst['url']}/things").json() == [{"id": 1}]


def test_graphql_schema_and_resolver(hub):
    inst = hub.post("/instances", json={"kind": "graphql-api", "auth_mode": "none"}).json()
    hub.track(inst["id"])

    hub.put(f"/instances/{inst['id']}/graphql/schema", json={"sdl": "type Query { hi(name: String): String }\n"})
    hub.post(
        f"/instances/{inst['id']}/graphql/resolvers",
        json={"type": "Query", "field": "hi", "response_body": "hello {{request.args.name}}"},
    )
    r = httpx.post(f"{inst['url']}/graphql", json={"query": '{ hi(name: "x") }'}).json()
    assert r == {"data": {"hi": "hello x"}}


def test_chaos_config(hub):
    inst = hub.post("/instances", json={"kind": "chaos-api", "auth_mode": "none"}).json()
    hub.track(inst["id"])

    hub.put(
        f"/instances/{inst['id']}/chaos-config",
        json={"mode": "chaos", "chaos": {"status_code": 418, "body": {"t": 1}, "latency_ms": 0, "failure_rate": 0.0}},
    )
    r = httpx.get(f"{inst['url']}/test")
    assert r.status_code == 418
    assert r.json() == {"t": 1}


def test_scenario_export_import_roundtrip(hub):
    inst = hub.post("/instances", json={"kind": "mock-api", "auth_mode": "none", "name": "scn"}).json()
    hub.track(inst["id"])
    hub.post(
        f"/instances/{inst['id']}/routes",
        json={"method": "GET", "path": "/ping", "response_body": {"ok": True}},
    )

    scenario = hub.get(f"/instances/{inst['id']}/scenario").json()
    assert "API_KEY" not in str(scenario) and "secret" not in str(scenario).lower()

    hub.delete(f"/instances/{inst['id']}")
    imported = hub.post("/scenario/import", json=scenario).json()
    new_inst = imported[0]
    hub.track(new_inst["id"])
    assert httpx.get(f"{new_inst['url']}/ping").json() == {"ok": True}


def test_session_logout_across_kinds(hub):
    """Regression test: mock-api, graphql-api, and webhook-receiver each
    used to have no working /logout (mock-api's was reserved-but-unhandled,
    graphql-api's didn't exist, webhook-receiver's catch-all silently
    swallowed it without invalidating the session) -- login always worked,
    logout never did. rest-api is covered implicitly since it's the one
    kind that already had this right."""
    cases = [
        ("mock-api", lambda s: s.get("/t")),
        ("graphql-api", lambda s: s.post("/graphql", json={"query": "{ items { id } }"})),
        ("webhook-receiver", lambda s: s.get("/x")),
    ]
    for kind, probe in cases:
        inst = hub.post("/instances", json={"kind": kind, "auth_mode": "session"}).json()
        hub.track(inst["id"])
        if kind == "mock-api":
            hub.post(
                f"/instances/{inst['id']}/routes",
                json={"method": "GET", "path": "/t", "response_body": {"ok": True}},
            )
        user, pw = inst["auth"]["username"], inst["auth"]["password"]

        s = httpx.Client(base_url=inst["url"])
        assert s.post("/login", json={"username": user, "password": pw}).status_code == 200
        assert probe(s).status_code != 401, f"{kind}: should be authenticated after login"
        assert s.post("/logout").status_code == 200, f"{kind}: /logout should succeed"
        assert probe(s).status_code == 401, f"{kind}: should be gated again after logout"


def test_rest_api_openapi_protect_gates_docs_and_redoc(hub):
    """Regression test: OPENAPI_PROTECT's own docstring promises it gates
    /openapi.json, /docs, AND /redoc, but /docs and /redoc used to have no
    auth dependency wired in at all and were always open."""
    inst = hub.post(
        "/instances", json={"kind": "rest-api", "auth_mode": "none", "openapi_protect": True}
    ).json()
    hub.track(inst["id"])
    url = inst["url"]
    token = inst["openapi"]["auth"]["token"]

    assert httpx.get(f"{url}/docs").status_code == 401
    assert httpx.get(f"{url}/redoc").status_code == 401
    assert httpx.get(f"{url}/docs", headers={"X-API-Key": token}).status_code == 200
    assert httpx.get(f"{url}/redoc", headers={"X-API-Key": token}).status_code == 200


def test_state_survives_port_change(hub):
    inst = hub.post("/instances", json={"kind": "mock-api", "auth_mode": "none"}).json()
    hub.track(inst["id"])
    hub.post(
        f"/instances/{inst['id']}/routes",
        json={"method": "GET", "path": "/ping", "response_body": {"ok": True}},
    )

    updated = hub.put(f"/instances/{inst['id']}/port", json={"port": _free_port()}).json()
    assert httpx.get(f"{updated['url']}/ping").json() == {"ok": True}
