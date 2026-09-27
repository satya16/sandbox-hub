"""
Unit tests for the chaos/rate-limit API resource (resources/chaos-api/app.py)
-- in-process against a fresh module per test via the make_app/app_module
fixtures in conftest.py, not a running container.
"""
from fastapi.testclient import TestClient


def _client(mod):
    return TestClient(mod.app)


def test_health_reflects_mode(app_module):
    c = _client(app_module)
    assert c.get("/health").json() == {"status": "ok", "mode": "normal"}


def test_config_is_open_and_has_defaults(app_module):
    c = _client(app_module)
    cfg = c.get("/_config").json()
    assert cfg["mode"] == "normal"
    assert cfg["rate_limit"] == {"limit": 5, "window_seconds": 10}
    assert cfg["chaos"]["status_code"] == 200


def test_set_config_requires_admin_token(app_module):
    c = _client(app_module)
    body = {"mode": "normal"}
    assert c.put("/_config", json=body).status_code == 403
    assert c.put("/_config", json=body, headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert c.put("/_config", json=body, headers={"X-Admin-Token": "dev-admin-token"}).status_code == 200


def test_set_config_rejects_unknown_mode(app_module):
    c = _client(app_module)
    resp = c.put(
        "/_config", json={"mode": "bogus"}, headers={"X-Admin-Token": "dev-admin-token"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "mode must be one of: normal, rate_limit, chaos"


def test_set_config_partial_update_leaves_other_section_untouched(app_module):
    c = _client(app_module)
    headers = {"X-Admin-Token": "dev-admin-token"}
    c.put(
        "/_config",
        json={"mode": "rate_limit", "rate_limit": {"limit": 2, "window_seconds": 60}},
        headers=headers,
    )
    cfg = c.put(
        "/_config",
        json={"mode": "chaos", "chaos": {"status_code": 418, "body": {"t": 1}, "latency_ms": 0, "failure_rate": 0}},
        headers=headers,
    ).json()
    assert cfg["rate_limit"] == {"limit": 2, "window_seconds": 60}
    assert cfg["chaos"]["status_code"] == 418


def test_set_config_clears_hit_counter(app_module):
    c = _client(app_module)
    headers = {"X-Admin-Token": "dev-admin-token"}
    c.put(
        "/_config",
        json={"mode": "rate_limit", "rate_limit": {"limit": 1, "window_seconds": 60}},
        headers=headers,
    )
    assert c.get("/test").status_code == 200
    assert c.get("/test").status_code == 429  # limit of 1 exhausted

    c.put(
        "/_config",
        json={"mode": "rate_limit", "rate_limit": {"limit": 1, "window_seconds": 60}},
        headers=headers,
    )
    assert c.get("/test").status_code == 200  # counter reset by reconfiguring


def test_normal_mode_always_ok(app_module):
    c = _client(app_module)
    assert c.get("/test").json() == {"ok": True}
    assert c.post("/test").json() == {"ok": True}
    assert c.put("/test").json() == {"ok": True}
    assert c.patch("/test").json() == {"ok": True}
    assert c.delete("/test").json() == {"ok": True}


def test_rate_limit_mode_allows_then_blocks(app_module):
    c = _client(app_module)
    c.put(
        "/_config",
        json={"mode": "rate_limit", "rate_limit": {"limit": 2, "window_seconds": 10}},
        headers={"X-Admin-Token": "dev-admin-token"},
    )
    first = c.get("/test").json()
    assert first == {"ok": True, "remaining": 1}
    second = c.get("/test").json()
    assert second == {"ok": True, "remaining": 0}

    third = c.get("/test")
    assert third.status_code == 429
    assert "Retry-After" in third.headers
    body = third.json()
    assert body["limit"] == 2
    assert body["window_seconds"] == 10


def test_rate_limit_mode_resets_after_window(app_module, monkeypatch):
    c = _client(app_module)
    c.put(
        "/_config",
        json={"mode": "rate_limit", "rate_limit": {"limit": 1, "window_seconds": 10}},
        headers={"X-Admin-Token": "dev-admin-token"},
    )
    base = app_module.time.time()
    monkeypatch.setattr(app_module.time, "time", lambda: base)
    assert c.get("/test").status_code == 200
    assert c.get("/test").status_code == 429

    monkeypatch.setattr(app_module.time, "time", lambda: base + 11)
    assert c.get("/test").status_code == 200


def test_chaos_mode_injects_latency_without_really_sleeping(app_module, monkeypatch):
    c = _client(app_module)
    c.put(
        "/_config",
        json={
            "mode": "chaos",
            "chaos": {"status_code": 200, "body": {"ok": True}, "latency_ms": 2500, "failure_rate": 0.0},
        },
        headers={"X-Admin-Token": "dev-admin-token"},
    )

    slept = {}

    async def fake_sleep(seconds):
        slept["seconds"] = seconds

    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)
    resp = c.get("/test")
    assert resp.status_code == 200
    assert slept["seconds"] == 2.5


def test_chaos_mode_forced_failure(app_module, monkeypatch):
    c = _client(app_module)
    c.put(
        "/_config",
        json={
            "mode": "chaos",
            "chaos": {"status_code": 200, "body": {"ok": True}, "latency_ms": 0, "failure_rate": 1.0},
        },
        headers={"X-Admin-Token": "dev-admin-token"},
    )
    monkeypatch.setattr(app_module.random, "random", lambda: 0.0)
    monkeypatch.setattr(app_module.random, "choice", lambda seq: 503)

    resp = c.get("/test")
    assert resp.status_code == 503
    assert resp.json() == {"error": "injected_failure", "status": 503}


def test_chaos_mode_no_failure_returns_configured_response(app_module, monkeypatch):
    c = _client(app_module)
    c.put(
        "/_config",
        json={
            "mode": "chaos",
            "chaos": {"status_code": 418, "body": {"teapot": True}, "latency_ms": 0, "failure_rate": 0.5},
        },
        headers={"X-Admin-Token": "dev-admin-token"},
    )
    monkeypatch.setattr(app_module.random, "random", lambda: 0.999)

    resp = c.get("/test")
    assert resp.status_code == 418
    assert resp.json() == {"teapot": True}
