"""Task service (Phase 3): creation, querying, and state-machine transitions.

Every status change goes through the deterministic state machine in
``task_states`` and is applied with a compare-and-swap UPDATE, so two
concurrent transitions of the same task cannot both succeed: exactly one wins
and the loser receives ``TaskStateConflictError``. No code path in this module
sets ``status`` directly on an object and commits without that guard.

Phase 3 performs no agent execution. ``agent_id`` is stored for future use and
never populated here.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Project, Task
from .task_states import (
    CANCELLED,
    COMPLETED,
    FAILED,
    RUNNING,
    TERMINAL_STATES,
    validate_transition,
)

logger = logging.getLogger(__name__)


class TaskError(Exception):
    """Base class for Task engine errors."""


class ProjectNotFoundError(TaskError):
    """The referenced project does not exist."""


class TaskNotFoundError(TaskError):
    """The referenced task does not exist."""


class ParentTaskNotFoundError(TaskError):
    """The referenced parent task does not exist."""


class InvalidParentTaskError(TaskError):
    """The parent relationship is invalid (cross-project, self-parent, cycle)."""


class TaskStateConflictError(TaskError):
    """The transition lost a concurrent compare-and-swap race."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


class TaskService:
    """The Task engine: create, query, transition, and cancel tasks."""

    def create_task(
        self,
        session: Session,
        *,
        project_id: uuid.UUID,
        parent_task_id: uuid.UUID | None,
        objective: str,
        instructions: str | None,
        constraints: list[str] | None,
        success_criteria: list[str],
    ) -> Task:
        """Create a task in state CREATED under an existing project."""
        if session.get(Project, project_id) is None:
            raise ProjectNotFoundError(f"project {project_id} does not exist")

        task = Task(
            id=uuid.uuid4(),
            project_id=project_id,
            parent_task_id=parent_task_id,
            objective=objective,
            instructions=instructions,
            constraints=constraints,
            success_criteria=success_criteria,
            status="CREATED",
        )
        if parent_task_id is not None:
            self._validate_parent(session, task, parent_task_id)

        session.add(task)
        session.commit()
        session.refresh(task)
        logger.info(
            "task created",
            extra={
                "task_id": str(task.id),
                "project_id": str(task.project_id),
                "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
                "status": task.status,
            },
        )
        return task

    def get_task(self, session: Session, task_id: uuid.UUID) -> Task:
        """Return a task by id or raise TaskNotFoundError."""
        task = session.get(Task, task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} does not exist")
        return task

    def list_project_tasks(self, session: Session, project_id: uuid.UUID) -> list[Task]:
        """Return all tasks of a project, ordered deterministically."""
        stmt = select(Task).where(Task.project_id == project_id).order_by(Task.created_at, Task.id)
        return list(session.scalars(stmt))

    # ---- explicit state transitions (all state-machine guarded) ----

    def plan_task(self, session: Session, task_id: uuid.UUID) -> Task:
        return self._transition(session, task_id, "PLANNED")

    def queue_task(self, session: Session, task_id: uuid.UUID) -> Task:
        return self._transition(session, task_id, "QUEUED")

    def start_task(self, session: Session, task_id: uuid.UUID) -> Task:
        return self._transition(session, task_id, RUNNING)

    def wait_for_agent(self, session: Session, task_id: uuid.UUID) -> Task:
        return self._transition(session, task_id, "WAITING_FOR_AGENT")

    def resume_task(self, session: Session, task_id: uuid.UUID) -> Task:
        return self._transition(session, task_id, RUNNING)

    def submit_for_review(self, session: Session, task_id: uuid.UUID) -> Task:
        return self._transition(session, task_id, "WAITING_FOR_REVIEW")

    def request_approval(self, session: Session, task_id: uuid.UUID) -> Task:
        return self._transition(session, task_id, "WAITING_FOR_APPROVAL")

    def approve_task(self, session: Session, task_id: uuid.UUID) -> Task:
        return self._transition(session, task_id, RUNNING)

    def complete_task(self, session: Session, task_id: uuid.UUID, *, result: str) -> Task:
        return self._transition(session, task_id, COMPLETED, result=result)

    def fail_task(self, session: Session, task_id: uuid.UUID, *, error: str) -> Task:
        return self._transition(session, task_id, FAILED, error=error)

    def cancel_task(self, session: Session, task_id: uuid.UUID) -> Task:
        """Cancel a task through the state machine.

        Already-cancelled tasks are returned unchanged (idempotent). Terminal
        states COMPLETED and FAILED cannot be cancelled: the state machine
        rejects the transition and ``TaskStateError`` propagates.
        """
        task = session.get(Task, task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} does not exist")
        if task.status == CANCELLED:
            return task
        return self._transition(session, task_id, CANCELLED)

    # ---- internals ----

    def _transition(
        self,
        session: Session,
        task_id: uuid.UUID,
        new_status: str,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> Task:
        """Apply ``new_status`` atomically unless the task changed underneath us."""
        task = session.get(Task, task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} does not exist")

        # Deterministic guard: this raises before any write for illegal moves.
        validate_transition(task.status, new_status)
        from_status = task.status

        values: dict = {"status": new_status, "updated_at": _now_utc()}
        if new_status == RUNNING and task.started_at is None:
            values["started_at"] = _now_utc()
        if new_status in TERMINAL_STATES:
            values["completed_at"] = _now_utc()
        if result is not None:
            values["result"] = result
        if error is not None:
            values["error"] = error

        # Compare-and-swap: only applies when the row still has the status we
        # validated against. A concurrent transition makes rowcount 0.
        stmt = update(Task).where(Task.id == task_id, Task.status == task.status).values(**values)
        rowcount = session.execute(stmt).rowcount
        session.commit()
        if rowcount != 1:
            session.refresh(task)
            raise TaskStateConflictError(
                f"task {task_id} state changed concurrently (now {task.status!r})"
            )

        session.refresh(task)
        logger.info(
            "task transition",
            extra={
                "task_id": str(task_id),
                "project_id": str(task.project_id),
                "from_status": from_status,
                "to_status": new_status,
            },
        )
        return task

    def _validate_parent(self, session: Session, task: Task, parent_task_id: uuid.UUID) -> None:
        """Enforce parent-task rules: exists, same project, no self/cycle."""
        parent = session.get(Task, parent_task_id)
        if parent is None:
            raise ParentTaskNotFoundError(f"parent task {parent_task_id} does not exist")
        if parent.project_id != task.project_id:
            raise InvalidParentTaskError("parent task belongs to a different project")
        self._assert_no_cycle(session, task.id, parent_task_id)

    def _assert_no_cycle(
        self, session: Session, task_id: uuid.UUID, parent_task_id: uuid.UUID
    ) -> None:
        """Walk up the parent chain and reject self-parenting or cycles.

        ``task_id`` is the candidate task (not yet a parent of anything); the
        walk collects every ancestor id. Reaching ``task_id`` or revisiting an
        ancestor means the relationship would create a cycle.
        """
        seen: set[uuid.UUID] = set()
        current: uuid.UUID | None = parent_task_id
        while current is not None:
            if current == task_id or current in seen:
                raise InvalidParentTaskError("parent relationship would create a cycle")
            seen.add(current)
            current = session.scalar(select(Task.parent_task_id).where(Task.id == current))
