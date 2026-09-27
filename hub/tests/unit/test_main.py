"""
Unit tests for main.py's own routing/validation/error-mapping logic --
every dm.* call is monkeypatched out, so this is independent of
docker_manager's correctness (see test_docker_manager.py for that).
"""
import pytest
from fastapi.testclient import TestClient

from app import main


def _raise(exc):
    def _f(*_a, **_k):
        raise exc

    return _f


@pytest.fixture(scope="module")
def client():
    # Module-scoped: mcp_app (spliced into main.app, see main.py's own
    # comment on this) wraps a StreamableHTTPSessionManager that can only be
    # .run() once per process -- entering main.app's lifespan (which starts
    # it) more than once raises. A fresh TestClient per test would do that;
    # one shared client for the whole module does not. Individual tests
    # still get their own function-scoped `monkeypatch` for dm.* calls.
    original_ensure_network = main.dm.ensure_network
    main.dm.ensure_network = lambda: None
    with TestClient(main.app) as c:
        yield c
    main.dm.ensure_network = original_ensure_network


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_list_kinds_shape(client):
    body = client.get("/api/kinds").json()
    assert "kinds" in body and "auth_modes" in body and "openapi_versions" in body
    assert {k["id"] for k in body["kinds"]} >= {"rest-api", "mock-api", "graphql-api", "chaos-api"}


# --------------------------------------------------------------- create_instance


def test_create_instance_unknown_kind(client):
    resp = client.post("/api/instances", json={"kind": "not-a-kind"})
    assert resp.status_code == 404


def test_create_instance_unknown_auth_mode(client):
    resp = client.post("/api/instances", json={"kind": "rest-api", "auth_mode": "not-a-mode"})
    assert resp.status_code == 400


def test_create_instance_value_error_maps_to_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "create_instance", _raise(ValueError("bad config")))
    resp = client.post("/api/instances", json={"kind": "rest-api"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad config"


def test_create_instance_generic_exception_maps_to_500(client, monkeypatch):
    monkeypatch.setattr(main.dm, "create_instance", _raise(RuntimeError("boom")))
    resp = client.post("/api/instances", json={"kind": "rest-api"})
    assert resp.status_code == 500


def test_create_instance_passes_openapi_and_async_job_config_through(client, monkeypatch):
    captured = {}

    def fake_create(kind, name, config):
        captured["kind"], captured["name"], captured["config"] = kind, name, config
        return {"id": "x"}

    monkeypatch.setattr(main.dm, "create_instance", fake_create)
    client.post(
        "/api/instances",
        json={
            "kind": "rest-api", "name": "my-rest", "auth_mode": "none",
            "openapi_version": "3.0", "openapi_protect": True,
            "async_jobs": True, "async_job_delay_seconds": 12,
        },
    )
    assert captured["kind"] == "rest-api"
    assert captured["name"] == "my-rest"
    assert captured["config"] == {
        "auth_mode": "none", "openapi_version": "3.0", "openapi_protect": True,
        "async_jobs": True, "async_job_delay_seconds": 12,
    }


def test_create_instance_omits_openapi_and_async_job_config_for_unsupported_kind(client, monkeypatch):
    captured = {}

    def fake_create(kind, name, config):
        captured["config"] = config
        return {"id": "x"}

    monkeypatch.setattr(main.dm, "create_instance", fake_create)
    client.post("/api/instances", json={"kind": "mock-api", "auth_mode": "none"})
    assert captured["config"] == {"auth_mode": "none"}


# --------------------------------------------------------------- get/delete instance


def test_get_instance_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    assert client.get("/api/instances/nope").status_code == 404


def test_get_instance_found(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i})
    resp = client.get("/api/instances/abc")
    assert resp.status_code == 200
    assert resp.json() == {"id": "abc"}


def test_delete_instance_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    assert client.delete("/api/instances/nope").status_code == 404


def test_delete_instance_calls_remove(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i})
    calls = []
    monkeypatch.setattr(main.dm, "remove_instance", lambda i: calls.append(i))
    resp = client.delete("/api/instances/abc")
    assert resp.status_code == 200
    assert calls == ["abc"]


# --------------------------------------------------------------- rotate


def test_rotate_instance_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    assert client.post("/api/instances/nope/rotate").status_code == 404


def test_rotate_instance_value_error_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i})
    monkeypatch.setattr(main.dm, "rotate_instance", _raise(ValueError("no rotatable credential")))
    resp = client.post("/api/instances/abc/rotate")
    assert resp.status_code == 400


# --------------------------------------------------------------- update_port


def test_update_port_out_of_range_never_calls_dm(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i})
    called = []
    monkeypatch.setattr(main.dm, "update_port", lambda *a, **k: called.append(1))
    resp = client.put("/api/instances/x/port", json={"port": 70000})
    assert resp.status_code == 400
    assert called == []


def test_update_port_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    resp = client.put("/api/instances/x/port", json={"port": 9000})
    assert resp.status_code == 404


def test_update_port_value_error_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i})
    monkeypatch.setattr(main.dm, "update_port", _raise(ValueError("port 9000 is already in use")))
    resp = client.put("/api/instances/x/port", json={"port": 9000})
    assert resp.status_code == 400


def test_update_port_generic_exception_500(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i})
    monkeypatch.setattr(main.dm, "update_port", _raise(RuntimeError("boom")))
    resp = client.put("/api/instances/x/port", json={"port": 9000})
    assert resp.status_code == 500


# --------------------------------------------------------------- logs


def test_get_logs_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    assert client.get("/api/instances/x/logs").status_code == 404


def test_get_logs_returns_dm_output(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i})
    monkeypatch.setattr(main.dm, "logs", lambda i, tail=200: f"log for {i} tail={tail}")
    resp = client.get("/api/instances/x/logs?tail=50")
    assert resp.json() == {"logs": "log for x tail=50"}


# --------------------------------------------------------------- chaos-config


def test_chaos_config_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    resp = client.put("/api/instances/x/chaos-config", json={"mode": "chaos"})
    assert resp.status_code == 404


def test_chaos_config_wrong_kind_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    resp = client.put("/api/instances/x/chaos-config", json={"mode": "chaos"})
    assert resp.status_code == 400


def test_chaos_config_value_error_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "chaos-api"})
    monkeypatch.setattr(main.dm, "configure_chaos", _raise(ValueError("bad mode")))
    resp = client.put("/api/instances/x/chaos-config", json={"mode": "bogus"})
    assert resp.status_code == 400


def test_chaos_config_generic_exception_502(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "chaos-api"})
    monkeypatch.setattr(main.dm, "configure_chaos", _raise(RuntimeError("admin api down")))
    resp = client.put("/api/instances/x/chaos-config", json={"mode": "chaos"})
    assert resp.status_code == 502


# --------------------------------------------------------------- mock-api routes


def test_get_routes_wrong_kind_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    assert client.get("/api/instances/x/routes").status_code == 400


def test_add_route_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    assert client.post("/api/instances/x/routes", json={}).status_code == 404


def test_add_route_wrong_kind_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    assert client.post("/api/instances/x/routes", json={}).status_code == 400


def test_add_route_value_error_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    monkeypatch.setattr(main.dm, "create_route", _raise(ValueError("duplicate path parameter name")))
    resp = client.post("/api/instances/x/routes", json={})
    assert resp.status_code == 400


def test_add_route_generic_exception_502(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    monkeypatch.setattr(main.dm, "create_route", _raise(RuntimeError("admin api down")))
    resp = client.post("/api/instances/x/routes", json={})
    assert resp.status_code == 502


def test_remove_route_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    assert client.delete("/api/instances/x/routes/r1").status_code == 404


def test_import_openapi_requires_exactly_one_of_spec_or_url(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    assert client.post("/api/instances/x/routes/import-openapi", json={}).status_code == 400
    assert (
        client.post(
            "/api/instances/x/routes/import-openapi",
            json={"spec": {"openapi": "3.0.0"}, "url": "http://example.com/spec.json"},
        ).status_code
        == 400
    )


def test_import_openapi_wrong_kind_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    resp = client.post("/api/instances/x/routes/import-openapi", json={"spec": {"openapi": "3.0.0"}})
    assert resp.status_code == 400


# --------------------------------------------------------------- graphql-api


def test_graphql_schema_get_wrong_kind_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    assert client.get("/api/instances/x/graphql/schema").status_code == 400


def test_graphql_schema_put_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    resp = client.put("/api/instances/x/graphql/schema", json={"sdl": "type Query { a: String }"})
    assert resp.status_code == 404


def test_graphql_schema_put_invalid_sdl_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "graphql-api"})
    monkeypatch.setattr(main.dm, "set_graphql_schema", _raise(Exception("invalid SDL: syntax error")))
    resp = client.put("/api/instances/x/graphql/schema", json={"sdl": "not valid sdl"})
    assert resp.status_code == 400


def test_graphql_resolver_post_wrong_kind_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    resp = client.post("/api/instances/x/graphql/resolvers", json={"type": "Query", "field": "x"})
    assert resp.status_code == 400


# --------------------------------------------------------------- webhook-receiver


def test_webhook_requests_wrong_kind_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    assert client.get("/api/instances/x/webhook-requests").status_code == 400


def test_webhook_requests_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    assert client.get("/api/instances/x/webhook-requests").status_code == 404


# --------------------------------------------------------------- scenario export/import


def test_export_instance_scenario_404(client, monkeypatch):
    monkeypatch.setattr(main.dm, "instance_detail", lambda i: None)
    assert client.get("/api/instances/x/scenario").status_code == 404


def test_import_scenario_value_error_400(client, monkeypatch):
    monkeypatch.setattr(main.dm, "import_scenario", _raise(ValueError("scenario has no instances to import")))
    resp = client.post("/api/scenario/import", json={"instances": []})
    assert resp.status_code == 400


def test_import_scenario_generic_exception_500(client, monkeypatch):
    monkeypatch.setattr(main.dm, "import_scenario", _raise(RuntimeError("boom")))
    resp = client.post("/api/scenario/import", json={"instances": [{"kind": "rest-api"}]})
    assert resp.status_code == 500


def test_import_scenario_success(client, monkeypatch):
    monkeypatch.setattr(main.dm, "import_scenario", lambda payload: [{"id": "new-1"}])
    resp = client.post("/api/scenario/import", json={"sandboxhub_scenario": 1, "instances": [{"kind": "rest-api"}]})
    assert resp.status_code == 200
    assert resp.json() == [{"id": "new-1"}]


# --------------------------------------------------------------- oauth-provider status


def test_oauth_provider_status(client, monkeypatch):
    monkeypatch.setattr(main.dm, "oauth_provider_status", lambda: {"state": "stopped", "url": None})
    resp = client.get("/api/oauth-provider")
    assert resp.json() == {"state": "stopped", "url": None}
