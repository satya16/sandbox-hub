"""
Unit tests for the webhook-receiver resource (resources/webhook-receiver/app.py)
-- in-process against a fresh module per test via the make_app/app_module
fixtures in conftest.py, not a running container.
"""
import hashlib
import hmac

from fastapi.testclient import TestClient


def _client(mod):
    return TestClient(mod.app)


def _hmac_signature(secret: str, ts: int, body: bytes = b"") -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient in the oauth introspection call."""

    def __init__(self, active=True, raise_error=False, **kwargs):
        self._active = active
        self._raise_error = raise_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None):
        if self._raise_error:
            import httpx

            raise httpx.ConnectError("boom")

        class _Resp:
            def __init__(self, active):
                self._active = active

            def json(self):
                return {"active": self._active}

        return _Resp(self._active)


def test_health_reflects_auth_mode_and_count(app_module):
    c = _client(app_module)
    assert c.get("/health").json() == {"status": "ok", "auth_mode": "none", "captured": 0}


def test_catch_all_records_json_body(app_module):
    c = _client(app_module)
    resp = c.post("/hooks/stripe?foo=bar", json={"event": "created"})
    assert resp.status_code == 200
    assert resp.json() == {"received": True, "id": 1}

    entry = c.get("/_requests/last").json()
    assert entry["method"] == "POST"
    assert entry["path"] == "/hooks/stripe"
    assert entry["query"] == {"foo": "bar"}
    assert entry["body_json"] == {"event": "created"}
    assert entry["body_text"] is None


def test_catch_all_records_non_json_body_as_text(app_module):
    c = _client(app_module)
    c.post("/x", content=b"not json at all", headers={"content-type": "text/plain"})
    entry = c.get("/_requests/last").json()
    assert entry["body_json"] is None
    assert entry["body_text"] == "not json at all"


def test_catch_all_strips_auth_and_cookie_headers(app_module):
    c = _client(app_module)
    c.get("/x", headers={"Authorization": "Bearer secret", "Cookie": "a=b", "X-Keep-Me": "yes"})
    entry = c.get("/_requests/last").json()
    lowered = {k.lower() for k in entry["headers"]}
    assert "authorization" not in lowered
    assert "cookie" not in lowered
    assert "x-keep-me" in entry["headers"]


def test_requests_list_and_clear(app_module):
    c = _client(app_module)
    assert c.get("/_requests/last").status_code == 404

    c.get("/a")
    c.get("/b")
    listed = c.get("/_requests").json()
    assert [r["path"] for r in listed] == ["/a", "/b"]  # most-recent-last, insertion order

    assert c.delete("/_requests").json() == {"ok": True}
    assert c.get("/_requests").json() == []
    assert c.get("/_requests/last").status_code == 404


def test_ring_buffer_evicts_oldest(app_module):
    c = _client(app_module)
    for i in range(201):
        c.get(f"/n/{i}")
    all_requests = c.get("/_requests").json()
    assert len(all_requests) == 200
    paths = {r["path"] for r in all_requests}
    assert "/n/0" not in paths
    assert "/n/200" in paths


def test_admin_endpoints_reachable_regardless_of_auth_mode(make_app):
    mod = make_app(AUTH_MODE="apikey", API_KEY="secret")
    c = _client(mod)
    assert c.get("/_requests").status_code == 200
    assert c.get("/_requests/last").status_code == 404
    assert c.delete("/_requests").status_code == 200
    assert c.get("/health").status_code == 200
    # login/logout are gated to AUTH_MODE=="session" internally, but reachable
    # (not blocked by require_auth) under any AUTH_MODE.
    assert c.post("/login", json={"username": "x", "password": "y"}).status_code == 404
    assert c.post("/logout").status_code == 404


def test_apikey_auth(make_app):
    mod = make_app(AUTH_MODE="apikey", API_KEY="secret123")
    c = _client(mod)
    assert c.get("/x").status_code == 401
    assert c.get("/x", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/x", headers={"X-API-Key": "secret123"}).status_code == 200


def test_basic_auth(make_app):
    import base64

    mod = make_app(AUTH_MODE="basic", BASIC_USERNAME="u", BASIC_PASSWORD="p")
    c = _client(mod)
    assert c.get("/x").status_code == 401
    creds = base64.b64encode(b"u:p").decode()
    wrong = base64.b64encode(b"u:wrong").decode()
    assert c.get("/x", headers={"Authorization": f"Basic {wrong}"}).status_code == 401
    assert c.get("/x", headers={"Authorization": f"Basic {creds}"}).status_code == 200


def test_jwt_auth(make_app):
    mod = make_app(AUTH_MODE="jwt", JWT_SECRET="shh")
    c = _client(mod)
    assert c.get("/x").status_code == 401
    assert c.get("/x", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401

    token = c.get("/_debug/token").json()["token"]
    assert c.get("/x", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_session_auth_login_and_logout(make_app):
    mod = make_app(AUTH_MODE="session", SESSION_USERNAME="sandbox", SESSION_PASSWORD="pw")
    c = _client(mod)
    assert c.get("/x").status_code == 401

    assert c.post("/login", json={"username": "sandbox", "password": "wrong"}).status_code == 401
    assert c.post("/login", json={"username": "sandbox", "password": "pw"}).status_code == 200
    assert c.get("/x").status_code == 200

    assert c.post("/logout").status_code == 200
    assert c.get("/x").status_code == 401


def test_hmac_auth(make_app):
    import time

    mod = make_app(AUTH_MODE="hmac", HMAC_SECRET="topsecret")
    c = _client(mod)
    now = int(time.time())

    assert c.get("/x").status_code == 401
    assert c.get("/x", headers={"X-Signature": "not-even-the-right-shape"}).status_code == 401
    assert c.get("/x", headers={"X-Signature": _hmac_signature("wrong-secret", now)}).status_code == 401
    assert c.get("/x", headers={"X-Signature": _hmac_signature("topsecret", now - 3600)}).status_code == 401
    assert c.get("/x", headers={"X-Signature": _hmac_signature("topsecret", now)}).status_code == 200


def test_oauth_auth(make_app, monkeypatch):
    mod = make_app(AUTH_MODE="oauth", OAUTH_INTROSPECT_URL="http://provider/introspect")
    c = _client(mod)

    assert c.get("/x").status_code == 401  # no bearer token at all

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(active=False))
    assert c.get("/x", headers={"Authorization": "Bearer whatever"}).status_code == 401

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(active=True))
    assert c.get("/x", headers={"Authorization": "Bearer whatever"}).status_code == 200

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(raise_error=True))
    assert c.get("/x", headers={"Authorization": "Bearer whatever"}).status_code == 502
