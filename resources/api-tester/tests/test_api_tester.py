"""
Unit tests for the API Tester resource (resources/api-tester/app.py): the
one-off /api/send call and the background poll loop. All outbound HTTP is
faked (via monkeypatching mod.httpx.AsyncClient) so no real network call is
ever made.
"""
import time

import pytest
from fastapi.testclient import TestClient


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class _FakeAsyncClient:
    """Records the single call made through it and returns (or raises)
    whatever the test configured."""

    last_call = None

    def __init__(self, response=None, error=None, **kwargs):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, headers=None, content=None):
        _FakeAsyncClient.last_call = {
            "method": method, "url": url, "headers": headers, "content": content,
        }
        if self._error is not None:
            raise self._error
        return self._response


def _install_fake_client(monkeypatch, mod, response=None, error=None):
    def factory(*args, **kwargs):
        return _FakeAsyncClient(response=response, error=error)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)


def test_send_success_passes_through_request_details(monkeypatch, app_module):
    mod = app_module
    _install_fake_client(
        monkeypatch, mod,
        response=_FakeResponse(status_code=201, headers={"x-a": "1"}, text='{"ok":true}'),
    )
    client = TestClient(mod.app)

    resp = client.post(
        "/api/send",
        json={"method": "post", "url": "http://x/y", "headers": {"X-Test": "1"}, "body": "hi"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == 201
    assert body["headers"] == {"x-a": "1"}
    assert body["body"] == '{"ok":true}'
    assert isinstance(body["elapsed_ms"], (int, float))

    call = _FakeAsyncClient.last_call
    assert call["method"] == "POST"
    assert call["url"] == "http://x/y"
    assert call["headers"] == {"X-Test": "1"}
    assert call["content"] == b"hi"


def test_send_empty_headers_and_body_pass_none(monkeypatch, app_module):
    mod = app_module
    _install_fake_client(monkeypatch, mod, response=_FakeResponse())
    client = TestClient(mod.app)

    client.post("/api/send", json={"method": "get", "url": "http://x/"})

    call = _FakeAsyncClient.last_call
    assert call["headers"] is None
    assert call["content"] is None


def test_send_failure_reports_error(monkeypatch, app_module):
    mod = app_module
    _install_fake_client(monkeypatch, mod, error=mod.httpx.HTTPError("boom"))
    client = TestClient(mod.app)

    resp = client.post("/api/send", json={"method": "GET", "url": "http://unreachable/"})
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] is None
    assert body["headers"] == {}
    assert body["body"] is None
    assert "boom" in body["error"]
    assert isinstance(body["elapsed_ms"], (int, float))


# ------------------------------------------------------------------ _evaluate


def _cfg(mod, **overrides):
    defaults = dict(method="GET", url="http://x/")
    defaults.update(overrides)
    return mod.PollRequest(**defaults)


def test_evaluate_fails_when_request_itself_failed(app_module):
    mod = app_module
    result = {"ok": False, "status": None, "body": None}
    cfg = _cfg(mod, expect_status=200)
    assert mod._evaluate(result, cfg) is False


def test_evaluate_status_match(app_module):
    mod = app_module
    result = {"ok": True, "status": 200, "body": "hello"}
    assert mod._evaluate(result, _cfg(mod, expect_status=200)) is True
    assert mod._evaluate(result, _cfg(mod, expect_status=404)) is False


def test_evaluate_body_contains(app_module):
    mod = app_module
    result = {"ok": True, "status": 200, "body": "hello world"}
    assert mod._evaluate(result, _cfg(mod, expect_body_contains="world")) is True
    assert mod._evaluate(result, _cfg(mod, expect_body_contains="missing")) is False


def test_evaluate_combined_criteria(app_module):
    mod = app_module
    result = {"ok": True, "status": 200, "body": "hello world"}
    cfg = _cfg(mod, expect_status=200, expect_body_contains="world")
    assert mod._evaluate(result, cfg) is True
    cfg_wrong_status = _cfg(mod, expect_status=500, expect_body_contains="world")
    assert mod._evaluate(result, cfg_wrong_status) is False


def test_evaluate_no_criteria_only_requires_ok(app_module):
    mod = app_module
    assert mod._evaluate({"ok": True, "status": 500, "body": ""}, _cfg(mod)) is True


# ---------------------------------------------------------------- endpoints


def test_poll_status_before_starting(app_module):
    client = TestClient(app_module.app)
    body = client.get("/api/poll/status").json()
    assert body == {"running": False, "config": None, "results": []}


def test_health_reflects_polling_state(app_module):
    client = TestClient(app_module.app)
    assert client.get("/health").json()["polling"] is False


def test_poll_start_status_stop_smoke(monkeypatch, app_module):
    mod = app_module
    _install_fake_client(monkeypatch, mod, response=_FakeResponse(status_code=200, text="ok"))
    client = TestClient(mod.app)

    resp = client.post(
        "/api/poll/start",
        json={"method": "GET", "url": "http://x/", "interval_seconds": 0.05, "expect_status": 200},
    )
    assert resp.status_code == 200
    try:
        time.sleep(0.2)
        status = client.get("/api/poll/status").json()
        assert status["running"] is True
        assert status["config"]["url"] == "http://x/"
        assert len(status["results"]) >= 1
        latest = status["results"][0]  # most recent first
        assert latest["passed"] is True
        assert latest["status"] == 200
        assert isinstance(latest["elapsed_ms"], (int, float))
    finally:
        stop_resp = client.post("/api/poll/stop")
        assert stop_resp.status_code == 200

    assert client.get("/api/poll/status").json()["running"] is False
    assert client.get("/health").json()["polling"] is False
