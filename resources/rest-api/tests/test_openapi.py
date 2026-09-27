"""OPENAPI_VERSION and OPENAPI_PROTECT -- the served spec/docs/redoc."""
from fastapi.testclient import TestClient


def test_openapi_version_3_1_default(make_app):
    client = TestClient(make_app(AUTH_MODE="none").app)
    assert client.get("/openapi.json").json()["openapi"] == "3.1.0"


def test_openapi_version_3_0(make_app):
    client = TestClient(make_app(AUTH_MODE="none", OPENAPI_VERSION="3.0").app)
    assert client.get("/openapi.json").json()["openapi"] == "3.0.2"


def test_openapi_protect_gates_the_spec(make_app):
    client = TestClient(
        make_app(AUTH_MODE="none", OPENAPI_PROTECT="true", OPENAPI_TOKEN="spec-token").app
    )

    assert client.get("/openapi.json").status_code == 401
    assert client.get("/openapi.json", headers={"X-API-Key": "wrong-token"}).status_code == 401
    resp = client.get("/openapi.json", headers={"X-API-Key": "spec-token"})
    assert resp.status_code == 200

    # /docs and /redoc are gated the same as /openapi.json, per the module docstring.
    assert client.get("/docs").status_code == 401
    assert client.get("/redoc").status_code == 401
    assert client.get("/docs", headers={"X-API-Key": "wrong-token"}).status_code == 401
    assert client.get("/redoc", headers={"X-API-Key": "wrong-token"}).status_code == 401
    assert client.get("/docs", headers={"X-API-Key": "spec-token"}).status_code == 200
    assert client.get("/redoc", headers={"X-API-Key": "spec-token"}).status_code == 200


def test_openapi_open_by_default(make_app):
    client = TestClient(make_app(AUTH_MODE="apikey", API_KEY="k").app)
    # OPENAPI_PROTECT is independent of AUTH_MODE -- items are gated, the spec isn't.
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_openapi_protect_independent_of_auth_mode(make_app):
    # AUTH_MODE=apikey gates /items with API_KEY; OPENAPI_PROTECT gates
    # /openapi.json with a *separate* OPENAPI_TOKEN -- neither key should work
    # for the other gate.
    mod = make_app(
        AUTH_MODE="apikey",
        API_KEY="items-key",
        OPENAPI_PROTECT="true",
        OPENAPI_TOKEN="spec-key",
    )
    client = TestClient(mod.app)

    assert client.get("/items", headers={"X-API-Key": "items-key"}).status_code == 200
    assert client.get("/items", headers={"X-API-Key": "spec-key"}).status_code == 401

    assert client.get("/openapi.json", headers={"X-API-Key": "spec-key"}).status_code == 200
    assert client.get("/openapi.json", headers={"X-API-Key": "items-key"}).status_code == 401
    assert client.get("/openapi.json").status_code == 401
