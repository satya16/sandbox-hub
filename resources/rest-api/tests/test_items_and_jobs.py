"""/items CRUD-ish sample data, and the ASYNC_JOBS feature flag."""
from fastapi.testclient import TestClient


def test_list_and_get_items(make_app):
    client = TestClient(make_app(AUTH_MODE="none").app)

    resp = client.get("/items")
    assert resp.status_code == 200
    assert resp.json() == [{"id": 1, "name": "widget"}, {"id": 2, "name": "gadget"}]

    assert client.get("/items/1").json() == {"id": 1, "name": "widget"}
    assert client.get("/items/999").status_code == 404


def test_create_item_assigns_next_id(make_app):
    client = TestClient(make_app(AUTH_MODE="none").app)

    resp = client.post("/items", params={"name": "thingamajig"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 3, "name": "thingamajig"}

    resp = client.post("/items", params={"name": "widget-2"})
    assert resp.json()["id"] == 4


def test_jobs_404_when_disabled(make_app):
    client = TestClient(make_app(AUTH_MODE="none").app)
    assert client.post("/jobs", json={"x": 1}).status_code == 404
    assert client.get("/jobs/whatever").status_code == 404


def test_jobs_still_gated_by_auth(make_app):
    client = TestClient(make_app(AUTH_MODE="apikey", API_KEY="k", ASYNC_JOBS="true").app)
    resp = client.post("/jobs", json={"x": 1})
    assert resp.status_code == 401


def test_job_submit_and_poll(make_app):
    client = TestClient(
        make_app(AUTH_MODE="none", ASYNC_JOBS="true", ASYNC_JOB_DELAY_SECONDS="0").app
    )

    submit = client.post("/jobs", json={"x": 1})
    assert submit.status_code == 202
    body = submit.json()
    assert body["status"] == "pending"
    job_id = body["job_id"]
    assert body["poll_url"] == f"/jobs/{job_id}"

    # ASYNC_JOB_DELAY_SECONDS=0 means "pending only while elapsed < 0",
    # i.e. never -- the job resolves to "done" on the very next poll.
    poll = client.get(f"/jobs/{job_id}")
    assert poll.status_code == 200
    assert poll.json() == {"job_id": job_id, "status": "done", "result": {"echoed": {"x": 1}}}


def test_job_pending_before_delay_elapses(make_app):
    client = TestClient(
        make_app(AUTH_MODE="none", ASYNC_JOBS="true", ASYNC_JOB_DELAY_SECONDS="60").app
    )
    job_id = client.post("/jobs", json={"x": 1}).json()["job_id"]
    resp = client.get(f"/jobs/{job_id}")
    assert resp.json() == {"job_id": job_id, "status": "pending"}


def test_job_unknown_id(make_app):
    client = TestClient(make_app(AUTH_MODE="none", ASYNC_JOBS="true").app)
    resp = client.get("/jobs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "job not found"
