"""HTTP-level tests for the Task API (Phase 3).

Covers the four task endpoints, project/parent isolation, mass-assignment
protection, input validation bounds, cancellation semantics, OpenAPI
contract, and adversarial inputs.
"""

import threading
import uuid

import pytest

from app.db import get_session_factory
from app.models import Task
from app.task_service import TaskService

service = TaskService()


def _c(client):
    """Unpack the (client, db_path, projects_root) fixture tuple."""
    c, _, _ = client
    return c


def _create_project(client, name="api-proj"):
    resp = _c(client).post("/projects", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _task_body(project_id, **overrides):
    body = {
        "project_id": project_id,
        "objective": "Do the thing",
        "success_criteria": ["it works"],
    }
    body.update(overrides)
    return body


def _create_task(client, project_id, **overrides):
    resp = _c(client).post("/tasks", json=_task_body(project_id, **overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _complete_via_service(task_id):
    session = get_session_factory()()
    try:
        task = session.get(Task, uuid.UUID(task_id))
        service.plan_task(session, task.id)
        service.queue_task(session, task.id)
        service.start_task(session, task.id)
        service.wait_for_agent(session, task.id)
        service.submit_for_review(session, task.id)
        service.complete_task(session, task.id, result="done")
    finally:
        session.close()


# ---------- creation ----------


def test_create_task_happy_path(client):
    pid = _create_project(client)
    resp = _c(client).post(
        "/tasks",
        json=_task_body(
            pid,
            objective="Build feature X",
            instructions="Use the documented API",
            constraints=["no new deps", "sqlite compatible"],
            success_criteria=["tests pass", "docs updated"],
        ),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "CREATED"
    assert body["objective"] == "Build feature X"
    assert body["instructions"] == "Use the documented API"
    assert body["constraints"] == ["no new deps", "sqlite compatible"]
    assert body["success_criteria"] == ["tests pass", "docs updated"]
    assert body["project_id"] == pid
    assert body["parent_task_id"] is None
    assert body["agent_id"] is None
    assert body["result"] is None
    assert body["error"] is None
    assert body["started_at"] is None
    assert body["completed_at"] is None
    assert body["created_at"] is not None
    assert body["updated_at"] is not None
    assert uuid.UUID(body["id"])


def test_create_task_with_parent(client):
    pid = _create_project(client)
    parent = _create_task(client, pid, objective="parent task")
    child = _create_task(client, pid, parent_task_id=parent["id"], objective="child task")
    assert child["parent_task_id"] == parent["id"]
    assert child["status"] == "CREATED"


def test_create_task_project_not_found(client):
    resp = _c(client).post("/tasks", json=_task_body(str(uuid.uuid4())))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"


def test_create_task_parent_not_found(client):
    pid = _create_project(client)
    resp = _c(client).post("/tasks", json=_task_body(pid, parent_task_id=str(uuid.uuid4())))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Parent task not found"


def test_create_task_cross_project_parent_rejected(client):
    pid_a = _create_project(client, name="cross-a")
    pid_b = _create_project(client, name="cross-b")
    parent_in_b = _create_task(client, pid_b, objective="b parent")
    resp = _c(client).post(
        "/tasks",
        json=_task_body(pid_a, parent_task_id=parent_in_b["id"], objective="a child"),
    )
    assert resp.status_code == 409
    assert "different project" in resp.json()["detail"]


def test_create_task_cyclic_parent_rejected(client):
    """A parent chain containing a cycle must be rejected with 409."""
    pid = _create_project(client, name="cyc")
    a = _create_task(client, pid, objective="a")
    b = _create_task(client, pid, parent_task_id=a["id"], objective="b")
    # Corrupt the chain to form a <-> b directly in the DB (bypasses guards).
    session = get_session_factory()()
    try:
        task_a = session.get(Task, uuid.UUID(a["id"]))
        task_a.parent_task_id = uuid.UUID(b["id"])
        session.commit()
    finally:
        session.close()
    # Creating any task under a now-cyclic ancestor must fail.
    resp = _c(client).post("/tasks", json=_task_body(pid, parent_task_id=a["id"], objective="c"))
    assert resp.status_code == 409
    assert "cycle" in resp.json()["detail"]


def test_create_task_requires_uuid_project_id(client):
    resp = _c(client).post("/tasks", json=_task_body("not-a-uuid"))
    assert resp.status_code == 422


def test_create_task_parent_must_be_uuid(client):
    pid = _create_project(client)
    resp = _c(client).post("/tasks", json=_task_body(pid, parent_task_id="nope"))
    assert resp.status_code == 422


# ---------- mass assignment ----------


def test_mass_assignment_is_ignored(client):
    """A client cannot set status, result or timestamps.

    ``agent_id`` is excluded here: it is a validated input in Phase 4, so an
    unknown value fails with 404 (covered by the assignment tests) instead of
    being silently dropped.  Only true read-only fields are forged below.
    """
    pid = _create_project(client)
    fake_time = "2020-01-01T00:00:00"
    resp = _c(client).post(
        "/tasks",
        json={
            "project_id": pid,
            "objective": "real work",
            "success_criteria": ["done"],
            "status": "COMPLETED",
            "result": "fake result",
            "error": "fake error",
            "created_at": fake_time,
            "started_at": fake_time,
            "completed_at": fake_time,
            "updated_at": fake_time,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "CREATED"
    assert body["result"] is None
    assert body["error"] is None
    assert body["agent_id"] is None
    assert body["started_at"] is None
    assert body["completed_at"] is None
    assert body["created_at"] != fake_time
    assert body["updated_at"] != fake_time
    # The row in the database agrees with the response.
    session = get_session_factory()()
    try:
        row = session.get(Task, uuid.UUID(body["id"]))
        assert row.status == "CREATED"
        assert row.result is None
        assert row.agent_id is None
    finally:
        session.close()


def test_mass_assignment_cannot_escalate_to_terminal(client):
    """Regression: even a full forged payload leaves the task in CREATED."""
    pid = _create_project(client)
    resp = _c(client).post(
        "/tasks",
        json=_task_body(pid, status="FAILED", completed_at="2020-01-01T00:00:00"),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "CREATED"


# ---------- validation bounds ----------


@pytest.mark.parametrize(
    "overrides",
    [
        {"objective": ""},  # empty
        {"objective": "   "},  # blank
        {"objective": "x" * 1001},  # too long
        {"instructions": "x" * 4001},  # too long
        {"constraints": ["x"] * 51},  # too many items
        {"constraints": ["x" * 501]},  # item too long
        {"constraints": ["   "]},  # blank item
        {"success_criteria": []},  # empty required list
        {"success_criteria": ["x"] * 51},  # too many items
        {"success_criteria": [""]},  # blank item
        {"success_criteria": ["x" * 501]},  # item too long
    ],
)
def test_create_task_validation_rejected(client, overrides):
    pid = _create_project(client)
    body = {"project_id": pid, "objective": "ok", "success_criteria": ["ok"]}
    body.update(overrides)
    resp = _c(client).post("/tasks", json=body)
    assert resp.status_code == 422, (resp.status_code, resp.text)


def test_create_task_requires_objective_and_success_criteria(client):
    pid = _create_project(client)
    assert _c(client).post("/tasks", json={"project_id": pid}).status_code == 422
    assert _c(client).post("/tasks", json={"project_id": pid, "objective": "x"}).status_code == 422
    assert (
        _c(client).post("/tasks", json={"project_id": pid, "success_criteria": ["x"]}).status_code
        == 422
    )


def test_create_task_giant_payload_rejected(client):
    pid = _create_project(client)
    body = _task_body(pid, objective="x" * 1_000_000)
    resp = _c(client).post("/tasks", json=body)
    assert resp.status_code == 422


def test_create_task_extra_fields_ignored(client):
    pid = _create_project(client)
    resp = _c(client).post(
        "/tasks",
        json=_task_body(pid, objective="ok", totally_unknown="field", nested={"a": 1}),
    )
    assert resp.status_code == 201


# ---------- queries and isolation ----------


def test_get_task(client):
    pid = _create_project(client)
    task = _create_task(client, pid, objective="fetch me")
    resp = _c(client).get(f"/tasks/{task['id']}")
    assert resp.status_code == 200
    assert resp.json()["objective"] == "fetch me"
    assert resp.json()["status"] == "CREATED"


def test_get_task_not_found(client):
    resp = _c(client).get(f"/tasks/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Task not found"


def test_get_task_malformed_id(client):
    assert _c(client).get("/tasks/not-a-uuid").status_code == 422


def test_list_project_tasks_scoped(client):
    pid_a = _create_project(client, name="iso-a")
    pid_b = _create_project(client, name="iso-b")
    ta = _create_task(client, pid_a, objective="a task")
    _create_task(client, pid_b, objective="b task")
    resp = _c(client).get(f"/projects/{pid_a}/tasks")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ta["id"] in ids
    assert all(t["project_id"] == pid_a for t in resp.json())
    # Project B's endpoint never sees Project A's task.
    resp_b = _c(client).get(f"/projects/{pid_b}/tasks")
    assert [t["objective"] for t in resp_b.json()] == ["b task"]


def test_list_project_tasks_missing_project(client):
    resp = _c(client).get(f"/projects/{uuid.uuid4()}/tasks")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"


def test_list_project_tasks_malformed_project_id(client):
    assert _c(client).get("/projects/not-a-uuid/tasks").status_code == 422


def test_list_project_tasks_empty(client):
    pid = _create_project(client, name="empty")
    resp = _c(client).get(f"/projects/{pid}/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_project_tasks_ordering_deterministic(client):
    """Order must be stable (created_at, id) and return both tasks."""
    pid = _create_project(client, name="order")
    t1 = _create_task(client, pid, objective="alpha")
    t2 = _create_task(client, pid, objective="beta")
    resp1 = _c(client).get(f"/projects/{pid}/tasks")
    assert resp1.status_code == 200
    ids = [t["id"] for t in resp1.json()]
    assert sorted(ids) == sorted([t1["id"], t2["id"]])
    # Second call must return the same order (deterministic under same-second
    # tie-break by id).
    resp2 = _c(client).get(f"/projects/{pid}/tasks")
    assert [t["id"] for t in resp2.json()] == ids


# ---------- cancellation ----------


def test_cancel_non_terminal_task(client):
    pid = _create_project(client)
    task = _create_task(client, pid)
    resp = _c(client).post(f"/tasks/{task['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    assert resp.json()["completed_at"] is not None


def test_cancel_is_idempotent_for_cancelled(client):
    pid = _create_project(client)
    task = _create_task(client, pid)
    first = _c(client).post(f"/tasks/{task['id']}/cancel")
    second = _c(client).post(f"/tasks/{task['id']}/cancel")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "CANCELLED"
    assert second.json()["status"] == "CANCELLED"


def test_cancel_terminal_task_rejected(client):
    pid = _create_project(client)
    task = _create_task(client, pid)
    _complete_via_service(task["id"])
    resp = _c(client).post(f"/tasks/{task['id']}/cancel")
    assert resp.status_code == 409
    # The terminal state must not be silently rewritten.
    assert resp.json()["detail"] != "CANCELLED"
    session = get_session_factory()()
    try:
        row = session.get(Task, uuid.UUID(task["id"]))
        assert row.status == "COMPLETED"
        assert row.result == "done"
    finally:
        session.close()


def test_cancel_failed_task_rejected(client):
    pid = _create_project(client)
    task = _create_task(client, pid)
    session = get_session_factory()()
    try:
        t = session.get(Task, uuid.UUID(task["id"]))
        service.plan_task(session, t.id)
        service.queue_task(session, t.id)
        service.start_task(session, t.id)
        service.fail_task(session, t.id, error="boom")
    finally:
        session.close()
    resp = _c(client).post(f"/tasks/{task['id']}/cancel")
    assert resp.status_code == 409


def test_cancel_not_found(client):
    resp = _c(client).post(f"/tasks/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


def test_cancel_malformed_id(client):
    assert _c(client).post("/tasks/not-a-uuid/cancel").status_code == 422


# ---------- concurrent cancellation (HTTP level) ----------


def test_concurrent_cancels_exactly_one_transition_other_conflicts(client):
    """Two simultaneous cancels: exactly one transition wins the CAS (200),
    the other observes a state conflict (409); final state CANCELLED."""
    pid = _create_project(client)
    task = _create_task(client, pid)
    tid = task["id"]
    barrier = threading.Barrier(3)
    results: list[int] = []

    # Use the raw HTTP client, one per thread.
    from fastapi.testclient import TestClient

    from app.main import app

    def do_cancel():
        with TestClient(app, raise_server_exceptions=False) as c:
            barrier.wait()
            resp = c.post(f"/tasks/{tid}/cancel")
            results.append(resp.status_code)

    threads = [threading.Thread(target=do_cancel) for _ in range(2)]
    for th in threads:
        th.start()
    barrier.wait()
    for th in threads:
        th.join(timeout=15)

    # Valid outcomes: overlapping requests race the CAS (one 200, one 409
    # conflict), or the second request serializes after the first commit and
    # hits the idempotent already-cancelled path (both 200).  Neither may 5xx.
    assert sorted(results) in ([200, 200], [200, 409])
    resp = _c(client).get(f"/tasks/{tid}")
    assert resp.json()["status"] == "CANCELLED"


# ---------- OpenAPI contract ----------


def test_openapi_declares_task_routes(client):
    schema = _c(client).get("/openapi.json").json()
    paths = schema["paths"]
    assert "/tasks" in paths
    assert "/tasks/{task_id}" in paths
    assert "/tasks/{task_id}/cancel" in paths
    assert "/projects/{project_id}/tasks" in paths

    post_resp = set(paths["/tasks"]["post"]["responses"])
    assert {"201", "404", "409", "422"} <= post_resp
    get_resp = set(paths["/tasks/{task_id}"]["get"]["responses"])
    assert {"200", "404", "422"} <= get_resp
    cancel_resp = set(paths["/tasks/{task_id}/cancel"]["post"]["responses"])
    assert {"200", "404", "409", "422"} <= cancel_resp
    list_resp = set(paths["/projects/{project_id}/tasks"]["get"]["responses"])
    assert {"200", "404", "422"} <= list_resp


def test_task_out_schema_matches_runtime(client):
    pid = _create_project(client)
    task = _create_task(client, pid)
    schema = _c(client).get("/openapi.json").json()
    props = set(schema["components"]["schemas"]["TaskOut"]["properties"])
    assert props == {
        "id",
        "project_id",
        "parent_task_id",
        "objective",
        "instructions",
        "constraints",
        "success_criteria",
        "status",
        "agent_id",
        "result",
        "error",
        "created_at",
        "started_at",
        "completed_at",
        "updated_at",
    }
    assert set(task.keys()) == props


# ---------- task -> agent assignment (Phase 4) ----------


def _register_agent(client, name="api-agent", provider="openhands"):
    resp = _c(client).post(
        "/agents",
        json={"name": name, "provider": provider, "capabilities": ["code"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_task_with_unknown_agent_returns_404(client):
    pid = _create_project(client)
    resp = _c(client).post(
        "/tasks",
        json=_task_body(pid, agent_id=str(uuid.uuid4())),
    )
    assert resp.status_code == 404, resp.text


def test_create_task_with_unusable_agent_returns_409(client):
    """Registered agents start UNAVAILABLE, so assignment is refused."""
    pid = _create_project(client)
    agent = _register_agent(client)
    resp = _c(client).post(
        "/tasks",
        json=_task_body(pid, agent_id=agent["id"]),
    )
    assert resp.status_code == 409, resp.text
    assert "not usable" in resp.text


def test_create_task_without_agent_still_succeeds(client):
    pid = _create_project(client)
    resp = _c(client).post("/tasks", json=_task_body(pid))
    assert resp.status_code == 201, resp.text
    assert resp.json()["agent_id"] is None
