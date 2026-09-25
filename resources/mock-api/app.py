"""
User-defined routes: method + path (or "*" for "any"/catch-all) mapped to a
status code and a templated JSON response. Routes are managed live via an
admin API the hub calls on your behalf -- no restart needed to add, edit, or
remove one.

Paths can contain parameters as whole segments -- /users/{id} -- readable in
templates as {{request.params.id}}. When several routes match a request, the
most specific wins: a concrete path beats "*", more literal segments beat
fewer (/users/me beats /users/{id}), and a specific method beats "*"; among
equally specific routes the one added first wins.

Template placeholders (used inside string values of response_body):
  {{request.body.<dotted.path>}}   value from the parsed JSON request body
  {{request.query.<param>}}        a query string parameter
  {{request.params.<name>}}        a path parameter, e.g. {id} in /users/{id}
  {{request.headers.<name>}}       a request header (lowercase name)
  {{request.method}} {{request.path}}
  {{uuid}}                         a fresh random uuid4
  {{now}}                          current UTC timestamp, ISO 8601
A value that is *exactly* "{{expr}}" is replaced with the real JSON value
(so {{request.body.count}} stays a number); embedded inside a longer string
it's stringified.

A route can require certain fields be present in the JSON request body
(dotted paths) -- if missing, the mock responds 400 instead of the
configured response, letting you test client behavior against a schema you
define rather than a fixed sample one.

Every route can also inject latency (latency_ms) and random failures
(failure_rate, 0-1: that fraction of requests get a random 500/502/503/504),
same as the Chaos API kind but per route.

A route of type "crud" is a stateful in-memory collection instead of a
canned response: at /users it serves
  GET /users            list          POST /users        create (201)
  GET /users/{id}       fetch (404)   PUT /users/{id}    replace
  PATCH /users/{id}     merge         DELETE /users/{id} remove (204)
starting from its seed items, keyed by id_field. POST assigns the next
integer id (or a uuid, if existing ids aren't all integers) when the body
doesn't carry one. required_fields apply to POST and PUT.

Routes can also be generated from an OpenAPI 3.x document
(POST /_routes/import-openapi): one static route per operation, answering
with the operation's documented example, or a sample built from its response
schema when there's no example.

Auth (applies to the mock routes only, not /_routes/*, /health) reuses the
same AUTH_MODE options as the REST API resource: none, apikey, basic, jwt,
session, oauth.
"""
import asyncio
import base64
import copy
import json
import os
import random
import re
import secrets
import time
import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import httpx
import jwt as pyjwt
import yaml
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

AUTH_MODE = os.environ.get("AUTH_MODE", "none")
API_KEY = os.environ.get("API_KEY", "")
BASIC_USERNAME = os.environ.get("BASIC_USERNAME", "")
BASIC_PASSWORD = os.environ.get("BASIC_PASSWORD", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "86400"))
SESSION_USERNAME = os.environ.get("SESSION_USERNAME", "")
SESSION_PASSWORD = os.environ.get("SESSION_PASSWORD", "")
OAUTH_INTROSPECT_URL = os.environ.get("OAUTH_INTROSPECT_URL", "")
ADMIN_TOKEN = os.environ.get("MOCK_ADMIN_TOKEN", "dev-admin-token")

SESSION_COOKIE = "sandboxhub_session"
_active_sessions: set[str] = set()

app = FastAPI(title="sandbox-hub mock API")

routes: dict[str, dict] = {}
# route id -> the live items of a "crud" route, seeded from its seed list.
collections: dict[str, list[dict]] = {}
RESERVED_PATHS = {"/_routes", "/health", "/login", "/logout", "/_debug/token"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
PARAM_RE = re.compile(r"\{(\w+)\}")


class RouteIn(BaseModel):
    type: Literal["static", "crud"] = "static"
    method: str = "*"
    path: str = "*"
    status_code: int = Field(200, ge=100, le=599)
    response_body: Any = {"ok": True}
    required_fields: list[str] = []
    latency_ms: int = Field(0, ge=0, le=60000)
    failure_rate: float = Field(0.0, ge=0.0, le=1.0)
    seed: list[dict] = []
    id_field: str = "id"


def _mint_jwt() -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"sub": "sandbox-hub-tester", "iat": now, "exp": now + JWT_TTL_SECONDS},
        JWT_SECRET,
        algorithm="HS256",
    )


async def require_auth(request: Request):
    if AUTH_MODE == "none":
        return
    if AUTH_MODE == "apikey":
        if request.headers.get("x-api-key") != API_KEY:
            raise HTTPException(401, "missing or invalid X-API-Key header")
        return
    if AUTH_MODE == "basic":
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            raise HTTPException(401, "missing Basic auth", headers={"WWW-Authenticate": "Basic"})
        try:
            decoded = base64.b64decode(auth.removeprefix("Basic ").strip()).decode()
            user, _, pwd = decoded.partition(":")
        except Exception:
            raise HTTPException(401, "malformed Basic auth header", headers={"WWW-Authenticate": "Basic"})
        if not (secrets.compare_digest(user, BASIC_USERNAME) and secrets.compare_digest(pwd, BASIC_PASSWORD)):
            raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Basic"})
        return
    if AUTH_MODE == "jwt":
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "missing Bearer token")
        try:
            pyjwt.decode(auth.removeprefix("Bearer ").strip(), JWT_SECRET, algorithms=["HS256"])
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(401, "token expired")
        except pyjwt.InvalidTokenError as exc:
            raise HTTPException(401, f"invalid token: {exc}")
        return
    if AUTH_MODE == "session":
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id or session_id not in _active_sessions:
            raise HTTPException(401, "not logged in -- POST /login first")
        return
    if AUTH_MODE == "oauth":
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "missing Bearer token")
        token = auth.removeprefix("Bearer ").strip()
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                resp = await client.post(OAUTH_INTROSPECT_URL, data={"token": token})
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"could not reach oauth provider: {exc}")
        if not resp.json().get("active"):
            raise HTTPException(401, "token is not active")
        return


def require_admin(x_admin_token: str | None):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")


@app.get("/health")
def health():
    return {"status": "ok", "auth_mode": AUTH_MODE, "routes": len(routes)}


@app.post("/login", include_in_schema=False)
async def login(payload: dict):
    if AUTH_MODE != "session":
        raise HTTPException(404, "not found")
    if not (
        secrets.compare_digest(str(payload.get("username", "")), SESSION_USERNAME)
        and secrets.compare_digest(str(payload.get("password", "")), SESSION_PASSWORD)
    ):
        raise HTTPException(401, "invalid username/password")
    session_id = secrets.token_urlsafe(24)
    _active_sessions.add(session_id)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    return resp


@app.get("/_debug/token", include_in_schema=False)
def debug_token():
    if AUTH_MODE != "jwt":
        raise HTTPException(404, "not found")
    return {"token": _mint_jwt(), "expires_in": JWT_TTL_SECONDS}


def _segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg]


def _normalize_route(route: RouteIn) -> dict:
    """Validate a route and return its stored form. Raises HTTPException(400)."""
    method = route.method.upper()
    if method != "*" and method not in METHODS:
        raise HTTPException(400, f"unsupported method {route.method!r}")
    path = route.path.strip()
    if path != "*":
        if not path.startswith("/"):
            raise HTTPException(400, "path must be * or start with /")
        if path in RESERVED_PATHS or path.startswith("/_routes"):
            raise HTTPException(400, f"{path} is reserved by the mock server itself")
        names = []
        for seg in _segments(path):
            if "{" in seg or "}" in seg:
                m = PARAM_RE.fullmatch(seg)
                if not m:
                    raise HTTPException(400, f"path parameter must be a whole segment like {{id}}, got {seg!r}")
                names.append(m.group(1))
        if len(names) != len(set(names)):
            raise HTTPException(400, "duplicate path parameter name")
    entry = {**route.model_dump(), "method": method, "path": path}
    if route.type == "crud":
        if path == "*":
            raise HTTPException(400, "a crud route needs a concrete collection path like /users")
        if not route.id_field:
            raise HTTPException(400, "id_field must not be empty")
        # A collection answers every method on its own two paths.
        entry["method"] = "*"
    return entry


def _public(entry: dict) -> dict:
    if entry["type"] == "crud":
        return {**entry, "items": collections.get(entry["id"], [])}
    return entry


def _seed_collection(entry: dict):
    items = copy.deepcopy(entry["seed"])
    for item in items:
        if entry["id_field"] not in item:
            item[entry["id_field"]] = _next_id(items, entry["id_field"])
    collections[entry["id"]] = items


def _store(route_id: str, route: RouteIn) -> dict:
    entry = {"id": route_id, **_normalize_route(route)}
    routes[route_id] = entry
    collections.pop(route_id, None)
    if entry["type"] == "crud":
        _seed_collection(entry)
    return _public(entry)


@app.get("/_routes")
def list_routes():
    return [_public(r) for r in routes.values()]


@app.post("/_routes")
def create_route(route: RouteIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    return _store(secrets.token_hex(4), route)


@app.put("/_routes/{route_id}")
def update_route(route_id: str, route: RouteIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    if route_id not in routes:
        raise HTTPException(404, "unknown route")
    return _store(route_id, route)


@app.delete("/_routes/{route_id}")
def delete_route(route_id: str, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    routes.pop(route_id, None)
    collections.pop(route_id, None)
    return {"ok": True}


@app.delete("/_routes")
def clear_routes(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    routes.clear()
    collections.clear()
    return {"ok": True}


# ------------------------------------------------------------ OpenAPI import

OPENAPI_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")
SAMPLE_MAX_DEPTH = 8


class OpenApiImportIn(BaseModel):
    spec: Any = None  # a parsed document, or its JSON/YAML text
    url: Optional[str] = None  # ...or where to fetch it from
    headers: dict[str, str] = {}  # sent with the url fetch, e.g. a spec token
    replace: bool = False  # clear existing routes first


def _parse_spec_text(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"spec is neither valid JSON nor YAML: {exc}")


def _deref(spec: dict, obj, seen: frozenset = frozenset()):
    """Follow local $refs. Returns (resolved object, refs followed) -- the
    set is carried along so recursive schemas stop instead of looping."""
    while isinstance(obj, dict) and "$ref" in obj:
        ref = obj["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
            return {}, seen
        seen = seen | {ref}
        parts = [p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/")]
        obj = _dig(spec, parts)
    return obj, seen


def _sample(spec: dict, schema, depth: int = 0, seen: frozenset = frozenset()):
    """A plausible example value for a JSON schema."""
    schema, seen = _deref(spec, schema, seen)
    if not isinstance(schema, dict) or depth > SAMPLE_MAX_DEPTH:
        return None
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    if "allOf" in schema:
        merged = {}
        for part in schema["allOf"]:
            value = _sample(spec, part, depth + 1, seen)
            if isinstance(value, dict):
                merged.update(value)
        return merged
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            return _sample(spec, schema[key][0], depth + 1, seen)

    kind = schema.get("type")
    if isinstance(kind, list):  # 3.1 allows ["string", "null"]
        kind = next((k for k in kind if k != "null"), None)
    if kind is None:
        kind = "object" if "properties" in schema else "array" if "items" in schema else None
    if kind == "object":
        return {k: _sample(spec, v, depth + 1, seen) for k, v in (schema.get("properties") or {}).items()}
    if kind == "array":
        item = _sample(spec, schema.get("items", {}), depth + 1, seen)
        return [] if item is None else [item]
    if kind == "string":
        return {
            "uuid": "{{uuid}}",
            "date-time": "{{now}}",
            "date": "2024-01-01",
            "email": "user@example.com",
            "uri": "https://example.com",
            "url": "https://example.com",
        }.get(schema.get("format"), "string")
    if kind == "integer":
        return schema.get("minimum", 0)
    if kind == "number":
        return schema.get("minimum", 0)
    if kind == "boolean":
        return True
    return None


def _json_content(spec: dict, holder) -> Optional[dict]:
    holder, _ = _deref(spec, holder)
    content = (holder or {}).get("content") or {}
    for mime, media in content.items():
        if "json" in mime:
            return media
    return None


def _pick_response(spec: dict, responses: dict) -> tuple[int, Any]:
    codes = [c for c in responses if str(c).isdigit()]
    success = sorted(int(c) for c in codes if 200 <= int(c) < 300)
    if success:
        status, key = success[0], str(success[0])
    elif "2XX" in responses or "2xx" in responses:
        status, key = 200, "2XX" if "2XX" in responses else "2xx"
    elif "default" in responses:
        status, key = 200, "default"
    elif codes:
        status = min(int(c) for c in codes)
        key = str(status)
    else:
        return 200, {"ok": True}

    media = _json_content(spec, responses.get(key, responses.get(int(key)) if key.isdigit() else None))
    if media is None:
        return status, None
    if "example" in media:
        return status, media["example"]
    if isinstance(media.get("examples"), dict) and media["examples"]:
        example, _ = _deref(spec, next(iter(media["examples"].values())))
        if isinstance(example, dict) and "value" in example:
            return status, example["value"]
    return status, _sample(spec, media.get("schema", {}))


def _server_base_path(spec: dict) -> str:
    servers = spec.get("servers") or []
    if not servers or not isinstance(servers[0], dict):
        return ""
    url = servers[0].get("url", "")
    for name, var in (servers[0].get("variables") or {}).items():
        url = url.replace("{" + name + "}", str(var.get("default", "")))
    return urlparse(url).path.rstrip("/")


def _routes_from_openapi(spec: dict) -> tuple[list[RouteIn], list[dict], str]:
    if not isinstance(spec, dict):
        raise HTTPException(400, "spec must be a JSON/YAML object")
    if "swagger" in spec:
        raise HTTPException(400, "Swagger 2.0 isn't supported -- convert it to OpenAPI 3 first")
    if not str(spec.get("openapi", "")).startswith("3"):
        raise HTTPException(400, "not an OpenAPI 3.x document (no openapi: 3.x field)")

    base = _server_base_path(spec)
    created, skipped = [], []
    for raw_path, path_item in (spec.get("paths") or {}).items():
        path_item, _ = _deref(spec, path_item)
        for method in OPENAPI_METHODS:
            op = (path_item or {}).get(method)
            if not isinstance(op, dict):
                continue
            path = base + raw_path
            status, body = _pick_response(spec, op.get("responses") or {})
            # Echo path params back where the response has a same-named
            # string field, so GET /users/{id} answers with that id.
            if isinstance(body, dict):
                for name in PARAM_RE.findall(raw_path):
                    if isinstance(body.get(name), str):
                        body[name] = "{{request.params." + name + "}}"
            required = []
            media = _json_content(spec, op.get("requestBody"))
            if media:
                req_schema, _ = _deref(spec, media.get("schema", {}))
                required = [f for f in (req_schema or {}).get("required", []) if isinstance(f, str)]
            route = RouteIn(
                method=method.upper(), path=path, status_code=status,
                response_body=body, required_fields=required,
            )
            try:
                _normalize_route(route)
            except HTTPException as exc:
                skipped.append({"method": method.upper(), "path": path, "reason": exc.detail})
                continue
            created.append(route)
    return created, skipped, base


@app.post("/_routes/import-openapi")
async def import_openapi(body: OpenApiImportIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    if (body.spec is None) == (body.url is None):
        raise HTTPException(400, "give exactly one of spec or url")
    spec = body.spec
    if body.url is not None:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            try:
                resp = await client.get(body.url, headers=body.headers)
            except httpx.HTTPError as exc:
                raise HTTPException(400, f"could not fetch {body.url}: {exc}")
        if resp.status_code >= 400:
            raise HTTPException(400, f"fetching {body.url} returned {resp.status_code}")
        spec = resp.text
    if isinstance(spec, str):
        spec = _parse_spec_text(spec)

    new_routes, skipped, base = _routes_from_openapi(spec)
    # Parse everything before touching the live routes, so a bad spec
    # leaves the instance exactly as it was.
    if body.replace:
        routes.clear()
        collections.clear()
    created = [_store(secrets.token_hex(4), r) for r in new_routes]
    return {"created": created, "skipped": skipped, "base_path": base}


TOKEN_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _dig(obj, parts):
    cur = obj
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _resolve_token(expr: str, ctx: dict):
    if expr == "uuid":
        return str(uuidlib.uuid4())
    if expr == "now":
        return datetime.now(timezone.utc).isoformat()
    if expr == "request.method":
        return ctx["method"]
    if expr == "request.path":
        return ctx["path"]
    parts = expr.split(".")
    if parts[0] == "request" and len(parts) >= 2:
        root = ctx.get(parts[1])
        return _dig(root, parts[2:]) if len(parts) > 2 else root
    return None


def render_template(value, ctx):
    if isinstance(value, str):
        full = TOKEN_RE.fullmatch(value.strip())
        if full:
            return _resolve_token(full.group(1), ctx)

        def _sub(m):
            v = _resolve_token(m.group(1), ctx)
            if v is None:
                return ""
            return v if isinstance(v, str) else json.dumps(v)

        return TOKEN_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: render_template(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, ctx) for v in value]
    return value


def _match_segments(pattern: list[str], segs: list[str]) -> Optional[tuple[dict, int]]:
    """Match path segments against a route pattern. Returns (params,
    number of literal segments) or None."""
    if len(pattern) != len(segs):
        return None
    params, literals = {}, 0
    for pat, seg in zip(pattern, segs):
        m = PARAM_RE.fullmatch(pat)
        if m:
            params[m.group(1)] = seg
        elif pat == seg:
            literals += 1
        else:
            return None
    return params, literals


def _find_route(method: str, path: str):
    """Pick the most specific matching route (see module docstring).
    Returns (route, path params, item id for a crud item path) or None."""
    segs = _segments(path)
    best, best_rank = None, None
    for r in routes.values():
        if r["path"] == "*":
            params, literals, item_id, concrete = {}, 0, None, False
        else:
            pattern = _segments(r["path"])
            matched = _match_segments(pattern, segs)
            item_id = None
            if matched is None and r["type"] == "crud" and segs:
                matched = _match_segments(pattern, segs[:-1])
                item_id = segs[-1]
            if matched is None:
                continue
            (params, literals), concrete = matched, True
        if r["method"] != "*" and r["method"] != method.upper():
            continue
        rank = (concrete, literals, r["method"] != "*")
        # Strictly greater, so among equals the earliest-added route wins.
        if best_rank is None or rank > best_rank:
            best, best_rank = (r, params, item_id), rank
    return best


def _next_id(items: list[dict], id_field: str):
    ids = [item[id_field] for item in items if id_field in item]
    if all(isinstance(x, int) and not isinstance(x, bool) for x in ids):
        return max(ids, default=0) + 1
    return str(uuidlib.uuid4())


def _missing_fields(route: dict, body) -> list[str]:
    return [f for f in route["required_fields"] if _dig(body, f.split(".")) is None]


def _crud(route: dict, method: str, item_id: Optional[str], body):
    items = collections.setdefault(route["id"], [])
    id_field = route["id_field"]
    if item_id is None and method not in ("GET", "POST"):
        return JSONResponse({"error": f"{method} not allowed on a collection"}, status_code=405)
    if item_id is not None and method not in ("GET", "PUT", "PATCH", "DELETE"):
        hint = " -- POST to the collection" if method == "POST" else ""
        return JSONResponse({"error": f"{method} not allowed on an item{hint}"}, status_code=405)
    if method in ("POST", "PUT", "PATCH") and not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    # required_fields describe a whole item, so a partial PATCH is exempt.
    missing = _missing_fields(route, body) if method in ("POST", "PUT") else []
    if missing:
        return JSONResponse({"error": "missing required field(s)", "fields": missing}, status_code=400)

    if item_id is None:
        if method == "GET":
            return JSONResponse(items)
        if method == "POST":
            item = dict(body)
            if id_field not in item:
                item[id_field] = _next_id(items, id_field)
            elif any(str(i.get(id_field)) == str(item[id_field]) for i in items):
                return JSONResponse({"error": f"{id_field} {item[id_field]!r} already exists"}, status_code=409)
            items.append(item)
            return JSONResponse(item, status_code=201)

    index = next((n for n, i in enumerate(items) if str(i.get(id_field)) == item_id), None)
    if index is None:
        return JSONResponse({"error": "not found", id_field: item_id}, status_code=404)
    if method == "GET":
        return JSONResponse(items[index])
    if method == "DELETE":
        items.pop(index)
        return Response(status_code=204)
    # The id comes from the path; a body can't move an item to another id.
    current_id = items[index][id_field]
    updated = dict(body) if method == "PUT" else {**items[index], **body}
    updated[id_field] = current_id
    items[index] = updated
    return JSONResponse(updated)


@app.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def mock_dispatch(request: Request, full_path: str = ""):
    path = "/" + full_path
    if path in RESERVED_PATHS:
        raise HTTPException(404, "not found")

    await require_auth(request)

    raw = await request.body()
    try:
        body_json = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body_json = {}

    match = _find_route(request.method, path)
    if match is None:
        return JSONResponse(
            {"error": "no matching mock route", "method": request.method, "path": path}, status_code=404
        )
    route, params, item_id = match

    if route["latency_ms"] > 0:
        await asyncio.sleep(route["latency_ms"] / 1000)
    if route["failure_rate"] > 0 and random.random() < route["failure_rate"]:
        status = random.choice([500, 502, 503, 504])
        return JSONResponse({"error": "injected_failure", "status": status}, status_code=status)

    if route["type"] == "crud":
        return _crud(route, request.method.upper(), item_id, body_json)

    missing = _missing_fields(route, body_json)
    if missing:
        return JSONResponse({"error": "missing required field(s)", "fields": missing}, status_code=400)

    ctx = {
        "method": request.method,
        "path": path,
        "query": dict(request.query_params),
        "headers": {k.lower(): v for k, v in request.headers.items()},
        "params": params,
        "body": body_json,
    }
    # These statuses can't carry a body; sending one breaks the response.
    if route["status_code"] in (204, 304) or 100 <= route["status_code"] < 200:
        return Response(status_code=route["status_code"])
    rendered = render_template(route["response_body"], ctx)
    return JSONResponse(rendered, status_code=route["status_code"])
