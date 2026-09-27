"""
Unit tests for the sample MCP server resource (resources/mcp-server/server.py):
the plain tool functions, the conditional AuthMiddleware wrapping, the four
always-unauthed routes, and AuthMiddleware's own request-classification logic.
Deliberately does not attempt full MCP streamable-http protocol traffic
(initialize handshake, session negotiation, tool invocation over the wire) --
that's exercised end to end by the hub's own Docker-based integration suite.
"""
import base64
import hashlib
import hmac
import time

import jwt as pyjwt
import pytest
from starlette.testclient import TestClient


def test_tool_functions_are_plain_callables(app_module):
    mod = app_module
    assert mod.echo("hi") == "hi"
    assert mod.add(2, 3) == 5
    now = float(mod.current_time())
    assert abs(now - time.time()) < 5


@pytest.mark.parametrize("mode", ["apikey", "basic", "jwt", "session", "hmac"])
def test_wrapped_in_auth_middleware_for_header_based_modes(make_app, mode):
    mod = make_app(AUTH_MODE=mode)
    assert isinstance(mod.app, mod.AuthMiddleware)


@pytest.mark.parametrize("mode", ["none", "oauth"])
def test_not_wrapped_for_none_and_oauth(make_app, mode):
    kwargs = {"AUTH_MODE": mode}
    if mode == "oauth":
        kwargs.update(OAUTH_INTROSPECT_URL="http://provider/introspect", OAUTH_ISSUER_URL="http://provider", PUBLIC_URL="http://localhost:8000")
    mod = make_app(**kwargs)
    assert not isinstance(mod.app, mod.AuthMiddleware)


@pytest.mark.parametrize("mode", ["apikey", "hmac"])
def test_unauthed_paths_reachable_without_credentials(make_app, mode):
    mod = make_app(AUTH_MODE=mode, API_KEY="k", HMAC_SECRET="s")
    client = TestClient(mod.app)
    assert client.get("/health").status_code == 200
    # /_debug/token and /login/logout 404 unless their own mode is active,
    # but that 404 comes from inside the (unauthed) handler, not a 401 from
    # the middleware -- proving these paths bypass auth entirely.
    assert client.get("/_debug/token").status_code == 404
    assert client.post("/login", json={}).status_code == 404
    assert client.post("/logout").status_code == 404


def test_health_reports_auth_mode(make_app):
    for mode in ("none", "apikey"):
        mod = make_app(AUTH_MODE=mode, API_KEY="k")
        client = TestClient(mod.app)
        assert client.get("/health").json() == {"status": "ok", "auth_mode": mode}


def test_debug_token_requires_jwt_mode(make_app):
    non_jwt = make_app(AUTH_MODE="none")
    assert TestClient(non_jwt.app).get("/_debug/token").status_code == 404

    jwt_mod = make_app(AUTH_MODE="jwt", JWT_SECRET="s3cret")
    resp = TestClient(jwt_mod.app).get("/_debug/token")
    assert resp.status_code == 200
    token = resp.json()["token"]
    pyjwt.decode(token, "s3cret", algorithms=["HS256"])  # doesn't raise


def test_login_logout_require_session_mode(make_app):
    non_session = make_app(AUTH_MODE="none")
    assert TestClient(non_session.app).post("/login", json={}).status_code == 404
    assert TestClient(non_session.app).post("/logout").status_code == 404

    mod = make_app(AUTH_MODE="session", SESSION_USERNAME="u", SESSION_PASSWORD="p")
    client = TestClient(mod.app)
    assert client.post("/login", json={"username": "u", "password": "wrong"}).status_code == 401
    ok = client.post("/login", json={"username": "u", "password": "p"})
    assert ok.status_code == 200
    session_id = ok.cookies.get("sandboxhub_session")
    assert session_id in mod._active_sessions

    logout = client.post("/logout")
    assert logout.status_code == 200
    assert session_id not in mod._active_sessions


# ---------------------------------------------------- AuthMiddleware._check*


def test_check_apikey(app_module):
    mod = app_module
    mw = mod.AuthMiddleware(None)
    mod.AUTH_MODE = "apikey"
    mod.API_KEY = "right"
    assert mw._check({}) == (False, "missing or invalid X-API-Key header", None)
    assert mw._check({"x-api-key": "wrong"}) == (False, "missing or invalid X-API-Key header", None)
    assert mw._check({"x-api-key": "right"}) == (True, None, None)


def test_check_basic(app_module):
    mod = app_module
    mod.AUTH_MODE = "basic"
    mod.BASIC_USERNAME, mod.BASIC_PASSWORD = "sandbox", "pw"
    mw = mod.AuthMiddleware(None)

    ok, err, challenge = mw._check({})
    assert (ok, challenge) == (False, "Basic")

    bad_b64 = {"authorization": "Basic not-base64!!"}
    ok, err, challenge = mw._check(bad_b64)
    assert (ok, challenge) == (False, "Basic")

    creds = base64.b64encode(b"sandbox:wrong").decode()
    ok, err, challenge = mw._check({"authorization": f"Basic {creds}"})
    assert (ok, challenge) == (False, "Basic")

    creds = base64.b64encode(b"sandbox:pw").decode()
    assert mw._check({"authorization": f"Basic {creds}"}) == (True, None, None)


def test_check_jwt(app_module):
    mod = app_module
    mod.AUTH_MODE = "jwt"
    mod.JWT_SECRET = "s3cret"
    mw = mod.AuthMiddleware(None)

    assert mw._check({})[0] is False

    expired = pyjwt.encode({"exp": int(time.time()) - 10}, "s3cret", algorithm="HS256")
    ok, err, _ = mw._check({"authorization": f"Bearer {expired}"})
    assert ok is False and "expired" in err

    wrong_secret = pyjwt.encode({"exp": int(time.time()) + 60}, "other", algorithm="HS256")
    ok, err, _ = mw._check({"authorization": f"Bearer {wrong_secret}"})
    assert ok is False and "invalid token" in err

    valid = mod._mint_jwt()
    assert mw._check({"authorization": f"Bearer {valid}"}) == (True, None, None)


def test_check_session(app_module):
    mod = app_module
    mod.AUTH_MODE = "session"
    mw = mod.AuthMiddleware(None)
    assert mw._check({})[0] is False
    assert mw._check({"cookie": "sandboxhub_session=unknown"})[0] is False

    mod._active_sessions.add("abc123")
    assert mw._check({"cookie": "sandboxhub_session=abc123"}) == (True, None, None)


def test_check_none_and_oauth_pass_through(app_module):
    mod = app_module
    mw = mod.AuthMiddleware(None)
    assert mw._check({}) == (True, None, None)


def test_check_hmac(app_module):
    mod = app_module
    mod.HMAC_SECRET = "s3cret"
    mw = mod.AuthMiddleware(None)

    assert mw._check_hmac({}, b"")[0] is False
    assert mw._check_hmac({"x-signature": "garbage"}, b"")[0] is False

    stale_ts = int(time.time()) - 3600
    stale_sig = hmac.new(b"s3cret", f"{stale_ts}.".encode(), hashlib.sha256).hexdigest()
    ok, err = mw._check_hmac({"x-signature": f"t={stale_ts},v1={stale_sig}"}, b"")
    assert ok is False and "tolerance" in err

    ts = int(time.time())
    wrong_sig = hmac.new(b"wrong-secret", f"{ts}.".encode(), hashlib.sha256).hexdigest()
    ok, err = mw._check_hmac({"x-signature": f"t={ts},v1={wrong_sig}"}, b"")
    assert ok is False and "invalid signature" in err

    body = b'{"a":1}'
    good_sig = hmac.new(b"s3cret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert mw._check_hmac({"x-signature": f"t={ts},v1={good_sig}"}, body) == (True, None)
