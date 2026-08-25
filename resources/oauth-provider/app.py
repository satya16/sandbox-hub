"""
Minimal local OAuth2 authorization server for testing purposes only.

Supports:
  - client_credentials grant (machine-to-machine)
  - authorization_code grant + PKCE (interactive login flow)
  - token introspection (RFC 7662) for resource servers to validate tokens
  - dynamic client registration via an internal admin endpoint used by the hub

State is in-memory and resets on restart -- this is disposable test infra,
not a real identity provider. Do not use for anything real.
"""
import base64
import hashlib
import os
import secrets
import time
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

ADMIN_TOKEN = os.environ.get("OAUTH_ADMIN_TOKEN", "dev-admin-token")
TOKEN_TTL_SECONDS = 3600
AUTH_CODE_TTL_SECONDS = 120

app = FastAPI(title="sandbox-hub local OAuth provider")

clients: dict[str, dict] = {}
auth_codes: dict[str, dict] = {}
tokens: dict[str, dict] = {}


def new_id(prefix: str, n: int = 16) -> str:
    return f"{prefix}_{secrets.token_urlsafe(n)}"


class ClientRegistration(BaseModel):
    name: str
    redirect_uris: list[str] = []


@app.post("/admin/clients")
def register_client(reg: ClientRegistration, request: Request):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    client_id = new_id("client", 8)
    client_secret = new_id("secret", 24)
    clients[client_id] = {
        "name": reg.name,
        "client_secret": client_secret,
        "redirect_uris": reg.redirect_uris,
    }
    return {"client_id": client_id, "client_secret": client_secret}


@app.delete("/admin/clients/{client_id}")
def delete_client(client_id: str, request: Request):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    clients.pop(client_id, None)
    return {"ok": True}


@app.get("/.well-known/oauth-authorization-server")
def metadata(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "introspection_endpoint": f"{base}/introspect",
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
        "response_types_supported": ["code"],
    }


@app.get("/authorize", response_class=HTMLResponse)
def authorize(
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: Optional[str] = Query(None),
    code_challenge: Optional[str] = Query(None),
    code_challenge_method: Optional[str] = Query("S256"),
    response_type: str = Query("code"),
):
    client = clients.get(client_id)
    if not client:
        raise HTTPException(400, "unknown client_id")
    if client["redirect_uris"] and redirect_uri not in client["redirect_uris"]:
        raise HTTPException(400, "redirect_uri not registered for this client")

    approve_url = (
        f"/authorize/approve?client_id={client_id}&redirect_uri={redirect_uri}"
        f"&state={state or ''}&code_challenge={code_challenge or ''}"
        f"&code_challenge_method={code_challenge_method}"
    )
    return f"""
    <html><body style="font-family: sans-serif; max-width: 420px; margin: 80px auto;">
      <h2>sandbox-hub</h2>
      <p><b>{client['name']}</b> is requesting access to your test data.</p>
      <p>This is a local test IdP -- approving does not grant access to anything real.</p>
      <form action="{approve_url}" method="get">
        <button type="submit" style="padding:10px 20px;">Approve</button>
      </form>
    </body></html>
    """


@app.get("/authorize/approve")
def authorize_approve(
    client_id: str,
    redirect_uri: str,
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
):
    code = new_id("code", 16)
    auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge or None,
        "code_challenge_method": code_challenge_method,
        "expires_at": time.time() + AUTH_CODE_TTL_SECONDS,
    }
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(location)


def verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)


def issue_token(client_id: str, scope: str = "") -> dict:
    access_token = new_id("at", 24)
    tokens[access_token] = {
        "client_id": client_id,
        "scope": scope,
        "expires_at": time.time() + TOKEN_TTL_SECONDS,
    }
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "scope": scope,
    }


@app.post("/token")
def token(
    grant_type: str = Form(...),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    scope: Optional[str] = Form(""),
):
    if grant_type == "client_credentials":
        client = clients.get(client_id or "")
        if not client or not secrets.compare_digest(client["client_secret"], client_secret or ""):
            raise HTTPException(401, "invalid client credentials")
        return issue_token(client_id, scope or "")

    if grant_type == "authorization_code":
        entry = auth_codes.pop(code or "", None)
        if not entry or entry["expires_at"] < time.time():
            raise HTTPException(400, "invalid or expired code")
        if entry["client_id"] != client_id:
            raise HTTPException(400, "client_id mismatch")
        if entry["redirect_uri"] != redirect_uri:
            raise HTTPException(400, "redirect_uri mismatch")
        if entry.get("code_challenge"):
            if not code_verifier or not verify_pkce(code_verifier, entry["code_challenge"]):
                raise HTTPException(400, "invalid code_verifier")
        return issue_token(client_id, scope or "")

    raise HTTPException(400, "unsupported grant_type")


@app.post("/introspect")
def introspect(token: str = Form(...)):
    entry = tokens.get(token)
    if not entry or entry["expires_at"] < time.time():
        return {"active": False}
    return {
        "active": True,
        "client_id": entry["client_id"],
        "scope": entry["scope"],
        "exp": int(entry["expires_at"]),
    }


@app.get("/health")
def health():
    return {"status": "ok", "clients": len(clients), "live_tokens": len(tokens)}
