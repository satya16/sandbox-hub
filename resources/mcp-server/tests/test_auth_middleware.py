"""
End-to-end AuthMiddleware coverage: unlike test_mcp_server.py's direct calls
to `AuthMiddleware._check`/`_check_hmac`, these tests drive real ASGI requests
through `mod.app` (a bogus, non-exempt path -- AuthMiddleware runs before
routing, so the path itself never has to resolve) to prove the middleware
actually rejects/admits requests end to end, including that hmac's
buffer-and-replay of the request body leaves it intact for the inner app.
"""
import base64
import hashlib
import hmac
import json
import time

import jwt as pyjwt
from starlette.testclient import TestClient

BOGUS_PATH = "/nonexistent-bogus-path"


def test_apikey_middleware_end_to_end(make_app):
    mod = make_app(AUTH_MODE="apikey", API_KEY="right")
    client = TestClient(mod.app)

    r = client.get(BOGUS_PATH)
    assert r.status_code == 401
    assert "X-API-Key" in r.json()["error"]

    r = client.get(BOGUS_PATH, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401

    r = client.get(BOGUS_PATH, headers={"X-API-Key": "right"})
    assert r.status_code != 401
    assert r.status_code == 404  # reached routing, proving auth passed


def test_basic_middleware_end_to_end(make_app):
    mod = make_app(AUTH_MODE="basic", BASIC_USERNAME="sandbox", BASIC_PASSWORD="pw")
    client = TestClient(mod.app)

    r = client.get(BOGUS_PATH)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Basic"

    wrong_creds = base64.b64encode(b"sandbox:wrong").decode()
    r = client.get(BOGUS_PATH, headers={"Authorization": f"Basic {wrong_creds}"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Basic"

    right_creds = base64.b64encode(b"sandbox:pw").decode()
    r = client.get(BOGUS_PATH, headers={"Authorization": f"Basic {right_creds}"})
    assert r.status_code != 401
    assert r.status_code == 404


def test_jwt_middleware_end_to_end(make_app):
    mod = make_app(AUTH_MODE="jwt", JWT_SECRET="s3cret")
    client = TestClient(mod.app)

    r = client.get(BOGUS_PATH)
    assert r.status_code == 401

    bad_token = pyjwt.encode({"exp": int(time.time()) + 60}, "wrong-secret", algorithm="HS256")
    r = client.get(BOGUS_PATH, headers={"Authorization": f"Bearer {bad_token}"})
    assert r.status_code == 401

    good_token = mod._mint_jwt()
    r = client.get(BOGUS_PATH, headers={"Authorization": f"Bearer {good_token}"})
    assert r.status_code != 401
    assert r.status_code == 404


def test_hmac_middleware_end_to_end_missing_and_wrong(make_app):
    mod = make_app(AUTH_MODE="hmac", HMAC_SECRET="s3cret")
    client = TestClient(mod.app)

    r = client.post(BOGUS_PATH, content=b"{}")
    assert r.status_code == 401
    assert "X-Signature" in r.json()["error"]

    ts = int(time.time())
    wrong_sig = hmac.new(b"wrong-secret", f"{ts}.".encode() + b"{}", hashlib.sha256).hexdigest()
    r = client.post(
        BOGUS_PATH, content=b"{}", headers={"X-Signature": f"t={ts},v1={wrong_sig}"}
    )
    assert r.status_code == 401
    assert "invalid signature" in r.json()["error"]


def test_hmac_middleware_replays_body_to_inner_app(make_app):
    """The middleware must buffer the raw body to verify the signature, then
    hand the inner app a receive() that replays the exact same bytes -- a
    bogus path 404 (rather than a hang or an empty-body error) proves the
    replay worked."""
    mod = make_app(AUTH_MODE="hmac", HMAC_SECRET="s3cret")
    client = TestClient(mod.app)

    payload = {"a": 1}
    raw_body = json.dumps(payload).encode()
    ts = int(time.time())
    sig = hmac.new(b"s3cret", f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()

    r = client.post(
        BOGUS_PATH,
        content=raw_body,
        headers={"X-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"},
    )
    assert r.status_code != 401
    assert r.status_code == 404
