"""GET /health -- always open, reflects the configured auth mode."""
from fastapi.testclient import TestClient


def test_health_reflects_auth_mode(make_app):
    mod = make_app(AUTH_MODE="apikey", API_KEY="k")
    resp = TestClient(mod.app).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["auth_mode"] == "apikey"
    assert isinstance(body["time"], (int, float))
