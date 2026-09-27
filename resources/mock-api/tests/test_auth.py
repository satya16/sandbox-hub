"""
The full AUTH_MODE matrix gating mock_dispatch (not /_routes*, which uses
the separate X-Admin-Token scheme). This is a literal copy of the same
require_auth code that lives in rest-api/graphql-api/webhook-receiver, so it
gets its own full pass here too in case the copies diverge.
"""
import hashlib
import hmac
import time

from fastapi.testclient import TestClient

ADMIN = {"X-Admin-Token": "dev-admin-token"}


def _add_open_route(client):
    resp = client.post("/_routes", json={"path": "/t", "response_body": {"ok": True}}, headers=ADMIN)
    assert resp.status_code == 200, resp.text


def test_auth_none_is_open(make_app):
    mod = make_app(AUTH_MODE="none")
    client = TestClient(mod.app)
    _add_open_route(client)
    assert client.get("/t").status_code == 200


def test_auth_apikey(make_app):
    mod = make_app(AUTH_MODE="apikey", API_KEY="secret123")
    client = TestClient(mod.app)
    _add_open_route(client)
    assert client.get("/t").status_code == 401
    assert client.get("/t", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/t", headers={"X-API-Key": "secret123"}).status_code == 200


def test_auth_basic(make_app):
    mod = make_app(AUTH_MODE="basic", BASIC_USERNAME="user", BASIC_PASSWORD="pass")
    client = TestClient(mod.app)
    _add_open_route(client)
    assert client.get("/t").status_code == 401
    assert client.get("/t", auth=("user", "wrong")).status_code == 401
    assert client.get("/t", auth=("user", "pass")).status_code == 200


def test_auth_jwt(make_app):
    mod = make_app(AUTH_MODE="jwt", JWT_SECRET="shh")
    client = TestClient(mod.app)
    _add_open_route(client)
    assert client.get("/t").status_code == 401

    token = client.get("/_debug/token").json()["token"]
    assert client.get("/t", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/t", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_auth_session(make_app):
    mod = make_app(AUTH_MODE="session", SESSION_USERNAME="user", SESSION_PASSWORD="pass")
    client = TestClient(mod.app)
    _add_open_route(client)

    assert client.get("/t").status_code == 401
    resp = client.post("/login", json={"username": "user", "password": "wrong"})
    assert resp.status_code == 401

    resp = client.post("/login", json={"username": "user", "password": "pass"})
    assert resp.status_code == 200
    assert client.get("/t").status_code == 200

    resp = client.post("/logout")
    assert resp.status_code == 200
    assert client.get("/t").status_code == 401


def test_logout_404_outside_session_mode(make_app):
    client = TestClient(make_app(AUTH_MODE="none").app)
    assert client.post("/logout").status_code == 404


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None):
        result = _OAUTH_RESULT["value"]
        if isinstance(result, Exception):
            raise result
        return result


_OAUTH_RESULT = {"value": None}


def test_auth_oauth(make_app, monkeypatch):
    mod = make_app(AUTH_MODE="oauth", OAUTH_INTROSPECT_URL="http://oauth/introspect")
    client = TestClient(mod.app)
    _add_open_route(client)
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeAsyncClient)

    assert client.get("/t").status_code == 401

    _OAUTH_RESULT["value"] = _FakeResponse({"active": False})
    assert client.get("/t", headers={"Authorization": "Bearer tok"}).status_code == 401

    _OAUTH_RESULT["value"] = _FakeResponse({"active": True})
    assert client.get("/t", headers={"Authorization": "Bearer tok"}).status_code == 200

    _OAUTH_RESULT["value"] = mod.httpx.HTTPError("unreachable")
    assert client.get("/t", headers={"Authorization": "Bearer tok"}).status_code == 502


def _hmac_sig(secret, ts, body=b""):
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def test_auth_hmac(make_app):
    mod = make_app(AUTH_MODE="hmac", HMAC_SECRET="topsecret")
    client = TestClient(mod.app)
    _add_open_route(client)

    assert client.get("/t").status_code == 401
    assert client.get("/t", headers={"X-Signature": "not-even-the-right-shape"}).status_code == 401

    now = int(time.time())
    assert client.get("/t", headers={"X-Signature": _hmac_sig("wrong", now)}).status_code == 401
    assert client.get("/t", headers={"X-Signature": _hmac_sig("topsecret", now - 3600)}).status_code == 401
    assert client.get("/t", headers={"X-Signature": _hmac_sig("topsecret", now)}).status_code == 200

    body = b'{"x": 1}'
    sig = _hmac_sig("topsecret", int(time.time()), body)
    assert client.post("/t", content=body, headers={"X-Signature": sig, "Content-Type": "application/json"}).status_code == 200
