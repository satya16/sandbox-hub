"""
Schema management: GET/PUT /_schema, and the seeded default schema every
fresh instance starts with.
"""
from fastapi.testclient import TestClient

ADMIN_HEADERS = {"X-Admin-Token": "dev-admin-token"}


def test_default_schema_and_resolvers_work_out_of_the_box(app_module):
    c = TestClient(app_module.app)
    r = c.post("/graphql", json={"query": "{ items { id name } }"})
    assert r.json() == {"data": {"items": [{"id": "1", "name": "widget"}, {"id": "2", "name": "gadget"}]}}

    r = c.post("/graphql", json={"query": '{ item(id: "1") { name } }'})
    assert r.json() == {"data": {"item": {"name": "sample-item"}}}

    r = c.post("/graphql", json={"query": 'mutation { addItem(name: "x") { name } }'})
    assert r.json() == {"data": {"addItem": {"name": "x"}}}


def test_get_schema_and_resolvers(app_module):
    c = TestClient(app_module.app)
    assert "type Item" in c.get("/_schema").json()["sdl"]
    fields = {r["field"] for r in c.get("/_resolvers").json()}
    assert fields == {"items", "item", "addItem"}


def test_put_schema_replaces_sdl(app_module):
    c = TestClient(app_module.app)
    new_sdl = "type Query { hi: String }\n"
    r = c.put("/_schema", json={"sdl": new_sdl}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert c.get("/_schema").json()["sdl"] == new_sdl


def test_put_schema_invalid_sdl(app_module):
    c = TestClient(app_module.app)
    r = c.put("/_schema", json={"sdl": "not valid sdl {{{"}, headers=ADMIN_HEADERS)
    assert r.status_code == 400
    assert "invalid SDL" in r.json()["detail"]


def test_put_schema_requires_admin_token(app_module):
    c = TestClient(app_module.app)
    assert c.put("/_schema", json={"sdl": "type Query { hi: String }"}).status_code == 403
    assert c.put(
        "/_schema", json={"sdl": "type Query { hi: String }"}, headers={"X-Admin-Token": "wrong"}
    ).status_code == 403


def test_put_schema_drops_stale_resolvers_keeps_others(app_module):
    c = TestClient(app_module.app)
    # New schema drops "item" and "addItem" but keeps a same-named "items".
    new_sdl = "type Item { id: ID! name: String! }\ntype Query { items: [Item!]! }\n"
    r = c.put("/_schema", json={"sdl": new_sdl}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    removed = set(r.json()["removed_resolvers"])
    assert removed == {"Query.item", "Mutation.addItem"}

    fields = {res["field"] for res in c.get("/_resolvers").json()}
    assert fields == {"items"}
