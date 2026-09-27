"""
/graphql execution: template rendering in mock resolvers, the always-200
"known simplification" for parse/validation/resolver errors, nested-field
resolution for free, and /graphiql.
"""
import re

from fastapi.testclient import TestClient

ADMIN_HEADERS = {"X-Admin-Token": "dev-admin-token"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_template_args_and_exact_token_preserves_type(app_module):
    c = TestClient(app_module.app)
    c.put(
        "/_schema",
        json={"sdl": "type Query { echoCount(count: Int!): Int }"},
        headers=ADMIN_HEADERS,
    )
    c.post(
        "/_resolvers",
        json={"type": "Query", "field": "echoCount", "response_body": "{{request.args.count}}"},
        headers=ADMIN_HEADERS,
    )
    r = c.post("/graphql", json={"query": "{ echoCount(count: 5) }"}).json()
    assert r == {"data": {"echoCount": 5}}


def test_template_embedded_string_stringifies(app_module):
    c = TestClient(app_module.app)
    c.put("/_schema", json={"sdl": "type Query { greet(name: String!): String }"}, headers=ADMIN_HEADERS)
    c.post(
        "/_resolvers",
        json={"type": "Query", "field": "greet", "response_body": "hello {{request.args.name}}!"},
        headers=ADMIN_HEADERS,
    )
    r = c.post("/graphql", json={"query": '{ greet(name: "world") }'}).json()
    assert r == {"data": {"greet": "hello world!"}}


def test_template_variables_uuid_now(app_module):
    c = TestClient(app_module.app)
    c.put(
        "/_schema",
        json={"sdl": "type Query { thing(x: String): String }"},
        headers=ADMIN_HEADERS,
    )
    c.post(
        "/_resolvers",
        json={
            "type": "Query",
            "field": "thing",
            "response_body": "{{request.variables.x}}|{{uuid}}|{{now}}",
        },
        headers=ADMIN_HEADERS,
    )
    r = c.post(
        "/graphql",
        json={"query": "query($x: String) { thing(x: $x) }", "variables": {"x": "v"}},
    ).json()
    value = r["data"]["thing"]
    v, u, now = value.split("|")
    assert v == "v"
    assert UUID_RE.match(u)
    assert "T" in now


def test_missing_resolver_returns_error_not_exception(app_module):
    c = TestClient(app_module.app)
    c.put(
        "/_schema",
        json={"sdl": "type Item { id: ID! name: String! } type Query { items: [Item!]! unmocked: String }"},
        headers=ADMIN_HEADERS,
    )
    # "items" already has a resolver seeded from before the schema swap? No --
    # a schema replace drops resolvers for fields no longer valid, but here
    # "items" is still Query.items so its old resolver survives.
    r = c.post("/graphql", json={"query": "{ unmocked }"}).json()
    assert r["data"] is None or r["data"].get("unmocked") is None
    assert "no mock resolver defined for Query.unmocked" in r["errors"][0]["message"]


def test_malformed_and_invalid_queries_are_still_http_200(app_module):
    c = TestClient(app_module.app)
    r = c.post("/graphql", json={"query": "{ items { "})
    assert r.status_code == 200
    assert "errors" in r.json()

    r = c.post("/graphql", json={"query": "{ thisFieldDoesNotExist }"})
    assert r.status_code == 200
    assert "errors" in r.json()


def test_nested_object_fields_resolve_for_free(app_module):
    c = TestClient(app_module.app)
    r = c.post("/graphql", json={"query": '{ item(id: "42") { id name } }'}).json()
    assert r["data"]["item"] == {"id": "42", "name": "sample-item"}


def test_graphiql_page(app_module):
    c = TestClient(app_module.app)
    r = c.get("/graphiql")
    assert r.status_code == 200
    assert "graphiql" in r.text.lower()
