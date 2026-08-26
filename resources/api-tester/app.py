"""
A minimal, ephemeral REST client -- point it at any local URL (typically
another sandbox-hub instance) and send one-off requests, or set it polling
that URL on an interval and checking each response against a simple
expected status / body-contains rule.

Everything server-side, on purpose: the browser hits this container's own
API, which does the actual outbound HTTP call itself. That sidesteps CORS
entirely (the target doesn't need to allow this origin) and means polling
keeps running in the background whether or not anyone has the page open.

Nothing is persisted -- requests, poll config, and poll history all live in
memory and vanish on restart. Not a saved-collections tool; a disposable one.
"""
import asyncio
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="sandbox-hub API tester")

_poll_task: Optional[asyncio.Task] = None
_poll_state = {"running": False, "config": None, "results": deque(maxlen=100)}


class SendRequest(BaseModel):
    method: str = "GET"
    url: str
    headers: dict[str, str] = {}
    body: Optional[str] = None
    timeout_seconds: float = 10.0


async def _do_send(req: SendRequest) -> dict:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=req.timeout_seconds, follow_redirects=True) as client:
            resp = await client.request(
                req.method.upper(),
                req.url,
                headers=req.headers or None,
                content=req.body.encode() if req.body else None,
            )
        return {
            "ok": True,
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.text,
            "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": None,
            "headers": {},
            "body": None,
            "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            "error": str(exc),
        }


@app.post("/api/send")
async def send(req: SendRequest):
    return await _do_send(req)


class PollRequest(SendRequest):
    interval_seconds: float = 5.0
    expect_status: Optional[int] = None
    expect_body_contains: Optional[str] = None


def _evaluate(result: dict, cfg: PollRequest) -> bool:
    if not result["ok"]:
        return False
    if cfg.expect_status is not None and result["status"] != cfg.expect_status:
        return False
    if cfg.expect_body_contains and cfg.expect_body_contains not in (result["body"] or ""):
        return False
    return True


async def _poll_loop(cfg: PollRequest):
    while True:
        result = await _do_send(cfg)
        passed = _evaluate(result, cfg)
        entry = {
            "at": time.time(),
            "passed": passed,
            "status": result["status"],
            "elapsed_ms": result["elapsed_ms"],
            "error": result.get("error"),
            "body_preview": (result["body"] or "")[:300] if result["ok"] else None,
        }
        _poll_state["results"].append(entry)
        tag = "PASS" if passed else "FAIL"
        print(
            f"[poll] {tag} {cfg.method.upper()} {cfg.url} -> status={result['status']} "
            f"{result['elapsed_ms']}ms {result.get('error') or ''}",
            flush=True,
        )
        await asyncio.sleep(max(1.0, cfg.interval_seconds))


@app.post("/api/poll/start")
async def poll_start(cfg: PollRequest):
    global _poll_task
    if _poll_task is not None:
        _poll_task.cancel()
    _poll_state["running"] = True
    _poll_state["config"] = cfg.model_dump()
    _poll_state["results"].clear()
    _poll_task = asyncio.create_task(_poll_loop(cfg))
    return {"ok": True}


@app.post("/api/poll/stop")
async def poll_stop():
    global _poll_task
    if _poll_task is not None:
        _poll_task.cancel()
        _poll_task = None
    _poll_state["running"] = False
    return {"ok": True}


@app.get("/api/poll/status")
def poll_status():
    return {
        "running": _poll_state["running"],
        "config": _poll_state["config"],
        "results": list(_poll_state["results"])[::-1],
    }


@app.get("/health")
def health():
    return {"status": "ok", "polling": _poll_state["running"]}


_INDEX_HTML = Path(__file__).parent / "index.html"


@app.get("/")
def index():
    return FileResponse(_INDEX_HTML)
