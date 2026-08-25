"""Projects, Tasks, and Agents API routers (Phase 2/3/4).

Projects (Phase 2): management endpoints; workspaces are created eagerly on
project creation; the WorkspaceService guarantees filesystem isolation.
Tasks (Phase 3): task engine endpoints; all status changes are guarded by the
deterministic state machine.
Agents (Phase 4): agent registry endpoints; adapters report not-configured and
no execution endpoint exists.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .agent_contracts import AgentHealth
from .agent_errors import (
    AgentCancellationError,
    AgentError,
    AgentMalformedResponseError,
    AgentNameConflictError,
    AgentNotFoundError,
    AgentProviderError,
    AgentTimeoutError,
    AgentUnavailableError,
    InvalidAgentConfigurationError,
    ProviderNotConfiguredError,
    UnsupportedProviderError,
)
from .agent_manager import AgentManager
from .config import get_settings
from .db import get_session
from .execution_service import (
    ExecutionConflictError,
    ExecutionError,
    ExecutionNotRunningError,
    ExecutionService,
)
from .models import Agent, Project, Task
from .schemas import (
    AgentCreate,
    AgentHealthOut,
    AgentOut,
    ProjectCreate,
    ProjectOut,
    TaskCreate,
    TaskExecuteOut,
    TaskExecutionOut,
    TaskOut,
)
from .task_service import (
    InvalidParentTaskError,
    ParentTaskNotFoundError,
    ProjectNotFoundError,
    TaskError,
    TaskNotFoundError,
    TaskService,
    TaskStateConflictError,
)
from .task_states import WAITING_FOR_AGENT, TaskStateError
from .workspaces import WorkspaceError, WorkspaceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


def _workspace_service() -> WorkspaceService:
    return WorkspaceService(get_settings().workspaces_root)


def _get_project_or_404(session: Session, project_id: uuid.UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


_ERROR_RESPONSES = {
    400: {"description": "Bad request (e.g. workspace path rejected)"},
    409: {"description": "Project name already exists"},
    404: {"description": "Project not found"},
}


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        "400": _ERROR_RESPONSES[400],
        "409": _ERROR_RESPONSES[409],
        "422": {"description": "Validation error"},
    },
)
def create_project(
    payload: ProjectCreate,
    session: Session = Depends(get_session),
    workspaces: WorkspaceService = Depends(_workspace_service),
) -> Project:
    """Create a project and its isolated workspace directory."""
    existing = session.scalar(select(Project).where(Project.name == payload.name))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project name already exists",
        )

    project = Project(
        name=payload.name,
        repository_url=payload.repository_url,
        repository_path=payload.repository_path,
        default_branch=payload.default_branch,
    )
    try:
        workspaces.create_workspace(project)
        session.add(project)
        session.commit()
    except WorkspaceError as exc:
        logger.error("failed to create workspace for project %r: %s", payload.name, exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except IntegrityError as exc:
        # Race: another request inserted the same unique name between the
        # pre-check and this commit. Roll back and report a clean conflict.
        session.rollback()
        workspaces.remove_workspace(project)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project name already exists",
        ) from exc
    except Exception:
        session.rollback()
        workspaces.remove_workspace(project)
        raise
    session.refresh(project)
    logger.info(
        "project created",
        extra={"project_id": str(project.id), "project_name": project.name},
    )
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(session: Session = Depends(get_session)) -> list[Project]:
    """List all projects, ordered by creation time then name."""
    return list(session.scalars(select(Project).order_by(Project.created_at, Project.name)))


@router.get(
    "/{project_id}",
    response_model=ProjectOut,
    responses={
        "404": _ERROR_RESPONSES[404],
        "422": {"description": "Validation error"},
    },
)
def get_project(project_id: uuid.UUID, session: Session = Depends(get_session)) -> Project:
    """Fetch a single project by id."""
    return _get_project_or_404(session, project_id)


@router.get(
    "/{project_id}/tasks",
    response_model=list[TaskOut],
    responses={
        "404": {"description": "Project not found"},
        "422": {"description": "Validation error"},
    },
)
def list_project_tasks(
    project_id: uuid.UUID, session: Session = Depends(get_session)
) -> list[Task]:
    """List all tasks of one project, ordered by creation time then id.

    Scoped strictly to ``project_id``: tasks of other projects are never
    returned from this endpoint.
    """
    _get_project_or_404(session, project_id)
    return list(
        session.scalars(
            select(Task).where(Task.project_id == project_id).order_by(Task.created_at, Task.id)
        )
    )


# ---------------------------------------------------------------------------
# Tasks router (Phase 3)
# ---------------------------------------------------------------------------

tasks_router = APIRouter(prefix="/tasks", tags=["tasks"])

_TASK_ERROR_RESPONSES = {
    404: {"description": "Task, parent task, or agent not found"},
    409: {
        "description": (
            "Invalid relationship or state conflict "
            "(cross-project parent, cycle, invalid/terminal transition, "
            "unusable agent, or concurrent state change)"
        )
    },
    422: {"description": "Validation error"},
}


def _task_service() -> TaskService:
    return TaskService()


def _execution_service() -> ExecutionService:
    return ExecutionService(workspaces=WorkspaceService(get_settings().workspaces_root))


def _task_http_error(exc: Exception) -> HTTPException:
    """Map Task engine errors to their HTTP status."""
    if isinstance(exc, ProjectNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if isinstance(exc, TaskNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if isinstance(exc, ParentTaskNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent task not found")
    if isinstance(exc, AgentNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if isinstance(exc, AgentUnavailableError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ExecutionConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ExecutionNotRunningError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (InvalidParentTaskError, TaskStateConflictError, TaskStateError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (ProviderNotConfiguredError, UnsupportedProviderError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, AgentCancellationError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, AgentTimeoutError):
        return HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc))
    if isinstance(exc, (AgentProviderError, AgentMalformedResponseError)):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    if isinstance(exc, WorkspaceError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@tasks_router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    responses=_TASK_ERROR_RESPONSES,
)
def create_task(
    payload: TaskCreate,
    session: Session = Depends(get_session),
    tasks: TaskService = Depends(_task_service),
) -> Task:
    """Create a task in state CREATED under an existing project.

    Only creation fields are accepted; status, timestamps, result and error
    are server-controlled.  ``agent_id`` is an optional reference to an
    existing usable agent; assigning never executes anything.
    """
    try:
        return tasks.create_task(
            session,
            project_id=payload.project_id,
            parent_task_id=payload.parent_task_id,
            agent_id=payload.agent_id,
            objective=payload.objective,
            instructions=payload.instructions,
            constraints=payload.constraints,
            success_criteria=payload.success_criteria,
        )
    except (TaskError, TaskStateError, AgentError) as exc:
        raise _task_http_error(exc) from exc


@tasks_router.get(
    "/{task_id}",
    response_model=TaskOut,
    responses={
        "404": _TASK_ERROR_RESPONSES[404],
        "422": _TASK_ERROR_RESPONSES[422],
    },
)
def get_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> Task:
    """Fetch a single task by id."""
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@tasks_router.post(
    "/{task_id}/cancel",
    response_model=TaskOut,
    responses=_TASK_ERROR_RESPONSES,
)
def cancel_task(
    task_id: uuid.UUID,
    session: Session = Depends(get_session),
    tasks: TaskService = Depends(_task_service),
) -> Task:
    """Cancel a task through the state machine.

    Cancellation is idempotent for already-cancelled tasks (200, state
    unchanged). COMPLETED and FAILED tasks are terminal: cancellation is
    rejected with 409 and never silently rewrites terminal state.

    **In-flight executions** (tasks in WAITING_FOR_AGENT with a stored
    execution reference) cannot be cancelled because the OpenHands Cloud API
    does not document a cancellation endpoint.  The task stays in its current
    state and the execution must finish or time out.
    """
    task = session.get(Task, task_id)
    if task is not None and task.status == WAITING_FOR_AGENT and task.execution_reference:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cancelling an in-flight execution is not supported by the "
            "openhands provider (Phase 5); the execution must finish or time out",
        )
    try:
        return tasks.cancel_task(session, task_id)
    except (TaskError, TaskStateError) as exc:
        raise _task_http_error(exc) from exc


# ---------------------------------------------------------------------------
# Execution endpoints (Phase 5)
# ---------------------------------------------------------------------------

_EXECUTION_ERROR_RESPONSES = {
    404: {"description": "Task or agent not found"},
    409: {
        "description": (
            "Execution not possible (no agent assigned, unusable agent, "
            "provider not configured, workspace not a git clone, "
            "duplicate/concurrent execution, or state conflict) or "
            "no in-flight execution to refresh"
        )
    },
    422: {"description": "Validation error"},
    502: {"description": "Provider error (OpenHands failure or malformed response)"},
    504: {"description": "Provider timeout"},
}


@tasks_router.post(
    "/{task_id}/execute",
    response_model=TaskExecuteOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_EXECUTION_ERROR_RESPONSES,
)
def execute_task(
    task_id: uuid.UUID,
    session: Session = Depends(get_session),
    executions: ExecutionService = Depends(_execution_service),
) -> TaskExecuteOut:
    """Start a real execution for a QUEUED task with an assigned agent.

    The task must be in state QUEUED and have an ``agent_id`` referencing an
    AVAILABLE openhands agent.  The workspace must be a git clone with an
    ``origin`` remote.

    Returns 202 Accepted with the task reaching WAITING_FOR_AGENT and an
    opaque execution reference.  Poll ``GET /tasks/{id}/execution`` or
    ``POST /tasks/{id}/execution/refresh`` to monitor the provider.
    """
    try:
        task = executions.execute_task(session, task_id)
        return TaskExecuteOut(
            task_id=task.id,
            status=task.status,
            execution_status=task.execution_status,
            reference=task.execution_reference,
        )
    except (TaskError, TaskStateError, AgentError, ExecutionError, WorkspaceError) as exc:
        raise _task_http_error(exc) from exc


@tasks_router.get(
    "/{task_id}/execution",
    response_model=TaskExecutionOut,
    responses=_EXECUTION_ERROR_RESPONSES,
)
def get_execution(
    task_id: uuid.UUID,
    session: Session = Depends(get_session),
    executions: ExecutionService = Depends(_execution_service),
) -> TaskExecutionOut:
    """Return the current execution state of a task (read-only, no provider poll).

    The ``execution_status`` and ``reference`` fields are the last-known
    values stored in the database.  Use ``POST /tasks/{id}/execution/refresh``
    to poll the provider and advance the task.
    """
    try:
        task, _ = executions.get_execution(session, task_id)
        return TaskExecutionOut(
            task_id=task.id,
            task_status=task.status,
            execution_status=task.execution_status,
            reference=task.execution_reference,
        )
    except (TaskError, AgentError, ExecutionError) as exc:
        raise _task_http_error(exc) from exc


@tasks_router.post(
    "/{task_id}/execution/refresh",
    response_model=TaskExecutionOut,
    responses=_EXECUTION_ERROR_RESPONSES,
)
def refresh_execution(
    task_id: uuid.UUID,
    session: Session = Depends(get_session),
    executions: ExecutionService = Depends(_execution_service),
) -> TaskExecutionOut:
    """Poll the provider and advance the task according to the result.

    When the provider reports a terminal state the task is transitioned:
    WAITING_FOR_AGENT -> WAITING_FOR_REVIEW (agent finished) or
    WAITING_FOR_AGENT -> FAILED (provider error / timeout).
    The agent is also released back to AVAILABLE.
    """
    try:
        task, status = executions.refresh_execution(session, task_id)
        return TaskExecutionOut(
            task_id=task.id,
            task_status=task.status,
            execution_status=task.execution_status,
            detail=status.detail if status is not None else "",
            reference=task.execution_reference,
        )
    except (TaskError, TaskStateError, AgentError, ExecutionError, WorkspaceError) as exc:
        raise _task_http_error(exc) from exc


# ---------------------------------------------------------------------------
# Agents router (Phase 4)
# ---------------------------------------------------------------------------

agents_router = APIRouter(prefix="/agents", tags=["agents"])

_AGENT_ERROR_RESPONSES = {
    404: {"description": "Agent not found"},
    409: {"description": "Agent name already exists"},
    422: {"description": "Validation error (e.g. unknown provider or secret key)"},
}


def _agent_manager() -> AgentManager:
    return AgentManager()


def _agent_http_error(exc: Exception) -> HTTPException:
    """Map Agent engine errors to their HTTP status."""
    if isinstance(exc, AgentNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if isinstance(exc, AgentNameConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (InvalidAgentConfigurationError, UnsupportedProviderError)):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@agents_router.post(
    "",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
    responses=_AGENT_ERROR_RESPONSES,
)
def register_agent(
    payload: AgentCreate,
    session: Session = Depends(get_session),
    agents: AgentManager = Depends(_agent_manager),
) -> Agent:
    """Register an agent definition.

    The agent is created in state UNAVAILABLE: no provider is connected in
    Phase 4, so claiming availability would be misleading.  No execution
    endpoint exists.
    """
    try:
        return agents.register_agent(
            session,
            name=payload.name,
            provider=payload.provider,
            capabilities=payload.capabilities,
            configuration=payload.configuration,
        )
    except AgentError as exc:
        raise _agent_http_error(exc) from exc


@agents_router.get("", response_model=list[AgentOut])
def list_agents(
    session: Session = Depends(get_session),
    agents: AgentManager = Depends(_agent_manager),
) -> list[Agent]:
    """List all registered agents, ordered deterministically."""
    return agents.list_agents(session)


@agents_router.get(
    "/{agent_id}",
    response_model=AgentOut,
    responses={
        "404": _AGENT_ERROR_RESPONSES[404],
        "422": {"description": "Validation error"},
    },
)
def get_agent(
    agent_id: uuid.UUID,
    session: Session = Depends(get_session),
    agents: AgentManager = Depends(_agent_manager),
) -> Agent:
    """Fetch a single agent definition by id."""
    try:
        return agents.get_agent(session, agent_id)
    except AgentError as exc:
        raise _agent_http_error(exc) from exc


@agents_router.get(
    "/{agent_id}/health",
    response_model=AgentHealthOut,
    responses={
        "404": _AGENT_ERROR_RESPONSES[404],
        "422": {"description": "Validation error"},
    },
)
def get_agent_health(
    agent_id: uuid.UUID,
    session: Session = Depends(get_session),
    agents: AgentManager = Depends(_agent_manager),
) -> AgentHealth:
    """Probe the agent's provider adapter.

    The probe runs and reports truthfully: Phase 4 adapters return
    ``not_configured``.  A 200 response means the probe itself succeeded, not
    that the provider is available.
    """
    try:
        return agents.check_health(session, agent_id)
    except AgentError as exc:
        raise _agent_http_error(exc) from exc
