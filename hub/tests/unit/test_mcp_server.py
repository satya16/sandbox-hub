"""
Unit tests for mcp_server.py's tool functions, called directly as plain
Python functions -- not over the MCP wire protocol, which is out of scope
here (see mcp_server.py's own module docstring: it's a second front door
onto the same docker_manager calls the REST API uses, already exercised
end-to-end by hub/tests/test_api.py). Every dm.* call here is monkeypatched.
"""
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app import mcp_server as ms


def _raise(exc):
    def _f(*_a, **_k):
        raise exc

    return _f


# --------------------------------------------------------------- _tool decorator


def test_tool_decorator_reclassifies_value_error_as_tool_error():
    @ms._tool
    def f():
        raise ValueError("nope, no such instance")

    with pytest.raises(ToolError, match="nope, no such instance"):
        f()


def test_tool_decorator_passes_through_existing_tool_error_unchanged():
    @ms._tool
    def f():
        raise ToolError("already the right type")

    with pytest.raises(ToolError, match="already the right type"):
        f()


def test_tool_decorator_returns_value_on_success():
    @ms._tool
    def f():
        return {"ok": True}

    assert f() == {"ok": True}


# --------------------------------------------------------------- _require_instance/_require_kind


def test_require_instance_raises_when_missing(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: None)
    with pytest.raises(ValueError, match="no instance"):
        ms._require_instance("nope")


def test_require_instance_returns_detail_when_present(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    assert ms._require_instance("abc") == {"id": "abc", "kind": "mock-api"}


def test_require_kind_raises_on_mismatch(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    with pytest.raises(ValueError, match="mock-api"):
        ms._require_kind("abc", "graphql-api")


def test_require_kind_returns_detail_on_match(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    assert ms._require_kind("abc", "mock-api")["kind"] == "mock-api"


# --------------------------------------------------------------- create_instance


def test_create_instance_unknown_kind_raises_before_calling_dm(monkeypatch):
    calls = []
    monkeypatch.setattr(ms.dm, "create_instance", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ToolError, match="unknown kind"):
        ms.create_instance("not-a-kind")
    assert calls == []


def test_create_instance_builds_config_for_kind_with_openapi_and_async_job(monkeypatch):
    captured = {}

    def fake_create(kind, name, config):
        captured.update(kind=kind, name=name, config=config)
        return {"id": "x"}

    monkeypatch.setattr(ms.dm, "create_instance", fake_create)
    ms.create_instance(
        "rest-api", name="my-rest", auth_mode="apikey",
        openapi_version="3.0", openapi_protect=True,
        async_jobs=True, async_job_delay_seconds=15,
    )
    assert captured["kind"] == "rest-api"
    assert captured["name"] == "my-rest"
    assert captured["config"] == {
        "auth_mode": "apikey", "openapi_version": "3.0", "openapi_protect": True,
        "async_jobs": True, "async_job_delay_seconds": 15,
    }


def test_create_instance_omits_unsupported_config_keys(monkeypatch):
    captured = {}
    monkeypatch.setattr(ms.dm, "create_instance", lambda kind, name, config: captured.update(config=config) or {"id": "x"})
    ms.create_instance("mock-api", auth_mode="none")
    assert captured["config"] == {"auth_mode": "none"}


# --------------------------------------------------------------- delete/rotate/update_port


def test_delete_instance_checks_existence_first(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: None)
    with pytest.raises(ToolError):
        ms.delete_instance("nope")


def test_rotate_instance_credentials_delegates_to_dm(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    monkeypatch.setattr(ms.dm, "rotate_instance", lambda i: {"id": i, "rotated": True})
    assert ms.rotate_instance_credentials("abc") == {"id": "abc", "rotated": True}


def test_rotate_instance_credentials_reclassifies_value_error(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    monkeypatch.setattr(ms.dm, "rotate_instance", _raise(ValueError("instance has no rotatable credential")))
    with pytest.raises(ToolError, match="no rotatable credential"):
        ms.rotate_instance_credentials("abc")


def test_update_instance_port_rejects_out_of_range(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    with pytest.raises(ToolError, match="between 1 and 65535"):
        ms.update_instance_port("abc", 70000)


def test_update_instance_port_delegates_to_dm(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    monkeypatch.setattr(ms.dm, "update_port", lambda i, port: {"id": i, "port": port})
    assert ms.update_instance_port("abc", 9000) == {"id": "abc", "port": 9000}


# --------------------------------------------------------------- mock-api tools


def test_add_mock_route_wrong_kind(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "rest-api"})
    with pytest.raises(ToolError):
        ms.add_mock_route("abc", path="/x")


def test_add_mock_route_builds_full_payload_with_defaults(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "create_route", lambda i, payload: captured.update(payload) or payload)

    ms.add_mock_route("abc", path="/users/{id}")

    assert captured == {
        "type": "static", "method": "*", "path": "/users/{id}", "status_code": 200,
        "response_body": {"ok": True}, "required_fields": [], "latency_ms": 0,
        "failure_rate": 0.0, "seed": [], "id_field": "id",
    }


def test_add_mock_route_builds_payload_with_explicit_values(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "create_route", lambda i, payload: captured.update(payload) or payload)

    ms.add_mock_route(
        "abc", path="/items", method="POST", type="crud", status_code=201,
        response_body={"x": 1}, required_fields=["name"], latency_ms=50,
        failure_rate=0.1, seed=[{"id": 1}], id_field="uuid",
    )

    assert captured == {
        "type": "crud", "method": "POST", "path": "/items", "status_code": 201,
        "response_body": {"x": 1}, "required_fields": ["name"], "latency_ms": 50,
        "failure_rate": 0.1, "seed": [{"id": 1}], "id_field": "uuid",
    }


def test_import_openapi_to_mock_requires_exactly_one_of_spec_or_url(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    with pytest.raises(ToolError, match="exactly one of"):
        ms.import_openapi_to_mock("abc")
    with pytest.raises(ToolError, match="exactly one of"):
        ms.import_openapi_to_mock("abc", spec={"openapi": "3.0.0"}, url="http://example.com/spec.json")


def test_import_openapi_to_mock_with_spec_builds_payload(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "import_openapi_routes", lambda i, payload: captured.update(payload) or {})

    ms.import_openapi_to_mock("abc", spec={"openapi": "3.0.0"}, replace=True)

    assert captured == {"replace": True, "spec": {"openapi": "3.0.0"}}
    assert "url" not in captured


def test_import_openapi_to_mock_with_url_builds_payload(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "import_openapi_routes", lambda i, payload: captured.update(payload) or {})

    ms.import_openapi_to_mock("abc", url="http://example.com/spec.json")

    assert captured == {"replace": False, "url": "http://example.com/spec.json"}
    assert "spec" not in captured


# --------------------------------------------------------------- graphql-api tools


def test_set_graphql_schema_wrong_kind(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    with pytest.raises(ToolError):
        ms.set_graphql_schema("abc", "type Query { x: String }")


def test_set_graphql_resolver_builds_payload_without_error(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "graphql-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "set_graphql_resolver", lambda i, payload: captured.update(payload) or payload)

    ms.set_graphql_resolver("abc", type="Query", field="items", response_body=[{"id": "1"}])

    assert captured == {"type": "Query", "field": "items", "response_body": [{"id": "1"}]}
    assert "error" not in captured


def test_set_graphql_resolver_builds_payload_with_error(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "graphql-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "set_graphql_resolver", lambda i, payload: captured.update(payload) or payload)

    ms.set_graphql_resolver(
        "abc", type="Mutation", field="addItem",
        error_message="boom", error_extensions={"code": "BOOM"},
    )

    assert captured == {
        "type": "Mutation", "field": "addItem", "response_body": None,
        "error": {"message": "boom", "extensions": {"code": "BOOM"}},
    }


# --------------------------------------------------------------- chaos-api tool


def test_set_chaos_config_wrong_kind(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "mock-api"})
    with pytest.raises(ToolError):
        ms.set_chaos_config("abc", mode="normal")


def test_set_chaos_config_mode_only(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "chaos-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "configure_chaos", lambda i, payload: captured.update(payload) or payload)

    ms.set_chaos_config("abc", mode="normal")

    assert captured == {"mode": "normal"}


def test_set_chaos_config_rate_limit_fills_defaults_for_omitted_fields(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "chaos-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "configure_chaos", lambda i, payload: captured.update(payload) or payload)

    ms.set_chaos_config("abc", mode="rate_limit", rate_limit_requests=10)

    assert captured == {"mode": "rate_limit", "rate_limit": {"limit": 10, "window_seconds": 10}}


def test_set_chaos_config_chaos_fills_defaults_for_omitted_fields(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "chaos-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "configure_chaos", lambda i, payload: captured.update(payload) or payload)

    ms.set_chaos_config("abc", mode="chaos", chaos_status_code=503)

    assert captured == {
        "mode": "chaos",
        "chaos": {"status_code": 503, "body": {"ok": True}, "latency_ms": 0, "failure_rate": 0.0},
    }


def test_set_chaos_config_no_rate_limit_or_chaos_keys_when_nothing_given(monkeypatch):
    monkeypatch.setattr(ms.dm, "instance_detail", lambda i: {"id": i, "kind": "chaos-api"})
    captured = {}
    monkeypatch.setattr(ms.dm, "configure_chaos", lambda i, payload: captured.update(payload) or payload)

    ms.set_chaos_config("abc", mode="normal")

    assert "rate_limit" not in captured
    assert "chaos" not in captured


# --------------------------------------------------------------- scenario tools


def test_export_scenario_delegates_to_dm(monkeypatch):
    monkeypatch.setattr(ms.dm, "export_scenario", lambda ids: {"sandboxhub_scenario": 1, "instances": []})
    assert ms.export_scenario() == {"sandboxhub_scenario": 1, "instances": []}


def test_import_scenario_reclassifies_value_error(monkeypatch):
    monkeypatch.setattr(ms.dm, "import_scenario", _raise(ValueError("scenario has no instances to import")))
    with pytest.raises(ToolError, match="no instances to import"):
        ms.import_scenario({"instances": []})
