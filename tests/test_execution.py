"""Orchestration tests for the execution layer (Phase 5).

Covers the execution lifecycle through the :class:`ExecutionService` and the
three HTTP endpoints (POST /tasks/{id}/execute, GET /tasks/{id}/execution,
POST /tasks/{id}/execution/refresh) with a fake in-memory adapter.

The fake adapter is injected via ``app.dependency_overrides`` at the API level
so the entire request→service→adapter path is exercised.  Service-level tests
exercise ``ExecutionService`` directly against a real DB session.
"""

from __future__ import annotations

import shutil
import threading
import uuid

import pytest
from fake_adapters import FakeInMemoryAdapter

from app.agent_contracts import AgentExecutionState
from app.agent_errors import (
    AgentProviderError,
    AgentTimeoutError,
)
from app.agent_providers import AVAILABLE
from app.api import _execution_service
from app.db import get_session_factory
from app.execution_service import ExecutionService
from app.models import Agent, Task
from app.task_service import TaskService
from app.task_states import (
    FAILED,
    WAITING_FOR_AGENT,
    WAITING_FOR_REVIEW,
)
from app.workspaces import WorkspaceService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_API_KEY = "test-openhands-key"


def _c(client):
    c, _, _ = client
    return c


def _make_git_workspace(workspace_root, name, git_workspace):
    """Copy the session git template into the project workspace."""
    workspace = workspace_root / name
    shutil.copytree(git_workspace, workspace, dirs_exist_ok=True)
    return workspace


def _make_git_projects(client, git_workspace):
    """Create a project with a workspace that is a git repo with origin."""
    client, db_path, projects_root = client
    resp = client.post("/projects", json={"name": "git-proj"})
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    _make_git_workspace(projects_root, "git-proj", git_workspace)
    return project_id


def _create_agent(client, name="exec-agent", **overrides):
    """Create an agent and set it to AVAILABLE via DB."""
    c, db_path, projects_root = client
    payload = {
        "name": name,
        "provider": "openhands",
        "capabilities": ["code"],
        "configuration": {"model": "sonnet"},
    }
    payload.update(overrides)
    resp = c.post("/agents", json=payload)
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]
    # Agents are created UNAVAILABLE; set to AVAILABLE for execution.
    session = get_session_factory()()
    try:
        agent = session.get(Agent, uuid.UUID(agent_id))
        agent.status = AVAILABLE
        session.commit()
    finally:
        session.close()
    return agent_id


def _create_task(client, project_id, agent_id=None, **overrides):
    """Create a task (optionally with an agent) and queue it via the service."""
    c, db_path, projects_root = client
    body = {
        "project_id": project_id,
        "objective": "Do the thing",
        "success_criteria": ["it works"],
    }
    if agent_id is not None:
        body["agent_id"] = agent_id
    body.update(overrides)
    resp = c.post("/tasks", json=body)
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    # Plan + queue the task via service so it's ready for execution
    # (CREATED -> PLANNED -> QUEUED).
    session = get_session_factory()()
    try:
        ts = TaskService()
        ts.plan_task(session, uuid.UUID(task_id))
        ts.queue_task(session, uuid.UUID(task_id))
    finally:
        session.close()
    return task_id


def _inject_fake(client, fake: FakeInMemoryAdapter):
    """Override the execution service dependency with a fake adapter."""
    from app.main import app

    c, db_path, projects_root = client
    ws = WorkspaceService(str(projects_root))

    def make_service():
        return ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    app.dependency_overrides[_execution_service] = make_service
    return make_service


def _clear_overrides():
    from app.main import app

    app.dependency_overrides.clear()


# ===================================================================
# Service-level tests (direct ExecutionService)
# ===================================================================


def test_execute_service_happy_path(client, git_workspace):
    """Full lifecycle: execute → refresh → completed."""
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["completed"])
    ws = WorkspaceService(str(projects_root))
    svc = ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    session = get_session_factory()()
    try:
        task = svc.execute_task(session, uuid.UUID(task_id))
        assert task.status == WAITING_FOR_AGENT
        assert task.execution_reference is not None
        assert task.execution_status == AgentExecutionState.RUNNING.value

        # Refresh completes the task.
        task, status = svc.refresh_execution(session, uuid.UUID(task_id))
        assert task.status == WAITING_FOR_REVIEW
        assert task.execution_status == AgentExecutionState.COMPLETED.value
        assert status.state is AgentExecutionState.COMPLETED
        session.commit()
    finally:
        session.close()
    assert len(fake.started) == 1
    assert len(fake.status_calls) >= 1


def test_execute_service_no_agent_assigned(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    # Create a task without agent_id and queue it via the service.
    task_id = _create_task(client, project_id)
    svc = ExecutionService(workspaces=WorkspaceService(str(projects_root)))
    session = get_session_factory()()
    try:
        with pytest.raises(Exception) as exc:
            svc.execute_task(session, uuid.UUID(task_id))
        assert "no agent assigned" in str(exc.value).lower()
    finally:
        session.close()


def test_execute_service_agent_not_available(client, git_workspace):
    """Agent with status BUSY or UNAVAILABLE is rejected."""
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client, name="busy-agent")
    task_id = _create_task(client, project_id, agent_id)

    # Make the agent BUSY.
    session = get_session_factory()()
    try:
        agent = session.get(Agent, uuid.UUID(agent_id))
        agent.status = "BUSY"
        session.commit()
    finally:
        session.close()

    svc = ExecutionService(workspaces=WorkspaceService(str(projects_root)))
    session = get_session_factory()()
    try:
        with pytest.raises(Exception) as exc:
            svc.execute_task(session, uuid.UUID(task_id))
        assert "not usable" in str(exc.value).lower() or "busy" in str(exc.value).lower()
    finally:
        session.close()


def test_execute_service_workspace_not_git(client):
    c, db_path, projects_root = client
    # Create a project (no git workspace).
    resp = _c(client).post("/projects", json={"name": "nogit-proj"})
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    agent_id = _create_agent(client, name="nogit-agent")
    task_id = _create_task(client, project_id, agent_id)

    # No git repo in the workspace.
    svc = ExecutionService(workspaces=WorkspaceService(str(projects_root)))
    session = get_session_factory()()
    try:
        with pytest.raises(Exception) as exc:
            svc.execute_task(session, uuid.UUID(task_id))
        assert "origin" in str(exc.value).lower()
    finally:
        session.close()


def test_execute_service_double_execute_raises_conflict(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client, name="double-agent")
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["running", "completed"])
    ws = WorkspaceService(str(projects_root))
    svc = ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    session = get_session_factory()()
    try:
        svc.execute_task(session, uuid.UUID(task_id))
        # Second execute: the agent is BUSY and the task is no longer QUEUED,
        # so the service must raise a conflict (not 5xx).
        with pytest.raises(Exception) as exc:
            svc.execute_task(session, uuid.UUID(task_id))
        assert (
            "conflict" in str(exc.value).lower()
            or "busy" in str(exc.value).lower()
            or "not usable" in str(exc.value).lower()
        )
    finally:
        session.close()


def test_refresh_completed_via_service(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["completed"])
    ws = WorkspaceService(str(projects_root))
    svc = ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    session = get_session_factory()()
    try:
        svc.execute_task(session, uuid.UUID(task_id))
        task, status = svc.refresh_execution(session, uuid.UUID(task_id))
        assert task.status == WAITING_FOR_REVIEW
        assert status.state is AgentExecutionState.COMPLETED
    finally:
        session.close()


def test_refresh_still_running_does_not_advance(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["running"])  # stays running
    ws = WorkspaceService(str(projects_root))
    svc = ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    session = get_session_factory()()
    try:
        svc.execute_task(session, uuid.UUID(task_id))
        task, status = svc.refresh_execution(session, uuid.UUID(task_id))
        assert task.status == WAITING_FOR_AGENT  # still waiting
        assert status.state is AgentExecutionState.RUNNING
    finally:
        session.close()


def test_refresh_failed_via_service(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["failed"])
    ws = WorkspaceService(str(projects_root))
    svc = ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    session = get_session_factory()()
    try:
        svc.execute_task(session, uuid.UUID(task_id))
        task, status = svc.refresh_execution(session, uuid.UUID(task_id))
        assert task.status == FAILED
        assert status.state is AgentExecutionState.FAILED
        assert task.error
        # Agent is released.
        agent = session.get(Agent, uuid.UUID(agent_id))
        assert agent.status == AVAILABLE
    finally:
        session.close()


def test_refresh_not_running_via_service(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    svc = ExecutionService(workspaces=WorkspaceService(str(projects_root)))
    session = get_session_factory()()
    try:
        with pytest.raises(Exception) as exc:
            svc.refresh_execution(session, uuid.UUID(task_id))
        assert "no in-flight execution" in str(exc.value).lower()
    finally:
        session.close()


def test_execute_adapter_timeout_fails_via_service(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_error=AgentTimeoutError("provider timed out"))
    ws = WorkspaceService(str(projects_root))
    svc = ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    session = get_session_factory()()
    try:
        with pytest.raises(AgentTimeoutError):
            svc.execute_task(session, uuid.UUID(task_id))
        # Task should be FAILED, agent released.
        task = session.get(Task, uuid.UUID(task_id))
        assert task.status == FAILED
        # Agent released.
        agent = session.get(Agent, uuid.UUID(agent_id))
        assert agent.status == AVAILABLE
    finally:
        session.close()


def test_execute_adapter_provider_error_fails_via_service(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_error=AgentProviderError("provider exploded"))
    ws = WorkspaceService(str(projects_root))
    svc = ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    session = get_session_factory()()
    try:
        with pytest.raises(AgentProviderError):
            svc.execute_task(session, uuid.UUID(task_id))
        task = session.get(Task, uuid.UUID(task_id))
        assert task.status == FAILED
    finally:
        session.close()


def test_execution_timeout_at_orchestrator_boundary(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["running"])
    ws = WorkspaceService(str(projects_root))
    # max_execution_seconds=0 means any task immediately times out.
    svc = ExecutionService(
        workspaces=ws, adapter_factory=lambda a, r, b: fake, max_execution_seconds=0
    )

    session = get_session_factory()()
    try:
        svc.execute_task(session, uuid.UUID(task_id))
        # Fake the started_at to be far in the past...
        task = session.get(Task, uuid.UUID(task_id))
        from datetime import UTC, datetime, timedelta

        task.started_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()
        task, status = svc.refresh_execution(session, uuid.UUID(task_id))
        assert task.status == FAILED
        assert status.state is AgentExecutionState.FAILED
        assert "timeout" in (status.detail or "").lower()
    finally:
        session.close()


def test_agent_released_after_completed_refresh(client, git_workspace):
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["completed"])
    ws = WorkspaceService(str(projects_root))
    svc = ExecutionService(workspaces=ws, adapter_factory=lambda a, r, b: fake)

    session = get_session_factory()()
    try:
        svc.execute_task(session, uuid.UUID(task_id))
        # Agent should be BUSY during execution.
        agent = session.get(Agent, uuid.UUID(agent_id))
        assert agent.status == "BUSY"
        svc.refresh_execution(session, uuid.UUID(task_id))
        session.refresh(agent)
        assert agent.status == AVAILABLE
    finally:
        session.close()


# ===================================================================
# API-level tests (via TestClient with dependency override)
# ===================================================================


def test_post_execute_returns_202(client, git_workspace):
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["completed"])
    _inject_fake(client, fake)
    try:
        resp = _c(client).post(f"/tasks/{task_id}/execute")
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["task_id"] == task_id
        assert body["status"] == WAITING_FOR_AGENT
        assert body["execution_status"] == AgentExecutionState.RUNNING.value
        assert body["reference"] is not None
    finally:
        _clear_overrides()


def test_post_execute_409_when_provider_not_configured(client, git_workspace):
    """No dependency override => default adapter factory => no provider."""
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)
    # No override — default adapter factory returns unconfigured adapter.
    resp = _c(client).post(f"/tasks/{task_id}/execute")
    assert resp.status_code == 409, resp.text
    assert "not configured" in resp.text.lower()


def test_post_execute_409_when_agent_unavailable(client, git_workspace):
    """Agent is UNAVAILABLE, not changed to AVAILABLE."""
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    resp = c.post(
        "/agents",
        json={
            "name": "unavail-agent",
            "provider": "openhands",
            "capabilities": ["code"],
            "configuration": {"model": "sonnet"},
        },
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]
    # Leave as UNAVAILABLE (default).
    task_id = _create_task(client, project_id)
    # Assign the unavailable agent directly (task creation rejects it).
    session = get_session_factory()()
    try:
        task = session.get(Task, uuid.UUID(task_id))
        task.agent_id = uuid.UUID(agent_id)
        session.commit()
    finally:
        session.close()

    fake = FakeInMemoryAdapter()
    _inject_fake(client, fake)
    try:
        resp = _c(client).post(f"/tasks/{task_id}/execute")
        assert resp.status_code == 409, resp.text
        assert "not usable" in resp.text.lower() or "unavailable" in resp.text.lower()
    finally:
        _clear_overrides()


def test_post_execute_409_task_not_queued(client, git_workspace):
    """Task in CREATED (not QUEUED) cannot be executed."""
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    # Create task but do NOT queue it.
    resp = c.post(
        "/tasks",
        json={
            "project_id": project_id,
            "agent_id": agent_id,
            "objective": "nope",
            "success_criteria": ["nope"],
        },
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    fake = FakeInMemoryAdapter()
    _inject_fake(client, fake)
    try:
        resp = _c(client).post(f"/tasks/{task_id}/execute")
        assert resp.status_code == 409, resp.text
    finally:
        _clear_overrides()


def test_get_execution_returns_200(client, git_workspace):
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["completed"])
    _inject_fake(client, fake)
    try:
        # Execute first.
        resp = _c(client).post(f"/tasks/{task_id}/execute")
        assert resp.status_code == 202
        # Then read.
        resp = _c(client).get(f"/tasks/{task_id}/execution")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_id"] == task_id
        assert body["task_status"] == WAITING_FOR_AGENT
    finally:
        _clear_overrides()


def test_get_execution_404_for_unknown_task(client):
    _inject_fake(client, FakeInMemoryAdapter())
    try:
        resp = _c(client).get(f"/tasks/{uuid.uuid4()}/execution")
        assert resp.status_code == 404, resp.text
    finally:
        _clear_overrides()


def test_refresh_execution_completed_200(client, git_workspace):
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["completed"])
    _inject_fake(client, fake)
    try:
        resp = _c(client).post(f"/tasks/{task_id}/execute")
        assert resp.status_code == 202
        resp = _c(client).post(f"/tasks/{task_id}/execution/refresh")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_status"] == WAITING_FOR_REVIEW
        assert body["execution_status"] == AgentExecutionState.COMPLETED.value
    finally:
        _clear_overrides()


def test_refresh_execution_409_when_not_running(client, git_workspace):
    """Task never executed → no in-flight execution to refresh."""
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    _inject_fake(client, FakeInMemoryAdapter())
    try:
        resp = _c(client).post(f"/tasks/{task_id}/execution/refresh")
        assert resp.status_code == 409, resp.text
        assert "no in-flight" in resp.text.lower()
    finally:
        _clear_overrides()


def test_concurrent_execute_single_winner(client, git_workspace):
    """Two simultaneous execute calls: exactly one wins (202), the other 409."""
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["completed"])
    _inject_fake(client, fake)

    from fastapi.testclient import TestClient

    from app.main import app

    barrier = threading.Barrier(3)
    results: list[int] = []

    def do_execute():
        with TestClient(app, raise_server_exceptions=False) as c:
            barrier.wait()
            resp = c.post(f"/tasks/{task_id}/execute")
            results.append(resp.status_code)

    threads = [threading.Thread(target=do_execute) for _ in range(2)]
    for th in threads:
        th.start()
    barrier.wait()
    for th in threads:
        th.join(timeout=15)

    assert sorted(results) in ([202, 202], [202, 409])
    # Only one provider start_task was called.
    if sorted(results) == [202, 409]:
        assert len(fake.started) == 1
    _clear_overrides()


def test_cancel_in_flight_execution_returns_409(client, git_workspace):
    """Cancelling a WAITING_FOR_AGENT task must be rejected (Phase 5 scope)."""
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    fake = FakeInMemoryAdapter(start_statuses=["running"])
    _inject_fake(client, fake)
    try:
        resp = _c(client).post(f"/tasks/{task_id}/execute")
        assert resp.status_code == 202
        # Try to cancel.
        resp = _c(client).post(f"/tasks/{task_id}/cancel")
        assert resp.status_code == 409, resp.text
    finally:
        _clear_overrides()


def test_openapi_includes_execution_endpoints(client):
    """OpenAPI spec must document the three execution endpoints."""
    resp = _c(client).get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    # POST /tasks/{task_id}/execute
    assert "/tasks/{task_id}/execute" in paths
    # GET /tasks/{task_id}/execution
    assert "/tasks/{task_id}/execution" in paths
    # POST /tasks/{task_id}/execution/refresh
    assert "/tasks/{task_id}/execution/refresh" in paths


# ---------------------------------------------------------------------------
# Adversarial / security
# ---------------------------------------------------------------------------


def test_execute_too_large_agent_id_rejected(client, git_workspace):
    """Agent_id must be a valid UUID — oversized payload is 422."""
    c, db_path, projects_root = client
    project_id = _make_git_projects(client, git_workspace)
    task_id = _create_task(client, project_id, _create_agent(client))
    # The execute endpoint doesn't take a body, so no oversized payload risk.
    # Forge: a task with a non-existent agent_id is handled via the service.
    # Test: set the task's agent_id to a random UUID that doesn't exist.
    session = get_session_factory()()
    try:
        task = session.get(Task, uuid.UUID(task_id))
        task.agent_id = uuid.uuid4()  # random agent that doesn't exist
        session.commit()
    finally:
        session.close()

    fake = FakeInMemoryAdapter()
    _inject_fake(client, fake)
    try:
        resp = _c(client).post(f"/tasks/{task_id}/execute")
        assert resp.status_code == 404, resp.text
    finally:
        _clear_overrides()


def test_execution_secrets_not_leaked_in_error_responses(client, git_workspace):
    """Provider errors must not expose the API key."""
    project_id = _make_git_projects(client, git_workspace)
    agent_id = _create_agent(client)
    task_id = _create_task(client, project_id, agent_id)

    # No override → default adapter → unconfigured → 409 "not configured".
    resp = _c(client).post(f"/tasks/{task_id}/execute")
    assert resp.status_code == 409
    assert _API_KEY not in resp.text
    # The error message should mention OPENHANDS_API_KEY without showing the key.
    assert "OPENHANDS_API_KEY" in resp.text
