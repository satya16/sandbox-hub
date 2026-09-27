"""
POST /_routes/import-openapi -- generates static routes from an OpenAPI 3.x
document: one per operation, answering with its documented example or a
sample built from its response schema. See app.py's _routes_from_openapi,
_sample, _pick_response, _deref.
"""
from fastapi.testclient import TestClient

ADMIN = {"X-Admin-Token": "dev-admin-token"}


def _import(client, **kwargs):
    return client.post("/_routes/import-openapi", json=kwargs, headers=ADMIN)


def test_neither_or_both_spec_and_url_rejected(app_module):
    client = TestClient(app_module.app)
    assert _import(client).status_code == 400
    assert _import(client, spec={"openapi": "3.0.3", "paths": {}}, url="http://x").status_code == 400


def test_explicit_example_used_verbatim(app_module):
    client = TestClient(app_module.app)
    spec = {
        "openapi": "3.0.3",
        "paths": {
            "/things": {
                "get": {
                    "responses": {
                        "200": {"description": "ok", "content": {"application/json": {"example": [{"id": 1}]}}}
                    }
                }
            }
        },
    }
    resp = _import(client, spec=spec)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["created"]) == 1
    assert body["skipped"] == []
    assert client.get("/things").json() == [{"id": 1}]


def test_sample_generated_from_schema_by_type(app_module):
    client = TestClient(app_module.app)
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/widget": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string", "format": "uuid"},
                                            "created": {"type": "string", "format": "date-time"},
                                            "email": {"type": "string", "format": "email"},
                                            "site": {"type": "string", "format": "url"},
                                            "count": {"type": "integer", "minimum": 3},
                                            "price": {"type": "number", "minimum": 1.5},
                                            "active": {"type": "boolean"},
                                            "tags": {"type": "array", "items": {"type": "string"}},
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    resp = _import(client, spec=spec)
    assert resp.status_code == 200
    resp2 = client.get("/widget")
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["id"].startswith("") and isinstance(body["id"], str)  # rendered from "{{uuid}}"
    assert body["email"] == "user@example.com"
    assert body["site"] == "https://example.com"
    assert body["count"] == 3
    assert body["price"] == 1.5
    assert body["active"] is True
    assert body["tags"] == ["string"]


def test_ref_resolution_allof_oneof(app_module):
    client = TestClient(app_module.app)
    spec = {
        "openapi": "3.0.3",
        "components": {
            "schemas": {
                "Base": {"type": "object", "properties": {"id": {"type": "integer", "minimum": 1}}},
                "Extra": {"type": "object", "properties": {"name": {"type": "string"}}},
            }
        },
        "paths": {
            "/ref": {
                "get": {
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Base"}}}}
                    }
                }
            },
            "/allof": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "allOf": [
                                            {"$ref": "#/components/schemas/Base"},
                                            {"$ref": "#/components/schemas/Extra"},
                                        ]
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/oneof": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "oneOf": [
                                            {"$ref": "#/components/schemas/Base"},
                                            {"$ref": "#/components/schemas/Extra"},
                                        ]
                                    }
                                }
                            }
                        }
                    }
                }
            },
        },
    }
    _import(client, spec=spec)
    assert client.get("/ref").json() == {"id": 1}
    assert client.get("/allof").json() == {"id": 1, "name": "string"}
    assert client.get("/oneof").json() == {"id": 1}


def test_path_param_echoed_back_when_response_has_same_named_field(app_module):
    client = TestClient(app_module.app)
    spec = {
        "openapi": "3.0.3",
        "paths": {
            "/users/{id}": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "example": {"id": "placeholder", "name": "x"}
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    _import(client, spec=spec)
    assert client.get("/users/42").json() == {"id": "42", "name": "x"}


def test_servers_base_path_prefixed(app_module):
    client = TestClient(app_module.app)
    spec = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/{version}", "variables": {"version": {"default": "v1"}}}],
        "paths": {"/ping": {"get": {"responses": {"200": {"content": {"application/json": {"example": {"ok": True}}}}}}}},
    }
    resp = _import(client, spec=spec)
    assert resp.json()["base_path"] == "/v1"
    assert client.get("/v1/ping").json() == {"ok": True}
    assert client.get("/ping").status_code == 404


def test_request_body_required_fields_carried_over(app_module):
    client = TestClient(app_module.app)
    spec = {
        "openapi": "3.0.3",
        "paths": {
            "/create": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
                            }
                        }
                    },
                    "responses": {"200": {"content": {"application/json": {"example": {"ok": True}}}}},
                }
            }
        },
    }
    _import(client, spec=spec)
    resp = client.post("/create", json={})
    assert resp.status_code == 400
    assert resp.json()["fields"] == ["name"]
    assert client.post("/create", json={"name": "x"}).status_code == 200


def test_swagger_2_rejected(app_module):
    client = TestClient(app_module.app)
    resp = _import(client, spec={"swagger": "2.0", "paths": {}})
    assert resp.status_code == 400


def test_missing_openapi_field_rejected(app_module):
    client = TestClient(app_module.app)
    resp = _import(client, spec={"paths": {}})
    assert resp.status_code == 400


def test_yaml_string_spec_accepted(app_module):
    client = TestClient(app_module.app)
    yaml_spec = """
openapi: "3.0.3"
paths:
  /y:
    get:
      responses:
        "200":
          content:
            application/json:
              example: {ok: true}
"""
    resp = _import(client, spec=yaml_spec)
    assert resp.status_code == 200
    assert client.get("/y").json() == {"ok": True}


def test_spec_neither_json_nor_yaml_rejected(app_module):
    client = TestClient(app_module.app)
    resp = _import(client, spec="not: valid: yaml: at: all: [")
    assert resp.status_code == 400


def test_invalid_operation_is_skipped_not_fatal(app_module):
    client = TestClient(app_module.app)
    spec = {
        "openapi": "3.0.3",
        "paths": {
            "/good": {"get": {"responses": {"200": {"content": {"application/json": {"example": {"ok": True}}}}}}},
            "/bad/{id}/{id}": {"get": {"responses": {"200": {"content": {"application/json": {"example": {"ok": True}}}}}}},
        },
    }
    resp = _import(client, spec=spec)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["created"]) == 1
    assert len(body["skipped"]) == 1
    assert client.get("/good").status_code == 200


def test_replace_clears_existing_routes_first(app_module):
    client = TestClient(app_module.app)
    client.post("/_routes", json={"path": "/keep-me"}, headers=ADMIN)
    spec = {
        "openapi": "3.0.3",
        "paths": {"/new": {"get": {"responses": {"200": {"content": {"application/json": {"example": {"ok": True}}}}}}}},
    }
    _import(client, spec=spec, replace=True)
    assert client.get("/keep-me").status_code == 404
    assert client.get("/new").status_code == 200


def test_replace_false_keeps_existing_routes(app_module):
    client = TestClient(app_module.app)
    client.post("/_routes", json={"path": "/keep-me"}, headers=ADMIN)
    spec = {
        "openapi": "3.0.3",
        "paths": {"/new": {"get": {"responses": {"200": {"content": {"application/json": {"example": {"ok": True}}}}}}}},
    }
    _import(client, spec=spec, replace=False)
    assert client.get("/keep-me").status_code == 200
    assert client.get("/new").status_code == 200


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self._get_result = kwargs.pop("_get_result", None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        result = _FAKE_GET_RESULT["value"]
        if isinstance(result, Exception):
            raise result
        return result


_FAKE_GET_RESULT = {"value": None}


def test_url_form_fetches_and_imports_spec(app_module, monkeypatch):
    client = TestClient(app_module.app)
    spec_text = '{"openapi": "3.0.3", "paths": {"/u": {"get": {"responses": {"200": {"content": {"application/json": {"example": {"ok": true}}}}}}}}}'
    _FAKE_GET_RESULT["value"] = _FakeResponse(spec_text, 200)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _FakeAsyncClient)
    resp = _import(client, url="http://example.com/openapi.json")
    assert resp.status_code == 200
    assert client.get("/u").json() == {"ok": True}


def test_url_form_fetch_failure_returns_400(app_module, monkeypatch):
    client = TestClient(app_module.app)
    _FAKE_GET_RESULT["value"] = app_module.httpx.HTTPError("boom")
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _FakeAsyncClient)
    resp = _import(client, url="http://example.com/openapi.json")
    assert resp.status_code == 400


def test_url_form_non_2xx_status_returns_400(app_module, monkeypatch):
    client = TestClient(app_module.app)
    _FAKE_GET_RESULT["value"] = _FakeResponse("not found", 404)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _FakeAsyncClient)
    resp = _import(client, url="http://example.com/openapi.json")
    assert resp.status_code == 400
