"""
Unit tests for the local OAuth2 provider resource (resources/oauth-provider/app.py)
-- in-process against a fresh module per test via the make_app/app_module
fixtures in conftest.py, not a running container.
"""
import base64
import hashlib
import secrets

from fastapi.testclient import TestClient


def _client(mod):
    return TestClient(mod.app)


def _register_client(c, name="test-client", redirect_uris=None):
    resp = c.post(
        "/admin/clients",
        json={"name": name, "redirect_uris": redirect_uris or []},
        headers={"X-Admin-Token": "dev-admin-token"},
    )
    assert resp.status_code == 200
    return resp.json()


def test_health_reflects_counts(app_module):
    c = _client(app_module)
    assert c.get("/health").json() == {"status": "ok", "clients": 0, "live_tokens": 0}

    creds = _register_client(c)
    c.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    assert c.get("/health").json() == {"status": "ok", "clients": 1, "live_tokens": 1}


def test_register_client_requires_admin_token(app_module):
    c = _client(app_module)
    body = {"name": "x", "redirect_uris": []}
    assert c.post("/admin/clients", json=body).status_code == 403
    assert c.post("/admin/clients", json=body, headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert c.post("/admin/clients", json=body, headers={"X-Admin-Token": "dev-admin-token"}).status_code == 200


def test_register_and_delete_client(app_module):
    c = _client(app_module)
    creds = _register_client(c)
    assert "client_id" in creds and "client_secret" in creds

    del_resp = c.delete(f"/admin/clients/{creds['client_id']}", headers={"X-Admin-Token": "dev-admin-token"})
    assert del_resp.status_code == 200

    # Idempotent -- deleting again (or an unknown id) still succeeds.
    again = c.delete(f"/admin/clients/{creds['client_id']}", headers={"X-Admin-Token": "dev-admin-token"})
    assert again.status_code == 200

    # The client_id is no longer recognized once deleted.
    resp = c.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    assert resp.status_code == 401


def test_delete_client_requires_admin_token(app_module):
    c = _client(app_module)
    creds = _register_client(c)
    assert c.delete(f"/admin/clients/{creds['client_id']}").status_code == 403


def test_metadata_endpoint(app_module):
    c = _client(app_module)
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"].endswith("/authorize")
    assert meta["token_endpoint"].endswith("/token")
    assert meta["introspection_endpoint"].endswith("/introspect")
    assert set(meta["grant_types_supported"]) == {"authorization_code", "client_credentials", "refresh_token"}
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["response_types_supported"] == ["code"]
    assert meta["issuer"] == meta["authorization_endpoint"].removesuffix("/authorize")


def test_authorize_unknown_client(app_module):
    c = _client(app_module)
    resp = c.get("/authorize", params={"client_id": "nope", "redirect_uri": "https://x/cb"})
    assert resp.status_code == 400


def test_authorize_no_redirect_uris_registered_accepts_any(app_module):
    c = _client(app_module)
    creds = _register_client(c, redirect_uris=[])
    resp = c.get(
        "/authorize",
        params={"client_id": creds["client_id"], "redirect_uri": "https://anything/cb"},
    )
    assert resp.status_code == 200


def test_authorize_redirect_uri_not_registered(app_module):
    c = _client(app_module)
    creds = _register_client(c, redirect_uris=["https://allowed/cb"])
    resp = c.get(
        "/authorize",
        params={"client_id": creds["client_id"], "redirect_uri": "https://not-allowed/cb"},
    )
    assert resp.status_code == 400


def test_authorize_shows_approve_page(app_module):
    c = _client(app_module)
    creds = _register_client(c, name="My Test App", redirect_uris=["https://allowed/cb"])
    resp = c.get(
        "/authorize",
        params={"client_id": creds["client_id"], "redirect_uri": "https://allowed/cb"},
    )
    assert resp.status_code == 200
    assert "My Test App" in resp.text
    assert "/authorize/approve" in resp.text


def test_authorize_approve_redirects_with_code_and_state(app_module):
    c = TestClient(app_module.app, follow_redirects=False)
    creds = _register_client(_client(app_module), redirect_uris=["https://allowed/cb"])
    resp = c.get(
        "/authorize/approve",
        params={
            "client_id": creds["client_id"],
            "redirect_uri": "https://allowed/cb",
            "state": "xyz",
        },
    )
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("https://allowed/cb?code=")
    assert "&state=xyz" in location


def _get_auth_code(mod, client_id, redirect_uri, code_challenge=None):
    c = TestClient(mod.app, follow_redirects=False)
    params = {"client_id": client_id, "redirect_uri": redirect_uri}
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    resp = c.get("/authorize/approve", params=params)
    location = resp.headers["location"]
    query = location.split("?", 1)[1]
    code = dict(p.split("=", 1) for p in query.split("&"))["code"]
    return code


def test_token_client_credentials(app_module):
    c = _client(app_module)
    creds = _register_client(c)

    bad = c.post(
        "/token",
        data={"grant_type": "client_credentials", "client_id": creds["client_id"], "client_secret": "wrong"},
    )
    assert bad.status_code == 401

    good = c.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    )
    assert good.status_code == 200
    body = good.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 120
    assert body["scope"] == ""
    assert "access_token" in body and "refresh_token" in body


def test_token_authorization_code_flow(app_module):
    c = _client(app_module)
    creds = _register_client(c, redirect_uris=["https://allowed/cb"])
    code = _get_auth_code(app_module, creds["client_id"], "https://allowed/cb")

    resp = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": creds["client_id"],
            "code": code,
            "redirect_uri": "https://allowed/cb",
        },
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()

    # The code is single-use.
    reuse = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": creds["client_id"],
            "code": code,
            "redirect_uri": "https://allowed/cb",
        },
    )
    assert reuse.status_code == 400


def test_token_authorization_code_expired(app_module):
    c = _client(app_module)
    creds = _register_client(c, redirect_uris=["https://allowed/cb"])
    code = _get_auth_code(app_module, creds["client_id"], "https://allowed/cb")
    app_module.auth_codes[code]["expires_at"] = 0  # force expiry without waiting

    resp = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": creds["client_id"],
            "code": code,
            "redirect_uri": "https://allowed/cb",
        },
    )
    assert resp.status_code == 400


def test_token_authorization_code_client_id_mismatch(app_module):
    c = _client(app_module)
    creds = _register_client(c, redirect_uris=["https://allowed/cb"])
    other = _register_client(c, name="other", redirect_uris=["https://allowed/cb"])
    code = _get_auth_code(app_module, creds["client_id"], "https://allowed/cb")

    resp = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": other["client_id"],
            "code": code,
            "redirect_uri": "https://allowed/cb",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "client_id mismatch"


def test_token_authorization_code_redirect_uri_mismatch(app_module):
    c = _client(app_module)
    creds = _register_client(c, redirect_uris=["https://allowed/cb", "https://allowed/cb2"])
    code = _get_auth_code(app_module, creds["client_id"], "https://allowed/cb")

    resp = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": creds["client_id"],
            "code": code,
            "redirect_uri": "https://allowed/cb2",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "redirect_uri mismatch"


def test_token_authorization_code_pkce(app_module):
    c = _client(app_module)
    creds = _register_client(c, redirect_uris=["https://allowed/cb"])

    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    # A code is popped (single-use) as soon as a token exchange is attempted
    # against it, whether or not the PKCE check passes -- so the "wrong
    # verifier" and "right verifier" cases each need their own fresh code.
    wrong_code = _get_auth_code(app_module, creds["client_id"], "https://allowed/cb", code_challenge=challenge)
    wrong = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": creds["client_id"],
            "code": wrong_code,
            "redirect_uri": "https://allowed/cb",
            "code_verifier": "not-the-right-verifier",
        },
    )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "invalid code_verifier"

    right_code = _get_auth_code(app_module, creds["client_id"], "https://allowed/cb", code_challenge=challenge)
    right = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": creds["client_id"],
            "code": right_code,
            "redirect_uri": "https://allowed/cb",
            "code_verifier": verifier,
        },
    )
    assert right.status_code == 200


def test_token_refresh_rotates_and_invalidates_old(app_module):
    c = _client(app_module)
    creds = _register_client(c)
    initial = c.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    ).json()

    refreshed = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": initial["refresh_token"]})
    assert refreshed.status_code == 200
    new_tokens = refreshed.json()
    assert new_tokens["access_token"] != initial["access_token"]
    assert new_tokens["refresh_token"] != initial["refresh_token"]

    # The old refresh token was invalidated by that rotation -- reusing it fails.
    reuse = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": initial["refresh_token"]})
    assert reuse.status_code == 400


def test_token_refresh_invalid_token(app_module):
    c = _client(app_module)
    resp = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": "not-a-real-token"})
    assert resp.status_code == 400


def test_token_refresh_client_id_mismatch(app_module):
    c = _client(app_module)
    creds = _register_client(c)
    other = _register_client(c, name="other")
    initial = c.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
    ).json()

    resp = c.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": initial["refresh_token"],
            "client_id": other["client_id"],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "client_id mismatch"


def test_token_unsupported_grant_type(app_module):
    c = _client(app_module)
    resp = c.post("/token", data={"grant_type": "not-a-real-grant"})
    assert resp.status_code == 400


def test_introspect(app_module):
    c = _client(app_module)
    creds = _register_client(c)
    issued = c.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "scope": "read write",
        },
    ).json()

    active = c.post("/introspect", data={"token": issued["access_token"]}).json()
    assert active["active"] is True
    assert active["client_id"] == creds["client_id"]
    assert active["scope"] == "read write"
    assert "exp" in active

    unknown = c.post("/introspect", data={"token": "no-such-token"}).json()
    assert unknown == {"active": False}

    app_module.tokens[issued["access_token"]]["expires_at"] = 0
    expired = c.post("/introspect", data={"token": issued["access_token"]}).json()
    assert expired == {"active": False}
