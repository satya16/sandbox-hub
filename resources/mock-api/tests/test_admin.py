"""
X-Admin-Token gating on /_routes* -- write operations require it, but
GET /_routes (read-only, used by the hub to display current state) does not.
"""
from fastapi.testclient import TestClient

ADMIN = {"X-Admin-Token": "dev-admin-token"}
WRONG = {"X-Admin-Token": "not-the-token"}


def test_create_route_requires_admin_token(app_module):
    client = TestClient(app_module.app)
    body = {"path": "/x", "response_body": {"ok": True}}
    assert client.post("/_routes", json=body).status_code == 403
    assert client.post("/_routes", json=body, headers=WRONG).status_code == 403
    assert client.post("/_routes", json=body, headers=ADMIN).status_code == 200


def test_update_route_requires_admin_token(app_module):
    client = TestClient(app_module.app)
    created = client.post("/_routes", json={"path": "/x"}, headers=ADMIN).json()
    body = {"path": "/y", "response_body": {"ok": True}}
    assert client.put(f"/_routes/{created['id']}", json=body).status_code == 403
    assert client.put(f"/_routes/{created['id']}", json=body, headers=ADMIN).status_code == 200


def test_delete_route_requires_admin_token(app_module):
    client = TestClient(app_module.app)
    created = client.post("/_routes", json={"path": "/x"}, headers=ADMIN).json()
    assert client.delete(f"/_routes/{created['id']}").status_code == 403
    assert client.delete(f"/_routes/{created['id']}", headers=ADMIN).status_code == 200


def test_clear_routes_requires_admin_token(app_module):
    client = TestClient(app_module.app)
    client.post("/_routes", json={"path": "/x"}, headers=ADMIN)
    assert client.delete("/_routes").status_code == 403
    assert client.delete("/_routes", headers=ADMIN).status_code == 200


def test_list_routes_needs_no_admin_token(app_module):
    client = TestClient(app_module.app)
    client.post("/_routes", json={"path": "/x"}, headers=ADMIN)
    resp = client.get("/_routes")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
