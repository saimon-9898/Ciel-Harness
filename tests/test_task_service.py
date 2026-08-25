"""Service-level tests for the Task engine.

These exercise TaskService directly: creation, parent validation (including
cycles and cross-project parents), state-machine-guarded transitions, and
concurrent transition safety (compare-and-swap).
"""

import threading
import uuid

import pytest

from app.db import get_session_factory
from app.models import Project, Task
from app.task_service import (
    InvalidParentTaskError,
    ParentTaskNotFoundError,
    ProjectNotFoundError,
    TaskNotFoundError,
    TaskService,
    TaskStateConflictError,
)
from app.task_states import (
    CANCELLED,
    COMPLETED,
    CREATED,
    FAILED,
    RUNNING,
    TaskStateError,
)

service = TaskService()


def _create_project(session, name: str = "svc-proj") -> Project:
    project = Project(name=name)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _new_session():
    return get_session_factory()()


def _task_payload(project_id, **overrides):
    payload = {
        "project_id": project_id,
        "parent_task_id": None,
        "objective": "objective",
        "instructions": None,
        "constraints": None,
        "success_criteria": ["criterion"],
    }
    payload.update(overrides)
    return payload


# ---------- creation ----------


def test_create_task_uses_server_controlled_defaults(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    try:
        assert task.status == CREATED
        assert task.agent_id is None
        assert task.result is None
        assert task.error is None
        assert task.started_at is None
        assert task.completed_at is None
        assert task.created_at is not None
        assert task.updated_at is not None
        assert task.parent_task_id is None
    finally:
        session.close()


def test_create_task_persists_all_fields(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(
        session,
        **_task_payload(
            project.id,
            objective="Ship the engine",
            instructions="Write it in Python",
            constraints=["no new deps", "keep tests green"],
            success_criteria=["tests pass", "docs updated"],
        ),
    )
    try:
        fresh = session.get(Task, task.id)
        assert fresh.objective == "Ship the engine"
        assert fresh.instructions == "Write it in Python"
        assert fresh.constraints == ["no new deps", "keep tests green"]
        assert fresh.success_criteria == ["tests pass", "docs updated"]
        assert fresh.project_id == project.id
        assert fresh.status == CREATED
    finally:
        session.close()


def test_create_task_requires_existing_project(client):
    session = _new_session()
    try:
        with pytest.raises(ProjectNotFoundError):
            service.create_task(session, **_task_payload(uuid.uuid4()))
    finally:
        session.close()


def test_create_task_id_is_server_controlled(client):
    """id must never be client-supplied: create_task has no id parameter."""
    session = _new_session()
    project = _create_project(session)
    with pytest.raises(TypeError):
        service.create_task(
            session,
            id=uuid.uuid4(),
            **_task_payload(project.id),
        )
    session.close()

    # The server assigns a fresh UUID on every create.
    session = _new_session()
    project = _create_project(session, name="svc-proj-2")
    task = service.create_task(session, **_task_payload(project.id))
    try:
        assert isinstance(task.id, uuid.UUID)
    finally:
        session.close()


# ---------- parent validation ----------


def test_create_task_with_valid_parent(client):
    session = _new_session()
    project = _create_project(session)
    parent = service.create_task(session, **_task_payload(project.id, objective="parent"))
    child = service.create_task(
        session, **_task_payload(project.id, parent_task_id=parent.id, objective="child")
    )
    try:
        assert child.parent_task_id == parent.id
    finally:
        session.close()


def test_create_task_parent_not_found(client):
    session = _new_session()
    project = _create_project(session)
    try:
        with pytest.raises(ParentTaskNotFoundError):
            service.create_task(session, **_task_payload(project.id, parent_task_id=uuid.uuid4()))
    finally:
        session.close()


def test_create_task_cross_project_parent_rejected(client):
    session = _new_session()
    project_a = _create_project(session, name="proj-a")
    project_b = _create_project(session, name="proj-b")
    parent_in_b = service.create_task(session, **_task_payload(project_b.id, objective="b-parent"))
    try:
        with pytest.raises(InvalidParentTaskError):
            service.create_task(
                session,
                **_task_payload(project_a.id, parent_task_id=parent_in_b.id, objective="a-child"),
            )
    finally:
        session.close()


def test_self_parent_rejected(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    try:
        with pytest.raises(InvalidParentTaskError):
            service._validate_parent(session, task, task.id)
    finally:
        session.close()


def test_cycle_detection_rejects_walk_that_revisits(client):
    """A pre-existing corrupt cycle (A <-> B) must be detected when walking."""
    session = _new_session()
    project = _create_project(session)
    a = service.create_task(session, **_task_payload(project.id, objective="a"))
    b = service.create_task(
        session, **_task_payload(project.id, parent_task_id=a.id, objective="b")
    )
    # Corrupt the chain directly (bypassing the guard) to form A <-> B.
    a.parent_task_id = b.id
    session.commit()
    try:
        with pytest.raises(InvalidParentTaskError):
            service._assert_no_cycle(session, uuid.uuid4(), a.id)
    finally:
        session.close()


def test_cycle_detection_accepts_deep_valid_chain(client):
    session = _new_session()
    project = _create_project(session)
    a = service.create_task(session, **_task_payload(project.id, objective="a"))
    b = service.create_task(
        session, **_task_payload(project.id, parent_task_id=a.id, objective="b")
    )
    c = service.create_task(
        session, **_task_payload(project.id, parent_task_id=b.id, objective="c")
    )
    try:
        # A fresh task whose parent is C walks C -> B -> A -> None: no cycle.
        service._assert_no_cycle(session, uuid.uuid4(), c.id)
    finally:
        session.close()


# ---------- transitions ----------


def test_full_lifecycle_with_timestamps(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    try:
        task = service.plan_task(session, task.id)
        assert task.status == "PLANNED"
        task = service.queue_task(session, task.id)
        assert task.status == "QUEUED"
        task = service.start_task(session, task.id)
        assert task.status == RUNNING
        assert task.started_at is not None
        task = service.wait_for_agent(session, task.id)
        assert task.status == "WAITING_FOR_AGENT"
        task = service.resume_task(session, task.id)
        assert task.status == RUNNING
        task = service.wait_for_agent(session, task.id)
        task = service.submit_for_review(session, task.id)
        assert task.status == "WAITING_FOR_REVIEW"
        task = service.complete_task(session, task.id, result="done well")
        assert task.status == COMPLETED
        assert task.result == "done well"
        assert task.completed_at is not None
        assert task.started_at is not None
    finally:
        session.close()


def test_approval_path(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    try:
        for op in (
            lambda t: service.plan_task(session, t),
            lambda t: service.queue_task(session, t),
            lambda t: service.start_task(session, t),
            lambda t: service.wait_for_agent(session, t),
            lambda t: service.submit_for_review(session, t),
            lambda t: service.request_approval(session, t),
        ):
            task = op(task.id)
        assert task.status == "WAITING_FOR_APPROVAL"
        task = service.approve_task(session, task.id)
        assert task.status == RUNNING
    finally:
        session.close()


def test_fail_sets_error_and_completed_at(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    try:
        for op in (
            lambda t: service.plan_task(session, t),
            lambda t: service.queue_task(session, t),
            lambda t: service.start_task(session, t),
        ):
            task = op(task.id)
        task = service.fail_task(session, task.id, error="agent crashed")
        assert task.status == FAILED
        assert task.error == "agent crashed"
        assert task.completed_at is not None
    finally:
        session.close()


def test_invalid_transition_leaves_state_unchanged(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    try:
        # COMPLETED -> anything must fail and leave COMPLETED intact.
        task = service.plan_task(session, task.id)
        task = service.queue_task(session, task.id)
        task = service.start_task(session, task.id)
        task = service.wait_for_agent(session, task.id)
        task = service.submit_for_review(session, task.id)
        task = service.complete_task(session, task.id, result="done")
        assert task.status == COMPLETED
        with pytest.raises(TaskStateError):
            service.fail_task(session, task.id, error="too late")
        with pytest.raises(TaskStateError):
            service.cancel_task(session, task.id)
        fresh = session.get(Task, task.id)
        assert fresh.status == COMPLETED
    finally:
        session.close()


def test_transition_on_missing_task_raises_not_found(client):
    session = _new_session()
    try:
        with pytest.raises(TaskNotFoundError):
            service.plan_task(session, uuid.uuid4())
    finally:
        session.close()


def test_cancel_is_idempotent_for_cancelled(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    try:
        cancelled = service.cancel_task(session, task.id)
        assert cancelled.status == CANCELLED
        again = service.cancel_task(session, task.id)
        assert again.status == CANCELLED
    finally:
        session.close()


def test_cancel_rejected_for_completed(client):
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    try:
        for op in (
            lambda t: service.plan_task(session, t),
            lambda t: service.queue_task(session, t),
            lambda t: service.start_task(session, t),
            lambda t: service.wait_for_agent(session, t),
            lambda t: service.submit_for_review(session, t),
            lambda t: service.complete_task(session, t, result="done"),
        ):
            task = op(task.id)
        assert task.status == COMPLETED
        with pytest.raises(TaskStateError):
            service.cancel_task(session, task.id)
    finally:
        session.close()


# ---------- queries ----------


def test_list_project_tasks_is_scoped(client):
    session = _new_session()
    project_a = _create_project(session, name="scoped-a")
    project_b = _create_project(session, name="scoped-b")
    try:
        ta1 = service.create_task(session, **_task_payload(project_a.id, objective="a1"))
        ta2 = service.create_task(session, **_task_payload(project_a.id, objective="a2"))
        service.create_task(session, **_task_payload(project_b.id, objective="b1"))
        tasks_a = service.list_project_tasks(session, project_a.id)
        assert {t.id for t in tasks_a} == {ta1.id, ta2.id}
        tasks_b = service.list_project_tasks(session, project_b.id)
        assert [t.objective for t in tasks_b] == ["b1"]
    finally:
        session.close()


# ---------- concurrency ----------


def test_concurrent_transitions_exactly_one_wins(client):
    """Two threads race RUNNING -> CANCELLED vs RUNNING -> FAILED.

    The compare-and-swap UPDATE guarantees exactly one succeeds; the loser
    gets TaskStateConflictError and the DB ends in exactly one terminal state.
    """
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    tid = task.id
    for op in (
        lambda t: service.plan_task(session, t),
        lambda t: service.queue_task(session, t),
        lambda t: service.start_task(session, t),
    ):
        op(tid)
    session.close()

    results: dict[str, str] = {}
    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def worker(name, transition):
        s = _new_session()
        try:
            barrier.wait()
            transition(s, tid)
            results[name] = "ok"
        except TaskStateConflictError as exc:
            errors.append(exc)
            results[name] = "conflict"
        except TaskStateError as exc:
            # The other thread already transitioned the task to a terminal state
            # before we read it.  Record as a conflict — the CAS protected us.
            errors.append(exc)
            results[name] = "conflict"
        finally:
            s.close()

    t1 = threading.Thread(target=worker, args=("cancel", lambda s, t: service.cancel_task(s, t)))
    t2 = threading.Thread(
        target=worker, args=("fail", lambda s, t: service.fail_task(s, t, error="e"))
    )
    t1.start()
    t2.start()
    barrier.wait()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert sorted(results.values()) == ["conflict", "ok"]
    final_session = _new_session()
    try:
        final = final_session.get(Task, tid)
        assert final.status in {CANCELLED, FAILED}
        assert final.completed_at is not None
        # Exactly one terminal transition applied: status is a single value
        # and the other worker observed the conflict.
        assert len(errors) == 1
    finally:
        final_session.close()


def test_concurrent_duplicate_cancels_are_safe(client):
    """Two simultaneous cancels both succeed; exactly one row transition."""
    session = _new_session()
    project = _create_project(session)
    task = service.create_task(session, **_task_payload(project.id))
    tid = task.id
    session.close()

    barrier = threading.Barrier(3)
    statuses: list[str] = []

    def do_cancel():
        s = _new_session()
        try:
            barrier.wait()
            cancelled = service.cancel_task(s, tid)
            statuses.append(cancelled.status)
        finally:
            s.close()

    threads = [threading.Thread(target=do_cancel) for _ in range(2)]
    for th in threads:
        th.start()
    barrier.wait()
    for th in threads:
        th.join(timeout=15)

    final_session = _new_session()
    try:
        final = final_session.get(Task, tid)
        assert final.status == CANCELLED
        assert all(s == CANCELLED for s in statuses)
    finally:
        final_session.close()
