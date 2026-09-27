"""
Route registration validation and dispatch specificity -- see app.py's
module docstring for the exact rules being tested here: a concrete path
beats "*", more literal segments beat fewer, a specific method beats "*",
and among equally specific routes the first added wins.
"""
from fastapi.testclient import TestClient

ADMIN = {"X-Admin-Token": "dev-admin-token"}


def _add_route(client, **kwargs):
    body = {"method": "*", "path": "*", "status_code": 200, "response_body": {"ok": True}, **kwargs}
    resp = client.post("/_routes", json=body, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_concrete_path_beats_wildcard_star(app_module):
    client = TestClient(app_module.app)
    _add_route(client, path="*", response_body={"which": "wildcard"})
    _add_route(client, path="/ping", response_body={"which": "concrete"})
    assert client.get("/ping").json() == {"which": "concrete"}
    assert client.get("/other").json() == {"which": "wildcard"}


def test_more_literal_segments_beat_param_segment(app_module):
    client = TestClient(app_module.app)
    _add_route(client, path="/users/{id}", response_body={"which": "param"})
    _add_route(client, path="/users/me", response_body={"which": "literal"})
    assert client.get("/users/me").json() == {"which": "literal"}
    assert client.get("/users/42").json() == {"which": "param"}


def test_specific_method_beats_wildcard_method(app_module):
    client = TestClient(app_module.app)
    _add_route(client, method="*", path="/thing", response_body={"which": "any-method"})
    _add_route(client, method="GET", path="/thing", response_body={"which": "get-only"})
    assert client.get("/thing").json() == {"which": "get-only"}
    assert client.post("/thing").json() == {"which": "any-method"}


def test_equal_specificity_first_added_wins(app_module):
    client = TestClient(app_module.app)
    _add_route(client, path="/tied", response_body={"which": "first"})
    _add_route(client, path="/tied", response_body={"which": "second"})
    assert client.get("/tied").json() == {"which": "first"}


def test_segment_count_must_match(app_module):
    client = TestClient(app_module.app)
    _add_route(client, path="/a/{b}/c", response_body={"ok": True})
    assert client.get("/a/b").status_code == 404
    assert client.get("/a/b/c").status_code == 200
    assert client.get("/a/b/c/d").status_code == 404


def test_no_matching_route_is_404(app_module):
    client = TestClient(app_module.app)
    resp = client.get("/nothing-registered")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "no matching mock route"
    assert body["method"] == "GET"
    assert body["path"] == "/nothing-registered"


def test_reserved_paths_never_dispatch_even_with_catch_all(app_module):
    client = TestClient(app_module.app)
    _add_route(client, path="*", response_body={"caught": True})
    for path in ("/_routes", "/health", "/login", "/logout", "/_debug/token"):
        resp = client.get(path)
        assert resp.status_code != 200 or resp.json() != {"caught": True}


def test_cannot_register_route_at_reserved_path(app_module):
    client = TestClient(app_module.app)
    resp = client.post("/_routes", json={"path": "/health"}, headers=ADMIN)
    assert resp.status_code == 400
    resp = client.post("/_routes", json={"path": "/_routes/whatever"}, headers=ADMIN)
    assert resp.status_code == 400


def test_malformed_path_parameter_segment_rejected(app_module):
    client = TestClient(app_module.app)
    resp = client.post("/_routes", json={"path": "/users/{id}extra"}, headers=ADMIN)
    assert resp.status_code == 400
    resp = client.post("/_routes", json={"path": "/users/{"}, headers=ADMIN)
    assert resp.status_code == 400


def test_duplicate_path_parameter_name_rejected(app_module):
    client = TestClient(app_module.app)
    resp = client.post("/_routes", json={"path": "/a/{id}/b/{id}"}, headers=ADMIN)
    assert resp.status_code == 400


def test_unsupported_method_rejected_and_method_upper_cased(app_module):
    client = TestClient(app_module.app)
    resp = client.post("/_routes", json={"path": "/x", "method": "FROB"}, headers=ADMIN)
    assert resp.status_code == 400

    created = _add_route(client, path="/lower", method="get", response_body={"ok": True})
    assert created["method"] == "GET"
    assert client.get("/lower").status_code == 200
