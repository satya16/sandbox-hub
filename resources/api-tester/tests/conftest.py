"""
api-tester's app.py has no env-driven config, but it does hold module-level
mutable state (_poll_state, a background _poll_task) -- re-exec'ing it fresh
per test (same trick as the other resources' conftest.py, see
resources/rest-api/tests/conftest.py) keeps tests from leaking a running
poll loop or stale results into each other.
"""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


@pytest.fixture
def make_app():
    loaded = []

    def _make():
        module_name = f"api_tester_app_under_test_{uuid.uuid4().hex}"
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
    return make_app()
