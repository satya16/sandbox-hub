"""
Resolver management: POST/PUT/DELETE /_resolvers, and validation against the
current schema (only Query/Mutation root fields are resolvable).
"""
from fastapi.testclient import TestClient

ADMIN_HEADERS = {"X-Admin-Token": "dev-admin-token"}


def test_register_resolver_for_non_root_type_rejected(app_module):
    c = TestClient(app_module.app)
    r = c.post(
        "/_resolvers",
        json={"type": "Item", "field": "name", "response_body": "x"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400
    assert "not Query or Mutation" in r.json()["detail"]


def test_register_resolver_for_subscription_rejected(app_module):
    c = TestClient(app_module.app)
    c.put(
        "/_schema",
        json={"sdl": "type Query { hi: String } type Subscription { ticked: String }"},
        headers=ADMIN_HEADERS,
    )
    r = c.post(
        "/_resolvers",
        json={"type": "Subscription", "field": "ticked", "response_body": "x"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400


def test_register_resolver_for_unknown_field_rejected(app_module):
    c = TestClient(app_module.app)
    r = c.post(
        "/_resolvers",
        json={"type": "Query", "field": "doesNotExist", "response_body": "x"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 400
    assert "no field" in r.json()["detail"]


def test_post_creates_put_updates(app_module):
    c = TestClient(app_module.app)
    c.put(
        "/_schema", json={"sdl": "type Query { hi(name: String): String }"}, headers=ADMIN_HEADERS
    )
    created = c.post(
        "/_resolvers",
        json={"type": "Query", "field": "hi", "response_body": "hello"},
        headers=ADMIN_HEADERS,
    ).json()
    assert created["response_body"] == "hello"

    updated = c.put(
        "/_resolvers/Query/hi",
        json={"type": "Query", "field": "hi", "response_body": "howdy"},
        headers=ADMIN_HEADERS,
    ).json()
    assert updated["response_body"] == "howdy"

    q = c.post("/graphql", json={"query": "{ hi }"}).json()
    assert q == {"data": {"hi": "howdy"}}


def test_delete_one_and_clear_all_require_admin_token(app_module):
    c = TestClient(app_module.app)
    assert c.delete("/_resolvers/Query/items").status_code == 403
    assert c.delete("/_resolvers/Query/items", headers=ADMIN_HEADERS).status_code == 200
    assert {r["field"] for r in c.get("/_resolvers").json()} == {"item", "addItem"}

    assert c.delete("/_resolvers").status_code == 403
    assert c.delete("/_resolvers", headers=ADMIN_HEADERS).status_code == 200
    assert c.get("/_resolvers").json() == []


def test_resolver_error_spec_distinct_from_response_body(app_module):
    c = TestClient(app_module.app)
    r = c.post(
        "/_resolvers",
        json={
            "type": "Query",
            "field": "items",
            "response_body": None,
            "error": {"message": "boom", "extensions": {"code": "BOOM"}},
        },
        headers=ADMIN_HEADERS,
    ).json()
    assert r["response_body"] is None
    assert r["error"] == {"message": "boom", "extensions": {"code": "BOOM"}}

    result = c.post("/graphql", json={"query": "{ items { id } }"}).json()
    assert result["errors"][0]["message"] == "boom"
    assert result["errors"][0]["extensions"] == {"code": "BOOM"}
