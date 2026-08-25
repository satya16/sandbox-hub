"""
A single test endpoint (/test) whose behavior you control live via a small
admin API the hub calls on your behalf -- no restart needed to change it.

Modes:
  normal      -> always 200 {"ok": true}
  rate_limit  -> allows `limit` requests per `window_seconds`, then 429 +
                 Retry-After until the window rolls over
  chaos       -> after `latency_ms` of injected delay, returns a random 5xx
                 `failure_rate` of the time, otherwise the configured
                 `status_code` + `body`

GET  /_config          -> current mode + params (open, just reflects state)
PUT  /_config          -> change mode + params (requires X-Admin-Token)
"""
import asyncio
import os
import random
import time
from collections import deque

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ADMIN_TOKEN = os.environ.get("CHAOS_ADMIN_TOKEN", "dev-admin-token")

app = FastAPI(title="sandbox-hub chaos/rate-limit API")

_config = {
    "mode": "normal",
    "rate_limit": {"limit": 5, "window_seconds": 10},
    "chaos": {"status_code": 200, "body": {"ok": True}, "latency_ms": 0, "failure_rate": 0.0},
}
_hits: deque = deque()


class RateLimitConfig(BaseModel):
    limit: int = 5
    window_seconds: int = 10


class ChaosConfig(BaseModel):
    status_code: int = 200
    body: dict = {"ok": True}
    latency_ms: int = 0
    failure_rate: float = 0.0


class ConfigUpdate(BaseModel):
    mode: str
    rate_limit: RateLimitConfig | None = None
    chaos: ChaosConfig | None = None


@app.get("/health")
def health():
    return {"status": "ok", "mode": _config["mode"]}


@app.get("/_config")
def get_config():
    return _config


@app.put("/_config")
def set_config(update: ConfigUpdate, x_admin_token: str | None = Header(default=None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    if update.mode not in ("normal", "rate_limit", "chaos"):
        raise HTTPException(400, "mode must be one of: normal, rate_limit, chaos")
    _config["mode"] = update.mode
    if update.rate_limit:
        _config["rate_limit"] = update.rate_limit.model_dump()
    if update.chaos:
        _config["chaos"] = update.chaos.model_dump()
    _hits.clear()
    return _config


@app.api_route("/test", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def test_endpoint(request: Request):
    mode = _config["mode"]

    if mode == "normal":
        return JSONResponse({"ok": True})

    if mode == "rate_limit":
        cfg = _config["rate_limit"]
        now = time.time()
        while _hits and _hits[0] <= now - cfg["window_seconds"]:
            _hits.popleft()
        if len(_hits) >= cfg["limit"]:
            retry_after = max(1, int(cfg["window_seconds"] - (now - _hits[0])))
            return JSONResponse(
                {"error": "rate_limited", "limit": cfg["limit"], "window_seconds": cfg["window_seconds"]},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        _hits.append(now)
        return JSONResponse({"ok": True, "remaining": cfg["limit"] - len(_hits)})

    if mode == "chaos":
        cfg = _config["chaos"]
        if cfg["latency_ms"] > 0:
            await asyncio.sleep(cfg["latency_ms"] / 1000)
        if cfg["failure_rate"] > 0 and random.random() < cfg["failure_rate"]:
            status = random.choice([500, 502, 503, 504])
            return JSONResponse({"error": "injected_failure", "status": status}, status_code=status)
        return JSONResponse(cfg["body"], status_code=cfg["status_code"])

    return JSONResponse({"error": f"unknown mode {mode}"}, status_code=500)
