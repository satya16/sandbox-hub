"""
Unit tests for hub/app/docker_manager.py -- the Docker client and every
instance's admin HTTP API are faked (see conftest.py's fake_docker/
fake_httpx fixtures and register_container() helper), so these run with no
Docker daemon and cover validation/error-mapping/config-building/live-state
logic that's slow or awkward to exercise by spinning real containers.
"""
import socket

import docker.errors
import httpx
import pytest

from app import catalog
from app import docker_manager as dm
from tests.unit.conftest import FakeContainer, register_container


# --------------------------------------------------------------- ensure_network


def test_ensure_network_creates_when_missing(fake_docker):
    dm.ensure_network()
    assert dm.NETWORK_NAME in fake_docker.networks._store


def test_ensure_network_reuses_existing_network(fake_docker):
    fake_docker.networks.create(dm.NETWORK_NAME)
    dm.ensure_network()
    assert len(fake_docker.networks._store) == 1


def test_ensure_network_connects_self_container_when_not_joined(fake_docker):
    # docker_manager._self_container() looks the hub's own container up by
    # its hostname -- registering one under socket.gethostname() is what
    # makes ensure_network() think it's running inside a container.
    self_c = FakeContainer(name=socket.gethostname(), store=fake_docker._container_store)
    fake_docker._container_store[self_c.name] = self_c

    dm.ensure_network()

    net = fake_docker.networks.get(dm.NETWORK_NAME)
    assert self_c in net.connected


def test_ensure_network_noop_when_self_container_already_joined(fake_docker):
    self_c = FakeContainer(
        name=socket.gethostname(), networks={dm.NETWORK_NAME: {}}, store=fake_docker._container_store
    )
    fake_docker._container_store[self_c.name] = self_c

    dm.ensure_network()

    net = fake_docker.networks.get(dm.NETWORK_NAME)
    assert net.connected == []


def test_ensure_network_noop_when_no_self_container(fake_docker):
    # No container named after our own hostname -- same as running outside
    # any container, e.g. this very test process.
    dm.ensure_network()
    net = fake_docker.networks.get(dm.NETWORK_NAME)
    assert net.connected == []


# --------------------------------------------------------------- _build_env


def test_build_env_apikey():
    env = dm._build_env("mock-api", {"auth_mode": "apikey"})
    assert env["AUTH_MODE"] == "apikey"
    assert env["API_KEY"]
    for k in ("BASIC_USERNAME", "BASIC_PASSWORD", "JWT_SECRET", "SESSION_USERNAME", "SESSION_PASSWORD", "HMAC_SECRET"):
        assert k not in env


def test_build_env_basic():
    env = dm._build_env("mock-api", {"auth_mode": "basic"})
    assert env["BASIC_USERNAME"] == "sandbox"
    assert env["BASIC_PASSWORD"]
    assert "API_KEY" not in env


def test_build_env_jwt():
    env = dm._build_env("mock-api", {"auth_mode": "jwt"})
    assert env["JWT_SECRET"]
    assert "API_KEY" not in env


def test_build_env_session():
    env = dm._build_env("mock-api", {"auth_mode": "session"})
    assert env["SESSION_USERNAME"] == "sandbox"
    assert env["SESSION_PASSWORD"]


def test_build_env_hmac():
    env = dm._build_env("mock-api", {"auth_mode": "hmac"})
    assert env["HMAC_SECRET"]


def test_build_env_oauth_and_none_generate_no_secrets():
    for mode in ("oauth", "none"):
        env = dm._build_env("mock-api", {"auth_mode": mode})
        for k in ("API_KEY", "BASIC_USERNAME", "BASIC_PASSWORD", "JWT_SECRET", "SESSION_USERNAME", "SESSION_PASSWORD", "HMAC_SECRET"):
            assert k not in env


def test_build_env_rest_api_openapi_protect_and_async_jobs():
    env = dm._build_env(
        "rest-api",
        {"auth_mode": "none", "openapi_protect": True, "async_jobs": True, "async_job_delay_seconds": 9},
    )
    assert env["OPENAPI_PROTECT"] == "true"
    assert env["OPENAPI_TOKEN"]
    assert env["ASYNC_JOBS"] == "true"
    assert env["ASYNC_JOB_DELAY_SECONDS"] == "9"


def test_build_env_rest_api_without_openapi_protect_or_async_jobs():
    env = dm._build_env("rest-api", {"auth_mode": "none"})
    assert "OPENAPI_TOKEN" not in env
    assert "OPENAPI_PROTECT" not in env
    assert "ASYNC_JOBS" not in env
    assert env["OPENAPI_VERSION"] == "3.1"


def test_build_env_admin_tokens_only_for_their_own_kind():
    for kind, key in (("chaos-api", "CHAOS_ADMIN_TOKEN"), ("mock-api", "MOCK_ADMIN_TOKEN"), ("graphql-api", "GRAPHQL_ADMIN_TOKEN")):
        env = dm._build_env(kind, {"auth_mode": "none"})
        assert env[key]
        others = {"CHAOS_ADMIN_TOKEN", "MOCK_ADMIN_TOKEN", "GRAPHQL_ADMIN_TOKEN"} - {key}
        for other in others:
            assert other not in env
    # A kind with none of the three admin APIs gets none of the tokens.
    env = dm._build_env("rest-api", {"auth_mode": "none"})
    assert not ({"CHAOS_ADMIN_TOKEN", "MOCK_ADMIN_TOKEN", "GRAPHQL_ADMIN_TOKEN"} & env.keys())


# ------------------------------------------------------------ create_instance


def test_create_instance_unknown_kind(fake_docker):
    with pytest.raises(ValueError):
        dm.create_instance("no-such-kind", None, {})


def test_create_instance_unknown_auth_mode(fake_docker):
    with pytest.raises(ValueError):
        dm.create_instance("mock-api", None, {"auth_mode": "bogus"})


def test_create_instance_forces_auth_none_for_supports_auth_false(fake_docker, fake_httpx):
    # chaos-api has supports_auth=False; passing another mode must not
    # error and must be silently overridden to "none".
    detail = dm.create_instance("chaos-api", None, {"auth_mode": "apikey"})
    assert detail["auth"]["mode"] == "none"
    run_call = fake_docker.containers.run_calls[-1]
    assert run_call["environment"]["AUTH_MODE"] == "none"


def test_create_instance_run_call_shape(fake_docker, fake_httpx):
    detail = dm.create_instance("mock-api", "my-name", {"auth_mode": "apikey"})
    run_call = fake_docker.containers.run_calls[-1]
    assert run_call["image"] == catalog.KINDS["mock-api"].image
    assert run_call["name"] == dm._container_name(detail["id"])
    assert list(run_call["ports"].keys()) == ["8000/tcp"]
    assert run_call["environment"]["AUTH_MODE"] == "apikey"
    assert run_call["environment"]["API_KEY"]
    labels = run_call["labels"]
    assert labels[dm.LABEL_MANAGED] == "true"
    assert labels[dm.LABEL_ROLE] == "instance"
    assert labels[dm.LABEL_KIND] == "mock-api"
    assert labels[dm.LABEL_NAME] == "my-name"


def test_create_instance_retries_on_port_conflict(fake_docker, fake_httpx, monkeypatch):
    # _free_port() only checks a port is free *at that instant* -- another
    # container can grab it first by the time Docker actually binds it.
    # Simulate that: the first run() raises the exact error Docker raises
    # for it, having already left a container behind (Docker creates before
    # it starts), and the second run() succeeds.
    calls = {"n": 0}
    original_run = fake_docker.containers.run

    def flaky_run(*args, name=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            fake_docker._container_store[name] = FakeContainer(name=name, store=fake_docker._container_store)
            raise docker.errors.APIError("Bind for 127.0.0.1:1234 failed: port is already allocated")
        return original_run(*args, name=name, **kwargs)

    monkeypatch.setattr(fake_docker.containers, "run", flaky_run)

    detail = dm.create_instance("mock-api", None, {"auth_mode": "none"})

    assert calls["n"] == 2
    assert detail is not None
    # The leftover container from the failed attempt must be gone, not
    # left behind forever in a "created" state.
    container_name = dm._container_name(detail["id"])
    assert fake_docker._container_store[container_name].removed is False  # this is the *successful* one
    assert len(fake_docker.containers.run_calls) == 1  # only the successful call is recorded as a real run


def test_create_instance_gives_up_after_repeated_port_conflicts(fake_docker, fake_httpx, monkeypatch):
    def always_conflicts(*args, name=None, **kwargs):
        fake_docker._container_store[name] = FakeContainer(name=name, store=fake_docker._container_store)
        raise docker.errors.APIError("Bind for 127.0.0.1:1234 failed: port is already allocated")

    monkeypatch.setattr(fake_docker.containers, "run", always_conflicts)

    with pytest.raises(docker.errors.APIError):
        dm.create_instance("mock-api", None, {"auth_mode": "none"})


def test_create_instance_does_not_retry_unrelated_docker_errors(fake_docker, fake_httpx, monkeypatch):
    def boom(*args, **kwargs):
        raise docker.errors.APIError("no such image: satya16dev/sandboxhub-mock-api:latest")

    monkeypatch.setattr(fake_docker.containers, "run", boom)

    with pytest.raises(docker.errors.APIError):
        dm.create_instance("mock-api", None, {"auth_mode": "none"})


def test_create_instance_calls_ensure_network(fake_docker, fake_httpx, monkeypatch):
    called = []
    monkeypatch.setattr(dm, "ensure_network", lambda: called.append(True))
    dm.create_instance("mock-api", None, {"auth_mode": "none"})
    assert called == [True]


def test_create_instance_oauth_mode_registers_client(fake_docker, fake_httpx, monkeypatch):
    calls = {"ensure": 0, "register": 0}

    def fake_ensure_oauth_provider():
        calls["ensure"] += 1
        return 55000

    def fake_register(instance_id):
        calls["register"] += 1
        return {"client_id": "cid", "client_secret": "csecret"}

    monkeypatch.setattr(dm, "_ensure_oauth_provider", fake_ensure_oauth_provider)
    monkeypatch.setattr(dm, "_register_oauth_client", fake_register)

    dm.create_instance("mock-api", None, {"auth_mode": "oauth"})
    assert calls == {"ensure": 1, "register": 1}


def test_create_instance_jwt_mode_fetches_token(fake_docker, fake_httpx, monkeypatch):
    calls = []
    monkeypatch.setattr(dm, "_fetch_jwt_token", lambda instance_id, kind: calls.append((instance_id, kind)))
    dm.create_instance("mock-api", None, {"auth_mode": "jwt"})
    assert len(calls) == 1


# -------------------------------------------------------------- _describe_auth


def test_describe_auth_apikey():
    info = dm._describe_auth("apikey", "iid", "mock-api", {"API_KEY": "abc"}, "http://localhost:1")
    assert info == {"mode": "apikey", "header": "X-API-Key", "api_key": "abc"}


def test_describe_auth_basic():
    info = dm._describe_auth("basic", "iid", "mock-api", {"BASIC_USERNAME": "u", "BASIC_PASSWORD": "p"}, None)
    assert info == {"mode": "basic", "username": "u", "password": "p"}


def test_describe_auth_jwt(monkeypatch):
    dm._jwt_token_cache["iid"] = "tok"
    info = dm._describe_auth("jwt", "iid", "mock-api", {}, "http://localhost:1")
    assert info["mode"] == "jwt"
    assert info["token"] == "tok"
    assert info["debug_token_url"] == "http://localhost:1/_debug/token"


def test_describe_auth_jwt_no_url():
    info = dm._describe_auth("jwt", "iid", "mock-api", {}, None)
    assert info["debug_token_url"] is None


def test_describe_auth_session():
    info = dm._describe_auth("session", "iid", "mock-api", {"SESSION_USERNAME": "u", "SESSION_PASSWORD": "p"}, "http://localhost:1")
    assert info == {
        "mode": "session",
        "login_url": "http://localhost:1/login",
        "logout_url": "http://localhost:1/logout",
        "username": "u",
        "password": "p",
    }


def test_describe_auth_oauth(fake_docker):
    dm._oauth_client_cache["iid"] = {"client_id": "cid", "client_secret": "csecret"}
    info = dm._describe_auth("oauth", "iid", "mock-api", {}, "http://localhost:1")
    assert info["mode"] == "oauth"
    assert info["client_id"] == "cid"
    assert info["client_secret"] == "csecret"
    # oauth provider not running -> stopped/None url -> token_endpoint None
    assert info["token_endpoint"] is None


def test_describe_auth_hmac():
    info = dm._describe_auth("hmac", "iid", "mock-api", {"HMAC_SECRET": "s"}, None)
    assert info == {"mode": "hmac", "header": "X-Signature", "secret": "s", "tolerance_seconds": 300}


def test_describe_auth_none():
    assert dm._describe_auth("none", "iid", "mock-api", {}, None) == {"mode": "none"}


# ------------------------------------------------------------- instance_detail


def test_instance_detail_unknown_returns_none(fake_docker):
    assert dm.instance_detail("does-not-exist") is None


def test_instance_detail_rest_api_openapi_and_async_jobs(fake_docker):
    register_container(
        fake_docker, "rest-api-abc123", "rest-api",
        config={"auth_mode": "none", "openapi_version": "3.0", "openapi_protect": True, "async_jobs": True, "async_job_delay_seconds": 7},
        env={"OPENAPI_TOKEN": "tok123"},
    )
    detail = dm.instance_detail("rest-api-abc123")
    assert detail["openapi"]["version"] == "3.0"
    assert detail["openapi"]["protected"] is True
    assert detail["openapi"]["auth"] == {"header": "X-API-Key", "token": "tok123"}
    assert detail["async_jobs"]["delay_seconds"] == 7
    assert detail["async_jobs"]["submit_url"].endswith("/jobs")


def test_instance_detail_chaos_api_running_populates_from_admin(fake_docker, fake_httpx):
    register_container(fake_docker, "chaos-api-abc", "chaos-api", config={"auth_mode": "none"}, status="running")
    fake_httpx.responses[("GET", dm._instance_internal_url("chaos-api-abc", "chaos-api", "/_config"))] = (
        __import__("tests.unit.conftest", fromlist=["FakeResponse"]).FakeResponse(200, {"mode": "normal"})
    )
    detail = dm.instance_detail("chaos-api-abc")
    assert detail["chaos"] == {"mode": "normal"}


def test_instance_detail_chaos_api_admin_down_gives_none(fake_docker, fake_httpx):
    register_container(fake_docker, "chaos-api-abc", "chaos-api", config={"auth_mode": "none"}, status="running")
    url = dm._instance_internal_url("chaos-api-abc", "chaos-api", "/_config")
    fake_httpx.responses[("GET", url)] = httpx.ConnectError("boom")
    detail = dm.instance_detail("chaos-api-abc")
    assert detail["chaos"] is None


def test_instance_detail_mock_api_running_populates_routes(fake_docker, fake_httpx):
    register_container(fake_docker, "mock-api-abc", "mock-api", config={"auth_mode": "none"}, status="running")
    url = dm._instance_internal_url("mock-api-abc", "mock-api", "/_routes")
    from tests.unit.conftest import FakeResponse
    fake_httpx.responses[("GET", url)] = FakeResponse(200, [{"id": "r1"}])
    detail = dm.instance_detail("mock-api-abc")
    assert detail["mock_routes"] == [{"id": "r1"}]


def test_instance_detail_mock_api_admin_down_gives_empty_list(fake_docker, fake_httpx):
    register_container(fake_docker, "mock-api-abc", "mock-api", config={"auth_mode": "none"}, status="running")
    url = dm._instance_internal_url("mock-api-abc", "mock-api", "/_routes")
    fake_httpx.responses[("GET", url)] = httpx.ConnectError("boom")
    detail = dm.instance_detail("mock-api-abc")
    assert detail["mock_routes"] == []


def test_instance_detail_graphql_api_running_populates_schema_and_resolvers(fake_docker, fake_httpx):
    register_container(fake_docker, "graphql-api-abc", "graphql-api", config={"auth_mode": "none"}, status="running")
    from tests.unit.conftest import FakeResponse
    schema_url = dm._instance_internal_url("graphql-api-abc", "graphql-api", "/_schema")
    resolvers_url = dm._instance_internal_url("graphql-api-abc", "graphql-api", "/_resolvers")
    fake_httpx.responses[("GET", schema_url)] = FakeResponse(200, {"sdl": "type Query { x: Int }"})
    fake_httpx.responses[("GET", resolvers_url)] = FakeResponse(200, [{"type": "Query", "field": "x"}])
    detail = dm.instance_detail("graphql-api-abc")
    assert detail["graphql_sdl"] == "type Query { x: Int }"
    assert detail["graphql_resolvers"] == [{"type": "Query", "field": "x"}]
    assert detail["graphiql_url"].endswith("/graphiql")


def test_instance_detail_graphql_api_admin_down_gives_none_and_empty(fake_docker, fake_httpx):
    register_container(fake_docker, "graphql-api-abc", "graphql-api", config={"auth_mode": "none"}, status="running")
    schema_url = dm._instance_internal_url("graphql-api-abc", "graphql-api", "/_schema")
    fake_httpx.responses[("GET", schema_url)] = httpx.ConnectError("boom")
    detail = dm.instance_detail("graphql-api-abc")
    assert detail["graphql_sdl"] is None
    assert detail["graphql_resolvers"] == []


def test_instance_detail_not_running_skips_admin_calls(fake_docker, fake_httpx):
    register_container(fake_docker, "chaos-api-abc", "chaos-api", config={"auth_mode": "none"}, status="exited")
    detail = dm.instance_detail("chaos-api-abc")
    assert "chaos" not in detail
    assert fake_httpx.calls == []


# --------------------------------------------------------------- list_instances


def test_list_instances_filters_by_labels(fake_docker):
    register_container(fake_docker, "mock-api-a", "mock-api", created="2024-01-01T00:00:00.000000000Z")
    detail = dm.list_instances()
    assert len(detail) == 1
    list_call_filters = None
    # Reach in via a wrapper to capture filters actually passed.
    orig_list = fake_docker.containers.list

    def spy_list(all=True, filters=None):
        nonlocal list_call_filters
        list_call_filters = filters
        return orig_list(all=all, filters=filters)

    fake_docker.containers.list = spy_list
    dm.list_instances()
    assert set(list_call_filters["label"]) == {f"{dm.LABEL_MANAGED}=true", f"{dm.LABEL_ROLE}=instance"}


def test_list_instances_sorts_by_created_at_descending(fake_docker):
    register_container(fake_docker, "mock-api-old", "mock-api", created="2024-01-01T00:00:00.000000000Z")
    register_container(fake_docker, "mock-api-new", "mock-api", created="2024-06-01T00:00:00.000000000Z")
    out = dm.list_instances()
    assert [d["id"] for d in out] == ["mock-api-new", "mock-api-old"]


# --------------------------------------------------------------- remove_instance


def test_remove_instance_stops_and_removes_container(fake_docker):
    register_container(fake_docker, "mock-api-a", "mock-api")
    container = fake_docker.containers.get(dm._container_name("mock-api-a"))
    dm.remove_instance("mock-api-a")
    assert container.stopped is True
    assert container.removed is True
    assert dm._get_container("mock-api-a") is None


def test_remove_instance_pops_oauth_cache_and_stops_provider_cleanly_when_absent(fake_docker):
    register_container(fake_docker, "mock-api-a", "mock-api", config={"auth_mode": "oauth"})
    dm._oauth_client_cache["mock-api-a"] = {"client_id": "x"}
    # No oauth-provider container registered at all -> _maybe_stop_oauth_provider
    # must swallow the NotFound cleanly.
    dm.remove_instance("mock-api-a")
    assert "mock-api-a" not in dm._oauth_client_cache


def test_remove_instance_stops_provider_when_no_running_oauth_instance_remains(fake_docker):
    register_container(fake_docker, "mock-api-a", "mock-api", config={"auth_mode": "oauth"})
    register_container(fake_docker, dm.OAUTH_PROVIDER_NAME.replace(dm.CONTAINER_PREFIX, "", 1), "unused")
    # Manually register the provider container under its exact expected name.
    from tests.unit.conftest import FakeContainer
    provider = FakeContainer(name=dm.OAUTH_PROVIDER_NAME, labels={dm.LABEL_MANAGED: "true", dm.LABEL_ROLE: "infra"}, status="running")
    fake_docker._container_store[dm.OAUTH_PROVIDER_NAME] = provider
    dm.remove_instance("mock-api-a")
    assert provider.stopped is True
    assert provider.removed is True


def test_remove_instance_leaves_provider_if_other_running_oauth_instance_exists(fake_docker):
    register_container(fake_docker, "mock-api-a", "mock-api", config={"auth_mode": "oauth"}, status="running")
    register_container(fake_docker, "mock-api-b", "mock-api", config={"auth_mode": "oauth"}, status="running")
    from tests.unit.conftest import FakeContainer
    provider = FakeContainer(name=dm.OAUTH_PROVIDER_NAME, labels={dm.LABEL_MANAGED: "true", dm.LABEL_ROLE: "infra"}, status="running")
    fake_docker._container_store[dm.OAUTH_PROVIDER_NAME] = provider
    dm.remove_instance("mock-api-a")
    assert provider.stopped is False
    assert provider.removed is False


# ------------------------------------------------- _snapshot/_restore_live_state


def test_snapshot_live_state_not_running_returns_none(fake_docker):
    c = register_container(fake_docker, "mock-api-a", "mock-api", status="exited")
    assert dm._snapshot_live_state(c, "mock-api-a", "mock-api") is None


def test_snapshot_live_state_unsupported_kind_returns_none(fake_docker):
    c = register_container(fake_docker, "rest-api-a", "rest-api", status="running")
    assert dm._snapshot_live_state(c, "rest-api-a", "rest-api") is None


def test_snapshot_live_state_mock_api(fake_docker, monkeypatch):
    c = register_container(fake_docker, "mock-api-a", "mock-api", status="running")
    monkeypatch.setattr(dm, "list_routes", lambda iid: [{"id": "r1"}])
    state = dm._snapshot_live_state(c, "mock-api-a", "mock-api")
    assert state == {"routes": [{"id": "r1"}]}


def test_snapshot_live_state_graphql_api(fake_docker, monkeypatch):
    c = register_container(fake_docker, "graphql-api-a", "graphql-api", status="running")
    monkeypatch.setattr(dm, "get_graphql_schema", lambda iid: {"sdl": "type Query { x: Int }"})
    monkeypatch.setattr(dm, "list_graphql_resolvers", lambda iid: [{"type": "Query", "field": "x"}])
    state = dm._snapshot_live_state(c, "graphql-api-a", "graphql-api")
    assert state == {"sdl": "type Query { x: Int }", "resolvers": [{"type": "Query", "field": "x"}]}


def test_snapshot_live_state_chaos_api(fake_docker, fake_httpx):
    c = register_container(fake_docker, "chaos-api-a", "chaos-api", status="running")
    from tests.unit.conftest import FakeResponse
    url = dm._instance_internal_url("chaos-api-a", "chaos-api", "/_config")
    fake_httpx.responses[("GET", url)] = FakeResponse(200, {"mode": "normal"})
    state = dm._snapshot_live_state(c, "chaos-api-a", "chaos-api")
    assert state == {"config": {"mode": "normal"}}


def test_restore_live_state_none_state_does_nothing(fake_docker, monkeypatch):
    called = []
    monkeypatch.setattr(dm, "create_route", lambda *a, **k: called.append(True))
    dm._restore_live_state("mock-api-a", "mock-api", None)
    assert called == []


def test_restore_live_state_mock_api_crud_uses_items_not_seed(fake_docker, monkeypatch):
    """Regression guard: a crud route's restored seed must come from the
    route's live "items" (current collection contents), not its original
    "seed" field -- the source has an explicit comment about this. If this
    test starts failing after a change to _restore_live_state, that's the
    bug the comment warns about, not a bad test."""
    clear_calls = []
    create_calls = []
    monkeypatch.setattr(dm, "clear_routes", lambda iid: clear_calls.append(iid))
    monkeypatch.setattr(dm, "create_route", lambda iid, payload: create_calls.append(payload))

    state = {
        "routes": [
            {
                "id": "r1",
                "type": "crud",
                "path": "/items",
                "seed": [{"id": 1, "name": "original-seed"}],
                "items": [{"id": 1, "name": "edited"}, {"id": 2, "name": "added-later"}],
            }
        ]
    }
    dm._restore_live_state("mock-api-a", "mock-api", state)
    assert clear_calls == ["mock-api-a"]
    assert len(create_calls) == 1
    payload = create_calls[0]
    assert payload["seed"] == [{"id": 1, "name": "edited"}, {"id": 2, "name": "added-later"}]
    assert "id" not in payload
    assert "items" not in payload


def test_restore_live_state_mock_api_static_route_passes_through(fake_docker, monkeypatch):
    create_calls = []
    monkeypatch.setattr(dm, "clear_routes", lambda iid: None)
    monkeypatch.setattr(dm, "create_route", lambda iid, payload: create_calls.append(payload))
    state = {"routes": [{"id": "r1", "type": "static", "path": "/x", "method": "GET"}]}
    dm._restore_live_state("mock-api-a", "mock-api", state)
    assert create_calls == [{"type": "static", "path": "/x", "method": "GET"}]


def test_restore_live_state_graphql_api(fake_docker, fake_httpx, monkeypatch):
    set_schema_calls = []
    monkeypatch.setattr(dm, "set_graphql_schema", lambda iid, sdl: set_schema_calls.append(sdl))
    from tests.unit.conftest import FakeResponse
    resolvers_url = dm._instance_internal_url("graphql-api-a", "graphql-api", "/_resolvers")
    fake_httpx.responses[("DELETE", resolvers_url)] = FakeResponse(200, {})
    fake_httpx.responses[("POST", resolvers_url)] = FakeResponse(200, {"ok": True})
    state = {"sdl": "type Query { x: Int }", "resolvers": [{"type": "Query", "field": "x"}]}
    dm._restore_live_state("graphql-api-a", "graphql-api", state)
    assert set_schema_calls == ["type Query { x: Int }"]
    post_calls = [c for c in fake_httpx.calls if c["method"] == "POST" and c["url"] == resolvers_url]
    assert len(post_calls) == 1


def test_restore_live_state_graphql_api_drops_400_rejected_resolver(fake_docker, fake_httpx, monkeypatch):
    monkeypatch.setattr(dm, "set_graphql_schema", lambda iid, sdl: None)
    from tests.unit.conftest import FakeResponse
    resolvers_url = dm._instance_internal_url("graphql-api-a", "graphql-api", "/_resolvers")
    fake_httpx.responses[("DELETE", resolvers_url)] = FakeResponse(200, {})
    fake_httpx.responses[("POST", resolvers_url)] = FakeResponse(400, {"detail": "no such field"})
    state = {"sdl": "type Query { x: Int }", "resolvers": [{"type": "Query", "field": "stale"}]}
    # Must not raise -- the 400 from a stale resolver is expected and dropped.
    dm._restore_live_state("graphql-api-a", "graphql-api", state)


def test_restore_live_state_chaos_api(fake_docker, monkeypatch):
    calls = []
    monkeypatch.setattr(dm, "configure_chaos", lambda iid, config: calls.append((iid, config)))
    state = {"config": {"mode": "normal"}}
    dm._restore_live_state("chaos-api-a", "chaos-api", state)
    assert calls == [("chaos-api-a", {"mode": "normal"})]


# ---------------------------------------------------------------- rotate_instance


def test_rotate_instance_unknown_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.rotate_instance("nope")


def test_rotate_instance_no_rotatable_credential(fake_docker):
    register_container(fake_docker, "chaos-api-a", "chaos-api", config={"auth_mode": "none"})
    with pytest.raises(ValueError, match="no rotatable credential"):
        dm.rotate_instance("chaos-api-a")


def test_rotate_instance_openapi_protect_is_rotatable_even_with_auth_none(fake_docker, fake_httpx):
    register_container(
        fake_docker, "rest-api-a", "rest-api",
        config={"auth_mode": "none", "openapi_protect": True},
        host_port=15000,
    )
    container = fake_docker.containers.get(dm._container_name("rest-api-a"))
    detail = dm.rotate_instance("rest-api-a")
    assert container.stopped is True
    assert container.removed is True
    assert detail["id"] == "rest-api-a"


def test_rotate_instance_oauth_reregisters_without_recreating_container(fake_docker, monkeypatch):
    register_container(fake_docker, "mock-api-a", "mock-api", config={"auth_mode": "oauth"})
    container = fake_docker.containers.get(dm._container_name("mock-api-a"))
    calls = []
    monkeypatch.setattr(dm, "_register_oauth_client", lambda iid: calls.append(iid))
    dm.rotate_instance("mock-api-a")
    assert calls == ["mock-api-a"]
    assert container.stopped is False
    assert container.removed is False


def test_rotate_instance_apikey_recreates_on_same_port_and_restores_state(fake_docker, fake_httpx, monkeypatch):
    register_container(fake_docker, "mock-api-a", "mock-api", config={"auth_mode": "apikey"}, host_port=17000)
    old_container = fake_docker.containers.get(dm._container_name("mock-api-a"))
    restore_calls = []
    monkeypatch.setattr(dm, "_snapshot_live_state", lambda container, iid, kind: {"routes": []})
    monkeypatch.setattr(dm, "_restore_live_state", lambda iid, kind, state: restore_calls.append((iid, kind, state)))
    detail = dm.rotate_instance("mock-api-a")
    assert old_container.stopped is True
    assert old_container.removed is True
    new_container = fake_docker.containers.get(dm._container_name("mock-api-a"))
    assert new_container.host_port == 17000
    assert restore_calls == [("mock-api-a", "mock-api", {"routes": []})]
    assert detail["auth"]["mode"] == "apikey"


def test_rotate_instance_jwt_fetches_new_token(fake_docker, fake_httpx, monkeypatch):
    register_container(fake_docker, "mock-api-a", "mock-api", config={"auth_mode": "jwt"})
    calls = []
    monkeypatch.setattr(dm, "_fetch_jwt_token", lambda iid, kind: calls.append((iid, kind)))
    dm.rotate_instance("mock-api-a")
    assert calls == [("mock-api-a", "mock-api")]


# ---------------------------------------------------------------- update_port


def test_update_port_unknown_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.update_port("nope", 12345)


def test_update_port_same_port_is_noop(fake_docker):
    register_container(fake_docker, "mock-api-a", "mock-api", host_port=19000)
    container = fake_docker.containers.get(dm._container_name("mock-api-a"))
    detail = dm.update_port("mock-api-a", 19000)
    assert container.stopped is False
    assert container.removed is False
    assert detail["id"] == "mock-api-a"


def test_update_port_new_port_in_use_raises(fake_docker, monkeypatch):
    register_container(fake_docker, "mock-api-a", "mock-api", host_port=19000)
    monkeypatch.setattr(dm, "_port_is_free", lambda port: False)
    with pytest.raises(ValueError, match="already in use"):
        dm.update_port("mock-api-a", 19001)


def test_update_port_recreates_and_refreshes_public_url(fake_docker, monkeypatch):
    register_container(
        fake_docker, "mock-api-a", "mock-api",
        config={"auth_mode": "oauth"}, host_port=19000,
        env={"PUBLIC_URL": "http://localhost:19000"},
    )
    monkeypatch.setattr(dm, "_port_is_free", lambda port: True)
    monkeypatch.setattr(dm, "_snapshot_live_state", lambda container, iid, kind: None)
    monkeypatch.setattr(dm, "_restore_live_state", lambda iid, kind, state: None)
    detail = dm.update_port("mock-api-a", 19999)
    new_container = fake_docker.containers.get(dm._container_name("mock-api-a"))
    assert new_container.host_port == 19999
    assert new_container._env["PUBLIC_URL"] == "http://localhost:19999"


def test_update_port_without_public_url_does_not_add_it(fake_docker, monkeypatch):
    register_container(fake_docker, "mock-api-a", "mock-api", config={"auth_mode": "none"}, host_port=19000)
    monkeypatch.setattr(dm, "_port_is_free", lambda port: True)
    monkeypatch.setattr(dm, "_snapshot_live_state", lambda container, iid, kind: None)
    monkeypatch.setattr(dm, "_restore_live_state", lambda iid, kind, state: None)
    dm.update_port("mock-api-a", 19999)
    new_container = fake_docker.containers.get(dm._container_name("mock-api-a"))
    assert "PUBLIC_URL" not in new_container._env


def test_update_port_jwt_refetches_token(fake_docker, monkeypatch):
    register_container(fake_docker, "mock-api-a", "mock-api", config={"auth_mode": "jwt"}, host_port=19000)
    monkeypatch.setattr(dm, "_port_is_free", lambda port: True)
    monkeypatch.setattr(dm, "_snapshot_live_state", lambda container, iid, kind: None)
    monkeypatch.setattr(dm, "_restore_live_state", lambda iid, kind, state: None)
    calls = []
    monkeypatch.setattr(dm, "_fetch_jwt_token", lambda iid, kind: calls.append((iid, kind)))
    dm.update_port("mock-api-a", 19999)
    assert calls == [("mock-api-a", "mock-api")]


# --------------------------------------------------- export/import scenario


def test_export_instance_never_contains_secrets(fake_docker, monkeypatch):
    register_container(
        fake_docker, "mock-api-a", "mock-api",
        config={"auth_mode": "apikey"},
        env={"API_KEY": "super-secret-value"},
    )
    monkeypatch.setattr(dm, "_snapshot_live_state", lambda container, iid, kind: {"routes": []})
    entry = dm.export_instance("mock-api-a")
    assert "super-secret-value" not in str(entry)
    assert entry["kind"] == "mock-api"
    assert entry["config"] == {"auth_mode": "apikey"}
    assert entry["live_state"] == {"routes": []}


def test_export_instance_unknown_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.export_instance("nope")


def test_export_instance_omits_live_state_key_when_none(fake_docker, monkeypatch):
    register_container(fake_docker, "rest-api-a", "rest-api", config={"auth_mode": "none"})
    monkeypatch.setattr(dm, "_snapshot_live_state", lambda container, iid, kind: None)
    entry = dm.export_instance("rest-api-a")
    assert "live_state" not in entry


def test_export_scenario_defaults_to_all_instances(fake_docker, monkeypatch):
    register_container(fake_docker, "mock-api-a", "mock-api")
    register_container(fake_docker, "mock-api-b", "mock-api")
    monkeypatch.setattr(dm, "_snapshot_live_state", lambda container, iid, kind: None)
    scenario = dm.export_scenario()
    assert scenario["sandboxhub_scenario"] == dm.SCENARIO_FORMAT_VERSION
    assert {e["kind"] for e in scenario["instances"]} == {"mock-api"}
    assert len(scenario["instances"]) == 2


def test_export_scenario_specific_ids(fake_docker, monkeypatch):
    register_container(fake_docker, "mock-api-a", "mock-api")
    register_container(fake_docker, "mock-api-b", "mock-api")
    monkeypatch.setattr(dm, "_snapshot_live_state", lambda container, iid, kind: None)
    scenario = dm.export_scenario(["mock-api-a"])
    assert len(scenario["instances"]) == 1


def test_import_scenario_wrong_version_raises(fake_docker):
    with pytest.raises(ValueError, match="format"):
        dm.import_scenario({"sandboxhub_scenario": 999, "instances": []})


def test_import_scenario_missing_instances_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.import_scenario({"sandboxhub_scenario": dm.SCENARIO_FORMAT_VERSION})


def test_import_scenario_empty_instances_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.import_scenario({"sandboxhub_scenario": dm.SCENARIO_FORMAT_VERSION, "instances": []})


def test_import_scenario_non_dict_entry_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.import_scenario({"sandboxhub_scenario": dm.SCENARIO_FORMAT_VERSION, "instances": ["not-a-dict"]})


def test_import_scenario_unknown_kind_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.import_scenario({"sandboxhub_scenario": dm.SCENARIO_FORMAT_VERSION, "instances": [{"kind": "bogus"}]})


def test_import_scenario_non_dict_config_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.import_scenario(
            {"sandboxhub_scenario": dm.SCENARIO_FORMAT_VERSION, "instances": [{"kind": "mock-api", "config": "nope"}]}
        )


def test_import_scenario_bad_auth_mode_raises(fake_docker):
    with pytest.raises(ValueError):
        dm.import_scenario(
            {
                "sandboxhub_scenario": dm.SCENARIO_FORMAT_VERSION,
                "instances": [{"kind": "mock-api", "config": {"auth_mode": "bogus"}}],
            }
        )


def test_import_scenario_validates_every_entry_before_creating_any(fake_docker, fake_httpx, monkeypatch):
    """A bad second entry must mean zero instances get created."""
    create_calls = []
    monkeypatch.setattr(dm, "create_instance", lambda kind, name, config: create_calls.append((kind, name, config)) or {"id": "x"})
    monkeypatch.setattr(dm, "instance_detail", lambda iid: {"id": iid})
    scenario = {
        "sandboxhub_scenario": dm.SCENARIO_FORMAT_VERSION,
        "instances": [
            {"kind": "mock-api", "config": {"auth_mode": "none"}},
            {"kind": "bogus-kind", "config": {"auth_mode": "none"}},
        ],
    }
    with pytest.raises(ValueError):
        dm.import_scenario(scenario)
    assert create_calls == []


def test_import_scenario_creates_and_restores_live_state(fake_docker, monkeypatch):
    create_calls = []
    restore_calls = []
    monkeypatch.setattr(dm, "create_instance", lambda kind, name, config: create_calls.append((kind, name, config)) or {"id": "iid1"})
    monkeypatch.setattr(dm, "_restore_live_state", lambda iid, kind, state: restore_calls.append((iid, kind, state)))
    monkeypatch.setattr(dm, "instance_detail", lambda iid: {"id": iid})
    scenario = {
        "sandboxhub_scenario": dm.SCENARIO_FORMAT_VERSION,
        "instances": [{"kind": "mock-api", "name": "n1", "config": {"auth_mode": "none"}, "live_state": {"routes": []}}],
    }
    created = dm.import_scenario(scenario)
    assert create_calls == [("mock-api", "n1", {"auth_mode": "none"})]
    assert restore_calls == [("iid1", "mock-api", {"routes": []})]
    assert created == [{"id": "iid1"}]


# --------------------------------------------------------------- _admin_request


def test_admin_request_raises_value_error_with_resource_detail(fake_httpx):
    from tests.unit.conftest import FakeResponse

    url = "http://fake-instance:8000/_config"
    fake_httpx.responses[("PUT", url)] = FakeResponse(
        400, {"detail": "mode must be one of: normal, rate_limit, chaos"}
    )

    with pytest.raises(ValueError, match="mode must be one of"):
        dm._admin_request("PUT", url, json={}, headers={})


def test_admin_request_returns_json_body_on_success(fake_httpx):
    from tests.unit.conftest import FakeResponse

    url = "http://fake-instance:8000/_config"
    fake_httpx.responses[("PUT", url)] = FakeResponse(200, {"mode": "normal"})

    assert dm._admin_request("PUT", url, json={}, headers={}) == {"mode": "normal"}


# --------------------------------------------------------- port helpers (real sockets)


def test_free_port_is_actually_bindable():
    port = dm._free_port()
    assert dm._port_is_free(port) is True


def test_port_is_free_false_when_port_is_bound():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((dm.BIND_HOST, 0))
    port = s.getsockname()[1]
    try:
        assert dm._port_is_free(port) is False
    finally:
        s.close()
