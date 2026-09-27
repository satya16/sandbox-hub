"""
rest-api's app.py reads its whole configuration (AUTH_MODE, secrets, feature
flags, ...) from environment variables at *import* time into module-level
constants -- exactly right for a container that's configured once via `docker
run -e`, but it means a test that wants a different AUTH_MODE can't just
monkeypatch os.environ and call into the already-imported module; the
constants were already baked in.

make_app() works around that by exec'ing app.py fresh, as its own module
object, once per call, after setting exactly the env vars given (everything
else relevant is cleared first via monkeypatch, which restores it after the
test regardless of outcome). Each call gets an independent module -- its own
_items dict, _active_sessions set, etc. -- so tests can run in any order
without leaking state into each other.

A random suffix on the module name (rather than reusing "app") means several
calls in the same test, or in different tests, never collide in sys.modules.
"""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

# Every env var app.py's module-level code reads via os.environ.get(...).
# Cleared before each load so a value left over from a previous test (or the
# ambient shell) can't leak in unnoticed.
ENV_KEYS = [
    "AUTH_MODE", "API_KEY", "BASIC_USERNAME", "BASIC_PASSWORD",
    "JWT_SECRET", "JWT_TTL_SECONDS", "SESSION_USERNAME", "SESSION_PASSWORD",
    "OAUTH_INTROSPECT_URL", "HMAC_SECRET",
    "OPENAPI_VERSION", "OPENAPI_PROTECT", "OPENAPI_TOKEN",
    "ASYNC_JOBS", "ASYNC_JOB_DELAY_SECONDS",
]


@pytest.fixture
def make_app(monkeypatch):
    loaded = []

    def _make(**env):
        for key in ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))

        module_name = f"rest_api_app_under_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded.append(module_name)
        return module

    yield _make

    for name in loaded:
        sys.modules.pop(name, None)
