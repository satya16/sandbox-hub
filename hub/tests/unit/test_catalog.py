"""catalog.py is mostly static data -- a light sanity pass, not exhaustive
checks of every hardcoded string."""
from app import catalog


def test_kinds_are_keyed_by_their_own_id():
    for key, kdef in catalog.KINDS.items():
        assert kdef.id == key


def test_expected_kinds_are_present():
    assert set(catalog.KINDS) == {
        "rest-api", "mcp-server", "mock-api", "graphql-api",
        "webhook-receiver", "chaos-api", "api-tester",
    }


def test_chaos_api_disallows_auth_but_supports_chaos_config():
    kdef = catalog.KINDS["chaos-api"]
    assert kdef.supports_auth is False
    assert kdef.supports_chaos_config is True


def test_rest_api_supports_openapi_and_async_jobs():
    kdef = catalog.KINDS["rest-api"]
    assert kdef.supports_openapi is True
    assert kdef.supports_async_job is True


def test_auth_modes_and_openapi_versions():
    assert catalog.AUTH_MODES == ["none", "apikey", "basic", "jwt", "session", "oauth", "hmac"]
    assert catalog.OPENAPI_VERSIONS == ["3.0", "3.1"]


def test_oauth_provider_constants_defined():
    assert catalog.OAUTH_PROVIDER_IMAGE
    assert catalog.OAUTH_PROVIDER_CONTAINER_PORT == 8000
