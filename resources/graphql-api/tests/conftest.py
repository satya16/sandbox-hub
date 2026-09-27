"""
See resources/rest-api/tests/conftest.py for why this exists: graphql-api's
app.py reads AUTH_MODE and friends into module-level constants at import
time, so testing more than one auth mode means re-exec'ing the module fresh
per test, with an isolated schema/resolvers/sessions each time.
"""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

ENV_KEYS = [
    "AUTH_MODE", "API_KEY", "BASIC_USERNAME", "BASIC_PASSWORD",
    "JWT_SECRET", "JWT_TTL_SECONDS", "SESSION_USERNAME", "SESSION_PASSWORD",
    "OAUTH_INTROSPECT_URL", "HMAC_SECRET", "GRAPHQL_ADMIN_TOKEN",
]


@pytest.fixture
def make_app(monkeypatch):
    loaded = []

    def _make(**env):
        for key in ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))

        module_name = f"graphql_api_app_under_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded.append(module_name)
        return module

    yield _make

    for name in loaded:
        sys.modules.pop(name, None)


@pytest.fixture
def app_module(make_app):
    """Most tests don't care about auth -- a ready instance with defaults
    (AUTH_MODE=none, the default dev admin token, the seeded Item schema)."""
    return make_app()
