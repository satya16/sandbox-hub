"""Every AUTH_MODE app.py supports, exercised against the two endpoints it
actually gates (/items, /whoami) plus the auth-mode-specific helper routes
(/login, /logout, /_debug/token)."""
import hashlib
import hmac
import time

from fastapi.testclient import TestClient


def test_auth_none(make_app):
    mod = make_app(AUTH_MODE="none")
    client = TestClient(mod.app)
    assert client.get("/items").status_code == 200
    assert client.get("/whoami").json() == {"authenticated_as": {"mode": "none"}}


def test_auth_apikey(make_app):
    mod = make_app(AUTH_MODE="apikey", API_KEY="secret123")
    client = TestClient(mod.app)
    assert client.get("/items").status_code == 401
    assert client.get("/items", headers={"X-API-Key": "wrong"}).status_code == 401
    resp = client.get("/whoami", headers={"X-API-Key": "secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"authenticated_as": {"mode": "apikey"}}


def test_auth_basic(make_app):
    mod = make_app(AUTH_MODE="basic", BASIC_USERNAME="sandbox", BASIC_PASSWORD="pw")
    client = TestClient(mod.app)

    resp = client.get("/items")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Basic"

    assert client.get("/items", headers={"Authorization": "Basic not-valid-base64!!"}).status_code == 401
    assert client.get("/items", auth=("sandbox", "wrong")).status_code == 401

    resp = client.get("/whoami", auth=("sandbox", "pw"))
    assert resp.status_code == 200
    assert resp.json() == {"authenticated_as": {"mode": "basic", "user": "sandbox"}}


def test_auth_jwt(make_app):
    mod = make_app(AUTH_MODE="jwt", JWT_SECRET="jwtsecret")
    client = TestClient(mod.app)

    assert client.get("/items").status_code == 401
    # non-Bearer Authorization hits the same "missing Bearer token" branch as no header at all.
    assert client.get("/items", headers={"Authorization": "Basic dXNlcjpwYXNz"}).status_code == 401

    token = client.get("/_debug/token").json()["token"]
    resp = client.get("/items", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    whoami = client.get("/whoami", headers={"Authorization": f"Bearer {token}"}).json()
    assert whoami["authenticated_as"]["claims"]["sub"] == "sandbox-hub-tester"

    expired = mod.pyjwt.encode(
        {"sub": "x", "iat": 0, "exp": 1}, "jwtsecret", algorithm="HS256"
    )
    resp = client.get("/items", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token expired"

    other_secret = mod.pyjwt.encode(
        {"sub": "x", "iat": int(time.time()), "exp": int(time.time()) + 3600},
        "some-other-secret",
        algorithm="HS256",
    )
    resp = client.get("/items", headers={"Authorization": f"Bearer {other_secret}"})
    assert resp.status_code == 401
    assert "invalid token" in resp.json()["detail"]

    resp = client.get("/items", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401
    assert "invalid token" in resp.json()["detail"]


def test_debug_token_404_unless_jwt_mode(make_app):
    mod = make_app(AUTH_MODE="none")
    assert TestClient(mod.app).get("/_debug/token").status_code == 404


def test_auth_session(make_app):
    mod = make_app(AUTH_MODE="session", SESSION_USERNAME="sandbox", SESSION_PASSWORD="pw")
    client = TestClient(mod.app)

    resp = client.get("/items")
    assert resp.status_code == 401
    assert "not logged in" in resp.json()["detail"]
    assert client.post("/login", json={"username": "sandbox", "password": "wrong"}).status_code == 401
    assert client.post("/login", json={"username": "wrong", "password": "pw"}).status_code == 401

    login_resp = client.post("/login", json={"username": "sandbox", "password": "pw"})
    assert login_resp.status_code == 200
    session_id = login_resp.cookies.get("sandboxhub_session")
    assert session_id

    assert client.get("/items").status_code == 200

    assert client.post("/logout").status_code == 200
    # A client still presenting the now-invalidated cookie is rejected --
    # use a fresh client so our own jar's logout cookie-clear doesn't mask it.
    stale_client = TestClient(mod.app, cookies={"sandboxhub_session": session_id})
    assert stale_client.get("/items").status_code == 401


def test_login_logout_404_unless_session_mode(make_app):
    mod = make_app(AUTH_MODE="none")
    client = TestClient(mod.app)
    assert client.post("/login", json={}).status_code == 404
    assert client.post("/logout").status_code == 404


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

    assert client.get("/items").status_code == 401
    # non-Bearer Authorization hits the same "missing Bearer token" branch as no header at all.
    assert client.get("/items", headers={"Authorization": "Basic dXNlcjpwYXNz"}).status_code == 401

    monkeypatch.setattr(
        mod.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(
            response=_FakeOauthResponse({"active": True, "client_id": "abc", "scope": "read"})
        ),
    )
    resp = client.get("/whoami", headers={"Authorization": "Bearer at_x"})
    assert resp.status_code == 200
    assert resp.json() == {
        "authenticated_as": {"mode": "oauth", "client_id": "abc", "scope": "read"}
    }

    monkeypatch.setattr(
        mod.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(response=_FakeOauthResponse({"active": False})),
    )
    resp = client.get("/items", headers={"Authorization": "Bearer at_x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token is not active"

    monkeypatch.setattr(
        mod.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(exc=mod.httpx.HTTPError("boom")),
    )
    resp = client.get("/items", headers={"Authorization": "Bearer at_x"})
    assert resp.status_code == 502
    assert "could not reach oauth provider" in resp.json()["detail"]


def _hmac_sig(secret: str, ts: int, body: bytes = b"") -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def test_auth_hmac(make_app):
    mod = make_app(AUTH_MODE="hmac", HMAC_SECRET="hmacsecret")
    client = TestClient(mod.app)

    resp = client.get("/items")
    assert resp.status_code == 401
    assert "missing or malformed" in resp.json()["detail"]
    assert client.get("/items", headers={"X-Signature": "not-even-the-right-shape"}).status_code == 401

    now = int(time.time())
    resp = client.get("/items", headers={"X-Signature": _hmac_sig("wrong-secret", now)})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid signature"

    resp = client.get("/items", headers={"X-Signature": _hmac_sig("hmacsecret", now - 3600)})
    assert resp.status_code == 401
    assert "outside tolerance window" in resp.json()["detail"]

    assert client.get("/items", headers={"X-Signature": _hmac_sig("hmacsecret", now)}).status_code == 200

    body = b'{"foo": "bar"}'
    sig = _hmac_sig("hmacsecret", now, body)
    resp = client.post(
        "/jobs",
        content=body,
        headers={"X-Signature": sig, "Content-Type": "application/json"},
    )
    # ASYNC_JOBS isn't enabled here -- reaching the 404 (rather than a 401)
    # proves the hmac signature over a real POST body was accepted.
    assert resp.status_code == 404
