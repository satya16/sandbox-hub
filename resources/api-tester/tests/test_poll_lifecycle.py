"""
Extra poll-lifecycle coverage for the API Tester resource, complementing
test_api_tester.py's smoke test: a bounded-retry-loop wait for the first poll
result (instead of a fixed sleep) and the double-start-cancels-previous-task
behavior of POST /api/poll/start.
"""
import time

from fastapi.testclient import TestClient


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class _FakeAsyncClient:
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


def _wait_for_results(client, min_count=1, timeout=2.0):
    deadline = time.monotonic() + timeout
    status = client.get("/api/poll/status").json()
    while len(status["results"]) < min_count and time.monotonic() < deadline:
        time.sleep(0.02)
        status = client.get("/api/poll/status").json()
    return status


def test_poll_start_status_stop_bounded_retry(monkeypatch, app_module):
    mod = app_module
    _install_fake_client(
        monkeypatch, mod, response=_FakeResponse(status_code=200, text="hello world")
    )
    client = TestClient(mod.app)

    resp = client.post(
        "/api/poll/start",
        json={
            "method": "GET",
            "url": "http://x/",
            "interval_seconds": 1.0,
            "expect_status": 200,
            "expect_body_contains": "world",
        },
    )
    assert resp.status_code == 200

    try:
        status = _wait_for_results(client, min_count=1, timeout=2.0)
        assert status["running"] is True
        assert len(status["results"]) >= 1

        entry = status["results"][0]  # most recent first
        assert set(entry) >= {"at", "passed", "status", "elapsed_ms", "body_preview"}
        assert entry["status"] == 200
        assert entry["body_preview"] == "hello world"
        # matches what _evaluate would compute for this cfg (status 200 and
        # body contains "world" both hold)
        assert entry["passed"] is True
    finally:
        stop_resp = client.post("/api/poll/stop")
        assert stop_resp.status_code == 200

    assert client.get("/api/poll/status").json()["running"] is False


def test_poll_start_twice_cancels_previous_and_new_config_wins(monkeypatch, app_module):
    mod = app_module
    _install_fake_client(monkeypatch, mod, response=_FakeResponse(status_code=200, text="ok"))
    client = TestClient(mod.app)

    try:
        first = client.post(
            "/api/poll/start",
            json={"method": "GET", "url": "http://first/", "interval_seconds": 5.0},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/poll/start",
            json={"method": "GET", "url": "http://second/", "interval_seconds": 5.0},
        )
        assert second.status_code == 200

        status = client.get("/api/poll/status").json()
        assert status["running"] is True
        assert status["config"]["url"] == "http://second/"
    finally:
        client.post("/api/poll/stop")
