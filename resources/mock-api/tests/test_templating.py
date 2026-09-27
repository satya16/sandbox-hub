"""
render_template / _resolve_token -- the {{...}} placeholder syntax
documented in app.py's module docstring, plus required_fields and the
"can't carry a body" status codes.
"""
import re

from fastapi.testclient import TestClient

ADMIN = {"X-Admin-Token": "dev-admin-token"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _add_route(client, **kwargs):
    body = {"method": "*", "path": "/t", "status_code": 200, "response_body": {"ok": True}, **kwargs}
    resp = client.post("/_routes", json=body, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_request_body_query_params_headers_method_path(app_module):
    client = TestClient(app_module.app)
    _add_route(
        client,
        path="/t/{id}",
        response_body={
            "body_val": "{{request.body.nested.x}}",
            "query_val": "{{request.query.q}}",
            "param_val": "{{request.params.id}}",
            # TOKEN_RE only matches [\w.]+ between the braces, so a header
            # name with a hyphen (e.g. "x-foo") could never appear in a
            # token -- use a hyphen-free name to test header lookup itself.
            "header_val": "{{request.headers.xfoo}}",
            "method": "{{request.method}}",
            "path": "{{request.path}}",
        },
    )
    resp = client.post(
        "/t/42?q=hello",
        json={"nested": {"x": "deep"}},
        headers={"Xfoo": "bar"},
    )
    assert resp.json() == {
        "body_val": "deep",
        "query_val": "hello",
        "param_val": "42",
        "header_val": "bar",
        "method": "POST",
        "path": "/t/42",
    }


def test_uuid_and_now_tokens(app_module):
    client = TestClient(app_module.app)
    _add_route(client, response_body={"a": "{{uuid}}", "b": "{{uuid}}", "when": "{{now}}"})
    data = client.get("/t").json()
    assert UUID_RE.match(data["a"])
    assert UUID_RE.match(data["b"])
    assert data["a"] != data["b"]
    assert ISO_RE.match(data["when"])


def test_exact_token_preserves_real_json_type(app_module):
    client = TestClient(app_module.app)
    _add_route(client, response_body={"count": "{{request.body.count}}"})
    resp = client.post("/t", json={"count": 5})
    assert resp.json() == {"count": 5}
    assert isinstance(resp.json()["count"], int)


def test_embedded_token_is_stringified(app_module):
    client = TestClient(app_module.app)
    _add_route(client, path="/t/{id}", response_body={"label": "id-{{request.params.id}}"})
    assert client.get("/t/7").json() == {"label": "id-7"}

    _add_route(client, path="/t2", response_body={"label": "val-{{request.body.obj}}"})
    resp = client.post("/t2", json={"obj": {"x": 1}})
    assert resp.json()["label"] == 'val-{"x": 1}'


def test_unresolvable_token_renders_empty_or_none(app_module):
    client = TestClient(app_module.app)
    _add_route(client, response_body={"whole": "{{request.body.missing}}", "embedded": "x-{{request.body.missing}}-y"})
    resp = client.post("/t", json={})
    assert resp.json() == {"whole": None, "embedded": "x--y"}


def test_templating_recurses_into_nested_structures(app_module):
    client = TestClient(app_module.app)
    _add_route(
        client,
        response_body={"list": [{"id": "{{request.params.id}}"}, "plain"]},
        path="/t/{id}",
    )
    assert client.get("/t/9").json() == {"list": [{"id": "9"}, "plain"]}


def test_required_fields_missing_returns_400_with_field_list(app_module):
    client = TestClient(app_module.app)
    _add_route(client, required_fields=["a", "b.c"])
    resp = client.post("/t", json={"a": 1})
    assert resp.status_code == 400
    assert set(resp.json()["fields"]) == {"b.c"}


def test_required_fields_present_returns_normal_response(app_module):
    client = TestClient(app_module.app)
    _add_route(client, required_fields=["a"], response_body={"ok": True})
    resp = client.post("/t", json={"a": 1})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_no_content_status_codes_have_empty_body(app_module):
    client = TestClient(app_module.app)
    _add_route(client, path="/nc", status_code=204, response_body={"should": "not appear"})
    resp = client.get("/nc")
    assert resp.status_code == 204
    assert resp.content == b""

    _add_route(client, path="/nm", status_code=304, response_body={"should": "not appear"})
    resp = client.get("/nm")
    assert resp.status_code == 304
    assert resp.content == b""


def test_failure_rate_injects_failure_when_forced(app_module, monkeypatch):
    client = TestClient(app_module.app)
    _add_route(client, path="/flaky", failure_rate=1.0, response_body={"ok": True})
    monkeypatch.setattr(app_module.random, "random", lambda: 0.0)
    monkeypatch.setattr(app_module.random, "choice", lambda seq: 503)
    resp = client.get("/flaky")
    assert resp.status_code == 503
    assert resp.json() == {"error": "injected_failure", "status": 503}


def test_failure_rate_zero_never_triggers(app_module, monkeypatch):
    client = TestClient(app_module.app)
    _add_route(client, path="/reliable", failure_rate=0.0, response_body={"ok": True})
    monkeypatch.setattr(app_module.random, "random", lambda: 0.0)
    resp = client.get("/reliable")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_latency_ms_sleeps_the_right_duration(app_module, monkeypatch):
    client = TestClient(app_module.app)
    _add_route(client, path="/slow", latency_ms=250, response_body={"ok": True})

    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)
    resp = client.get("/slow")
    assert resp.status_code == 200
    assert calls == [0.25]
