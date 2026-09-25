"""
A GraphQL sandbox: you supply the schema (SDL) and, per root field, a
templated mock response -- no fixed schema baked in, same philosophy as the
Mock API kind but for GraphQL instead of REST routes.

Schema and resolvers are managed live via an admin API the hub calls on your
behalf (PUT/GET /_schema, GET/POST/DELETE /_resolvers) -- no restart needed
to change either. A freshly-created instance starts with a small seeded
Item/Query/Mutation schema and matching resolvers, so it's queryable right
away.

Only root Query/Mutation fields need a registered resolver -- nested object
fields resolve for free from whatever JSON-like structure the root resolver
returns (graphql-core's default resolver does dict-key lookup), exactly like
Mock API only needing you to define the routes you care about.

Template placeholders (used inside string values of response_body):
  {{request.args.<name>}}       a resolved GraphQL field argument
  {{request.variables.<name>}}  a query variable
  {{uuid}}                      a fresh random uuid4
  {{now}}                       current UTC timestamp, ISO 8601
A value that is *exactly* "{{expr}}" is replaced with the real value (so
{{request.args.count}} stays a number); embedded inside a longer string it's
stringified.

A resolver can instead be configured to always raise a GraphQL error
(`error: {message, extensions}`) -- lets you test a client's error-handling
path without a real backend failure. A field with no registered resolver
raises "no mock resolver defined for Type.field" rather than a 500.

Because this always speaks real GraphQL (a schema built from your SDL via
graphql-core, not a stand-in), introspection just works -- point a codegen
tool or GraphiQL (served at /graphiql) at it like a real endpoint.

Auth (applies to /graphql only, not /graphiql, /_schema, /_resolvers,
/health) reuses the same AUTH_MODE options as the other kinds: none, apikey,
basic, jwt, session, oauth.

Known simplification: every /graphql response is HTTP 200, with `errors`
included whenever the query fails to parse, fails validation, or a resolver
raises -- some servers distinguish request-level failures with a 4xx. Fine
for testing a client's GraphQL-level error handling; not for testing its
HTTP-level error handling on malformed requests.
"""
import base64
import json
import os
import re
import secrets
import time
import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import jwt as pyjwt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from graphql import GraphQLError, build_schema, default_field_resolver, graphql
from pydantic import BaseModel

AUTH_MODE = os.environ.get("AUTH_MODE", "none")
API_KEY = os.environ.get("API_KEY", "")
BASIC_USERNAME = os.environ.get("BASIC_USERNAME", "")
BASIC_PASSWORD = os.environ.get("BASIC_PASSWORD", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "86400"))
SESSION_USERNAME = os.environ.get("SESSION_USERNAME", "")
SESSION_PASSWORD = os.environ.get("SESSION_PASSWORD", "")
OAUTH_INTROSPECT_URL = os.environ.get("OAUTH_INTROSPECT_URL", "")
ADMIN_TOKEN = os.environ.get("GRAPHQL_ADMIN_TOKEN", "dev-admin-token")

SESSION_COOKIE = "sandboxhub_session"
_active_sessions: set[str] = set()

app = FastAPI(title="sandbox-hub GraphQL API")

DEFAULT_SDL = """type Item {
  id: ID!
  name: String!
}

type Query {
  items: [Item!]!
  item(id: ID!): Item
}

type Mutation {
  addItem(name: String!): Item!
}
"""

DEFAULT_RESOLVERS: dict[str, dict] = {
    "Query.items": {
        "type": "Query",
        "field": "items",
        "response_body": [
            {"id": "1", "name": "widget"},
            {"id": "2", "name": "gadget"},
        ],
        "error": None,
    },
    "Query.item": {
        "type": "Query",
        "field": "item",
        "response_body": {"id": "{{request.args.id}}", "name": "sample-item"},
        "error": None,
    },
    "Mutation.addItem": {
        "type": "Mutation",
        "field": "addItem",
        "response_body": {"id": "{{uuid}}", "name": "{{request.args.name}}"},
        "error": None,
    },
}

_state: dict[str, Any] = {"sdl": DEFAULT_SDL, "schema": build_schema(DEFAULT_SDL)}
resolvers: dict[str, dict] = {k: dict(v) for k, v in DEFAULT_RESOLVERS.items()}


class ErrorSpec(BaseModel):
    message: str
    extensions: Optional[dict] = None


class ResolverIn(BaseModel):
    type: str
    field: str
    response_body: Any = None
    error: Optional[ErrorSpec] = None


class SchemaIn(BaseModel):
    sdl: str


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
    return {"status": "ok", "auth_mode": AUTH_MODE, "resolvers": len(resolvers)}


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


# ------------------------------------------------------------------- schema


@app.get("/_schema")
def get_schema():
    return {"sdl": _state["sdl"]}


@app.put("/_schema")
def set_schema(body: SchemaIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    try:
        new_schema = build_schema(body.sdl)
    except GraphQLError as exc:
        raise HTTPException(400, f"invalid SDL: {exc}")
    _state["sdl"] = body.sdl
    _state["schema"] = new_schema
    # Drop resolvers for root fields the new schema no longer has -- they
    # can never be called, and would otherwise linger in the list forever.
    stale = [key for key, r in resolvers.items() if not _field_exists(r["type"], r["field"])]
    for key in stale:
        del resolvers[key]
    return {"sdl": _state["sdl"], "removed_resolvers": stale}


def _root_type_names() -> set[str]:
    schema = _state["schema"]
    return {t.name for t in (schema.query_type, schema.mutation_type, schema.subscription_type) if t}


# Resolvers can only be registered for Query/Mutation -- there's no
# WebSocket transport here to actually deliver a Subscription, so accepting
# one at registration time would just be a silent dead end.
RESOLVABLE_ROOT_TYPES = {"Query", "Mutation"}


def _field_exists(type_name: str, field_name: str) -> bool:
    if type_name not in _root_type_names() or type_name not in RESOLVABLE_ROOT_TYPES:
        return False
    schema_type = _state["schema"].type_map.get(type_name)
    return schema_type is not None and field_name in getattr(schema_type, "fields", {})


def _validate_type_field(type_name: str, field_name: str):
    resolvable = {n for n in _root_type_names() if n in RESOLVABLE_ROOT_TYPES}
    if type_name not in resolvable:
        raise HTTPException(400, f"{type_name!r} is not Query or Mutation in the current schema")
    if not _field_exists(type_name, field_name):
        raise HTTPException(400, f"no field {type_name}.{field_name} in the current schema")


# ---------------------------------------------------------------- resolvers


@app.get("/_resolvers")
def list_resolvers():
    return list(resolvers.values())


@app.post("/_resolvers")
def create_resolver(body: ResolverIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    _validate_type_field(body.type, body.field)
    key = f"{body.type}.{body.field}"
    entry = {
        "type": body.type,
        "field": body.field,
        "response_body": body.response_body,
        "error": body.error.model_dump() if body.error else None,
    }
    resolvers[key] = entry
    return entry


@app.put("/_resolvers/{type_name}/{field_name}")
def update_resolver(type_name: str, field_name: str, body: ResolverIn, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    _validate_type_field(type_name, field_name)
    key = f"{type_name}.{field_name}"
    entry = {
        "type": type_name,
        "field": field_name,
        "response_body": body.response_body,
        "error": body.error.model_dump() if body.error else None,
    }
    resolvers[key] = entry
    return entry


@app.delete("/_resolvers/{type_name}/{field_name}")
def delete_resolver(type_name: str, field_name: str, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    resolvers.pop(f"{type_name}.{field_name}", None)
    return {"ok": True}


@app.delete("/_resolvers")
def clear_resolvers(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    resolvers.clear()
    return {"ok": True}


# -------------------------------------------------------------- templating


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
    parts = expr.split(".")
    if parts[0] == "request" and len(parts) >= 3 and parts[1] in ("args", "variables"):
        return _dig(ctx[parts[1]], parts[2:])
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


def _mock_field_resolver(source, info, **args):
    type_name = info.parent_type.name
    field_name = info.field_name
    if type_name not in _root_type_names():
        return default_field_resolver(source, info, **args)

    entry = resolvers.get(f"{type_name}.{field_name}")
    if entry is None:
        raise GraphQLError(f"no mock resolver defined for {type_name}.{field_name}")
    if entry.get("error"):
        err = entry["error"]
        raise GraphQLError(err.get("message", "mocked error"), extensions=err.get("extensions"))

    ctx = {"args": args, "variables": dict(info.variable_values or {})}
    return render_template(entry["response_body"], ctx)


# --------------------------------------------------------------- execution


class GraphQLRequest(BaseModel):
    query: str
    variables: Optional[dict] = None
    operationName: Optional[str] = None


@app.post("/graphql")
async def graphql_endpoint(body: GraphQLRequest, request: Request):
    await require_auth(request)
    result = await graphql(
        _state["schema"],
        source=body.query,
        variable_values=body.variables,
        operation_name=body.operationName,
        field_resolver=_mock_field_resolver,
    )
    payload: dict[str, Any] = {}
    if result.data is not None:
        payload["data"] = result.data
    if result.errors:
        payload["errors"] = [e.formatted for e in result.errors]
    return JSONResponse(payload)


# ----------------------------------------------------------------- graphiql


_GRAPHIQL_HTML = """<!DOCTYPE html>
<html>
  <head>
    <title>sandbox-hub GraphQL API - GraphiQL</title>
    <style>body { margin: 0; height: 100vh; }</style>
    <link rel="stylesheet" href="https://unpkg.com/graphiql/graphiql.min.css" />
  </head>
  <body>
    <div id="graphiql" style="height: 100vh;"></div>
    <script src="https://unpkg.com/react/umd/react.production.min.js"></script>
    <script src="https://unpkg.com/react-dom/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/graphiql/graphiql.min.js"></script>
    <script>
      const fetcher = GraphiQL.createFetcher({ url: '/graphql' });
      ReactDOM.render(
        React.createElement(GraphiQL, { fetcher: fetcher }),
        document.getElementById('graphiql'),
      );
    </script>
  </body>
</html>"""


@app.get("/graphiql", include_in_schema=False)
def graphiql_page():
    return HTMLResponse(_GRAPHIQL_HTML)
