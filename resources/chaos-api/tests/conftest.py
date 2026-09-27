"""
See resources/rest-api/tests/conftest.py for why this exists: even though
chaos-api only reads one env var (CHAOS_ADMIN_TOKEN), each test still wants
its own fresh _config/_hits state rather than sharing a single imported
module across the whole test session.
"""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parent.parent / "app.py"

ENV_KEYS = ["CHAOS_ADMIN_TOKEN"]


@pytest.fixture
def make_app(monkeypatch):
    loaded = []

    def _make(**env):
        for key in ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))

        module_name = f"chaos_api_app_under_test_{uuid.uuid4().hex}"
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
