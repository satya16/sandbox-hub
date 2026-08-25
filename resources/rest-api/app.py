"""
A small sample REST API whose auth behavior is controlled entirely by env vars,
so the same image serves as sandbox-hub's "plain", "api-key", and "oauth"
REST API resources.

AUTH_MODE=none    -> no auth
AUTH_MODE=apikey  -> requires header  X-API-Key: <API_KEY>
AUTH_MODE=oauth   -> requires header  Authorization: Bearer <token>,
                     validated by POSTing to OAUTH_INTROSPECT_URL
"""
import os
import time

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException

AUTH_MODE = os.environ.get("AUTH_MODE", "none")
API_KEY = os.environ.get("API_KEY", "")
OAUTH_INTROSPECT_URL = os.environ.get("OAUTH_INTROSPECT_URL", "")

app = FastAPI(title=f"sandbox-hub sample REST API ({AUTH_MODE})")

_items = {
    1: {"id": 1, "name": "widget"},
    2: {"id": 2, "name": "gadget"},
}


async def require_auth(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    if AUTH_MODE == "none":
        return {"mode": "none"}

    if AUTH_MODE == "apikey":
        if not x_api_key or x_api_key != API_KEY:
            raise HTTPException(401, "missing or invalid X-API-Key header")
        return {"mode": "apikey"}

    if AUTH_MODE == "oauth":
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing Bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                resp = await client.post(OAUTH_INTROSPECT_URL, data={"token": token})
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"could not reach oauth provider: {exc}")
        data = resp.json()
        if not data.get("active"):
            raise HTTPException(401, "token is not active")
        return {"mode": "oauth", "client_id": data.get("client_id"), "scope": data.get("scope")}

    raise HTTPException(500, f"unknown AUTH_MODE={AUTH_MODE}")


@app.get("/health")
def health():
    return {"status": "ok", "auth_mode": AUTH_MODE, "time": time.time()}


@app.get("/whoami")
def whoami(auth=Depends(require_auth)):
    return {"authenticated_as": auth}


@app.get("/items")
def list_items(auth=Depends(require_auth)):
    return list(_items.values())


@app.get("/items/{item_id}")
def get_item(item_id: int, auth=Depends(require_auth)):
    if item_id not in _items:
        raise HTTPException(404, "not found")
    return _items[item_id]


@app.post("/items")
def create_item(name: str, auth=Depends(require_auth)):
    new_id = max(_items) + 1
    _items[new_id] = {"id": new_id, "name": name}
    return _items[new_id]
