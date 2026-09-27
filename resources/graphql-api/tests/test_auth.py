"""Every AUTH_MODE app.py supports, exercised against the one route it
actually gates (POST /graphql) -- plus proof that /_schema, /_resolvers,
/health and /graphiql stay reachable with no auth header at all regardless
of AUTH_MODE, since require_auth is never applied to them."""
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

ADMIN_HEADERS = {"X-Admin-Token": "dev-admin-token"}
ITEMS_QUERY = {"query": "{ items { id name } }"}


def _assert_admin_and_misc_routes_need_no_auth(client):
    assert client.get("/health").status_code == 200
    assert client.get("/graphiql").status_code == 200
    assert client.get("/_schema").status_code == 200
    assert client.get("/_resolvers").status_code == 200
    assert client.post(
        "/_resolvers",
        json={"type": "Query", "field": "items", "response_body": []},
        headers=ADMIN_HEADERS,
    ).status_code == 200


def test_auth_none(make_app):
    mod = make_app(AUTH_MODE="none")
    client = TestClient(mod.app)
    resp = client.post("/graphql", json=ITEMS_QUERY)
    assert resp.status_code == 200
    assert "errors" not in resp.json()
    _assert_admin_and_misc_routes_need_no_auth(client)


def test_auth_apikey(make_app):
    mod = make_app(AUTH_MODE="apikey", API_KEY="secret123")
    client = TestClient(mod.app)
    assert client.post("/graphql", json=ITEMS_QUERY).status_code == 401
    assert client.post("/graphql", json=ITEMS_QUERY, headers={"X-API-Key": "wrong"}).status_code == 401

    resp = client.post("/graphql", json=ITEMS_QUERY, headers={"X-API-Key": "secret123"})
    assert resp.status_code == 200
    assert "errors" not in resp.json()

    _assert_admin_and_misc_routes_need_no_auth(client)


def test_auth_basic(make_app):
    mod = make_app(AUTH_MODE="basic", BASIC_USERNAME="sandbox", BASIC_PASSWORD="pw")
    client = TestClient(mod.app)

    resp = client.post("/graphql", json=ITEMS_QUERY)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Basic"

    assert client.post(
        "/graphql", json=ITEMS_QUERY, headers={"Authorization": "Basic not-valid-base64!!"}
    ).status_code == 401
    assert client.post("/graphql", json=ITEMS_QUERY, auth=("sandbox", "wrong")).status_code == 401

    resp = client.post("/graphql", json=ITEMS_QUERY, auth=("sandbox", "pw"))
    assert resp.status_code == 200
    assert "errors" not in resp.json()

    _assert_admin_and_misc_routes_need_no_auth(client)


def test_auth_jwt(make_app):
    mod = make_app(AUTH_MODE="jwt", JWT_SECRET="jwtsecret")
    client = TestClient(mod.app)

    assert client.post("/graphql", json=ITEMS_QUERY).status_code == 401

    token = client.get("/_debug/token").json()["token"]
    resp = client.post("/graphql", json=ITEMS_QUERY, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "errors" not in resp.json()

    expired = mod.pyjwt.encode({"sub": "x", "iat": 0, "exp": 1}, "jwtsecret", algorithm="HS256")
    resp = client.post("/graphql", json=ITEMS_QUERY, headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token expired"

    resp = client.post("/graphql", json=ITEMS_QUERY, headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401
    assert "invalid token" in resp.json()["detail"]

    _assert_admin_and_misc_routes_need_no_auth(client)


def test_debug_token_404_unless_jwt_mode(make_app):
    mod = make_app(AUTH_MODE="none")
    assert TestClient(mod.app).get("/_debug/token").status_code == 404


def test_auth_session(make_app):
    mod = make_app(AUTH_MODE="session", SESSION_USERNAME="sandbox", SESSION_PASSWORD="pw")
    client = TestClient(mod.app)

    assert client.post("/graphql", json=ITEMS_QUERY).status_code == 401
    assert client.post("/login", json={"username": "sandbox", "password": "wrong"}).status_code == 401

    login_resp = client.post("/login", json={"username": "sandbox", "password": "pw"})
    assert login_resp.status_code == 200
    session_id = login_resp.cookies.get("sandboxhub_session")
    assert session_id

    resp = client.post("/graphql", json=ITEMS_QUERY)
    assert resp.status_code == 200
    assert "errors" not in resp.json()

    _assert_admin_and_misc_routes_need_no_auth(client)

    logout_resp = client.post("/logout")
    assert logout_resp.status_code == 200
    assert client.post("/graphql", json=ITEMS_QUERY).status_code == 401


def test_login_404_unless_session_mode(make_app):
    mod = make_app(AUTH_MODE="none")
    assert TestClient(mod.app).post("/login", json={}).status_code == 404


def test_logout_404_unless_session_mode(make_app):
    mod = make_app(AUTH_MODE="none")
    assert TestClient(mod.app).post("/logout").status_code == 404


class _FakeOauthResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, data=None):
        if self._exc is not None:
            raise self._exc
        return self._response


def test_auth_oauth(make_app, monkeypatch):
    mod = make_app(AUTH_MODE="oauth", OAUTH_INTROSPECT_URL="http://oauth-provider/introspect")
    client = TestClient(mod.app)

    assert client.post("/graphql", json=ITEMS_QUERY).status_code == 401

    monkeypatch.setattr(
        mod.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(response=_FakeOauthResponse({"active": True}))
    )
    resp = client.post("/graphql", json=ITEMS_QUERY, headers={"Authorization": "Bearer at_x"})
    assert resp.status_code == 200
    assert "errors" not in resp.json()

    monkeypatch.setattr(
        mod.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(response=_FakeOauthResponse({"active": False}))
    )
    assert client.post(
        "/graphql", json=ITEMS_QUERY, headers={"Authorization": "Bearer at_x"}
    ).status_code == 401

    monkeypatch.setattr(
        mod.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(exc=mod.httpx.HTTPError("boom"))
    )
    resp = client.post("/graphql", json=ITEMS_QUERY, headers={"Authorization": "Bearer at_x"})
    assert resp.status_code == 502

    _assert_admin_and_misc_routes_need_no_auth(client)


def _hmac_sig(secret: str, ts: int, body: bytes = b"") -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def test_auth_hmac(make_app):
    mod = make_app(AUTH_MODE="hmac", HMAC_SECRET="hmacsecret")
    client = TestClient(mod.app)

    assert client.post("/graphql", json=ITEMS_QUERY).status_code == 401
    assert client.post(
        "/graphql", json=ITEMS_QUERY, headers={"X-Signature": "not-even-the-right-shape"}
    ).status_code == 401

    now = int(time.time())
    body = json.dumps(ITEMS_QUERY).encode()

    assert client.post(
        "/graphql", content=body, headers={"X-Signature": _hmac_sig("wrong-secret", now), "Content-Type": "application/json"}
    ).status_code == 401
    assert client.post(
        "/graphql",
        content=body,
        headers={"X-Signature": _hmac_sig("hmacsecret", now - 3600), "Content-Type": "application/json"},
    ).status_code == 401

    resp = client.post(
        "/graphql",
        content=body,
        headers={"X-Signature": _hmac_sig("hmacsecret", now, body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert "errors" not in resp.json()

    _assert_admin_and_misc_routes_need_no_auth(client)
