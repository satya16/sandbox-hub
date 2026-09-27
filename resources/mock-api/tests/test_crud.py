"""
"crud" routes: a stateful in-memory collection at /path (list/create) and
/path/{id} (get/replace/merge/delete), per app.py's module docstring.
"""
from fastapi.testclient import TestClient

ADMIN = {"X-Admin-Token": "dev-admin-token"}


def _add_crud(client, path="/items", **kwargs):
    body = {"type": "crud", "path": path, "seed": [], "id_field": "id", **kwargs}
    resp = client.post("/_routes", json=body, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_crud_requires_concrete_path(app_module):
    client = TestClient(app_module.app)
    resp = client.post("/_routes", json={"type": "crud", "path": "*"}, headers=ADMIN)
    assert resp.status_code == 400


def test_crud_requires_nonempty_id_field(app_module):
    client = TestClient(app_module.app)
    resp = client.post("/_routes", json={"type": "crud", "path": "/x", "id_field": ""}, headers=ADMIN)
    assert resp.status_code == 400


def test_seed_items_missing_id_field_get_one_assigned(app_module):
    client = TestClient(app_module.app)
    # _next_id looks at every seed item's id (not just ones already
    # processed), so the assigned id accounts for the *whole* seed list --
    # here max(5) + 1, not 1.
    created = _add_crud(client, seed=[{"name": "a"}, {"id": 5, "name": "b"}])
    assert created["items"] == [{"name": "a", "id": 6}, {"id": 5, "name": "b"}]


def test_get_collection_lists_items(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a"}])
    assert client.get("/items").json() == [{"id": 1, "name": "a"}]


def test_post_creates_with_next_integer_id(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a"}])
    resp = client.post("/items", json={"name": "b"})
    assert resp.status_code == 201
    assert resp.json() == {"id": 2, "name": "b"}


def test_post_creates_with_uuid_when_ids_not_all_int(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": "abc", "name": "a"}])
    resp = client.post("/items", json={"name": "b"})
    assert resp.status_code == 201
    body = resp.json()
    assert isinstance(body["id"], str) and len(body["id"]) == 36


def test_post_duplicate_id_conflicts(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a"}])
    resp = client.post("/items", json={"id": 1, "name": "dupe"})
    assert resp.status_code == 409


def test_post_non_dict_body_rejected(app_module):
    client = TestClient(app_module.app)
    _add_crud(client)
    resp = client.post("/items", json=[1, 2, 3])
    assert resp.status_code == 400


def test_get_item_by_id_and_404(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a"}])
    resp = client.get("/items/1")
    assert resp.status_code == 200
    assert resp.json() == {"id": 1, "name": "a"}

    resp = client.get("/items/999")
    assert resp.status_code == 404
    assert resp.json()["id"] == "999"


def test_put_replaces_and_cannot_move_id(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a", "extra": "x"}])
    resp = client.put("/items/1", json={"id": 999, "name": "b"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert body["name"] == "b"
    assert "extra" not in body


def test_patch_merges_partial_and_is_exempt_from_required_fields(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a", "extra": "x"}], required_fields=["name", "extra"])
    resp = client.patch("/items/1", json={"name": "b"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 1, "name": "b", "extra": "x"}


def test_delete_item_then_404(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a"}])
    resp = client.delete("/items/1")
    assert resp.status_code == 204
    assert client.get("/items/1").status_code == 404


def test_method_not_allowed_on_collection(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a"}])
    for method in ("PUT", "PATCH", "DELETE"):
        resp = client.request(method, "/items", json={"name": "x"})
        assert resp.status_code == 405
        assert "collection" in resp.json()["error"]


def test_method_not_allowed_on_item_hints_post_to_collection(app_module):
    client = TestClient(app_module.app)
    _add_crud(client, seed=[{"id": 1, "name": "a"}])
    resp = client.post("/items/1", json={"name": "x"})
    assert resp.status_code == 405
    assert "POST to the collection" in resp.json()["error"]


def test_delete_route_also_clears_collection_state(app_module):
    client = TestClient(app_module.app)
    created = _add_crud(client, seed=[{"id": 1, "name": "a"}])
    route_id = created["id"]
    resp = client.delete(f"/_routes/{route_id}", headers=ADMIN)
    assert resp.status_code == 200
    assert app_module.collections.get(route_id) is None


def test_clear_all_routes_also_clears_collections(app_module):
    client = TestClient(app_module.app)
    created = _add_crud(client, seed=[{"id": 1, "name": "a"}])
    route_id = created["id"]
    resp = client.delete("/_routes", headers=ADMIN)
    assert resp.status_code == 200
    assert app_module.collections == {}
    assert app_module.routes == {}
