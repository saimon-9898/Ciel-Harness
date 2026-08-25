"""Execution service (Phase 5): run tasks on a real provider adapter.

The execution layer is the only place that starts, polls, and finishes
provider work. It composes the Phase 3 Task state machine (never bypassing
``TaskService``), the Phase 4 Agent registry, and the Phase 2
``WorkspaceService``:

- The target repository is derived **server-side** from the task's project
  workspace (git ``origin`` remote) and is never taken from an HTTP parameter.
- The task transition ``QUEUED -> RUNNING`` is the compare-and-swap lock that
  prevents two requests from executing the same task twice.
- The agent claim ``AVAILABLE -> BUSY`` (also CAS) prevents the same agent
  from running two executions at once.
- After the provider accepts the task, the task moves to WAITING_FOR_AGENT and
  stays there until ``refresh_execution`` observes a terminal provider state.

Lifecycle (documented):

    QUEUED -> RUNNING -> WAITING_FOR_AGENT -> WAITING_FOR_REVIEW   (agent finished)
    QUEUED -> RUNNING -> FAILED                                    (start or provider failure)
    WAITING_FOR_AGENT -> FAILED                                    (provider error / timeout)

Agent lifecycle: AVAILABLE -> BUSY when execution starts; BUSY -> AVAILABLE
when the execution reaches a terminal provider state (or the orchestrator
stops tracking it on timeout/provider error).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .adapters import AgentAdapter
from .agent_contracts import (
    AgentExecutionState,
    AgentStatusResult,
    AgentTaskHandle,
    AgentTaskRequest,
)
from .agent_errors import (
    AgentError,
    AgentNotFoundError,
    AgentUnavailableError,
    ProviderNotConfiguredError,
    UnsupportedProviderError,
)
from .agent_manager import AgentManager
from .agent_providers import AgentProvider, is_usable_agent_status
from .config import get_settings
from .models import Agent, Project, Task
from .task_service import (
    ProjectNotFoundError,
    TaskError,
    TaskNotFoundError,
    TaskService,
)
from .task_states import WAITING_FOR_AGENT, TaskStateError
from .workspaces import WorkspaceError, WorkspaceService

logger = logging.getLogger(__name__)

#: Adapter factory: (agent, repository, branch) -> AgentAdapter.
AdapterFactory = Callable[[Agent, str | None, str | None], AgentAdapter]


class ExecutionError(Exception):
    """Base class for execution layer errors."""


class ExecutionConflictError(ExecutionError):
    """The task cannot be executed (state, agent, or configuration conflict)."""


class ExecutionNotRunningError(ExecutionError):
    """The task has no in-flight execution to refresh."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def default_adapter_factory(
    agent: Agent, repository: str | None, branch: str | None
) -> AgentAdapter:
    """Build the adapter for ``agent`` (default wiring).

    Only the OpenHands provider has a real adapter in Phase 5.  The
    workspace-derived repository/branch context is passed only to the
    OpenHands adapter; every other provider is truthfully unconfigured.
    """
    try:
        provider = AgentProvider(agent.provider)
    except ValueError as exc:
        raise UnsupportedProviderError(f"unsupported agent provider: {agent.provider!r}") from exc
    if provider is AgentProvider.OPENHANDS:
        from .adapters.openhands import OpenHandsAdapter

        return OpenHandsAdapter(repository=repository, branch=branch)
    raise ProviderNotConfiguredError(
        f"{agent.provider!r} provider is not configured: "
        "only the openhands provider supports real execution in Phase 5"
    )


class ExecutionService:
    """Orchestrates real task execution on provider adapters."""

    def __init__(
        self,
        *,
        task_service: TaskService | None = None,
        agent_manager: AgentManager | None = None,
        workspaces: WorkspaceService | None = None,
        adapter_factory: AdapterFactory | None = None,
        max_execution_seconds: float | None = None,
    ) -> None:
        self.task_service = task_service or TaskService()
        self.agent_manager = agent_manager or AgentManager()
        self.workspaces = workspaces or WorkspaceService(get_settings().workspaces_root)
        self.adapter_factory = adapter_factory or default_adapter_factory
        self.max_execution_seconds = (
            max_execution_seconds
            if max_execution_seconds is not None
            else get_settings().openhands_max_execution_seconds
        )

    # ---- execution -----------------------------------------------------------

    def execute_task(self, session: Session, task_id: uuid.UUID) -> Task:
        """Start a real execution for a QUEUED task.

        Returns the task in WAITING_FOR_AGENT with ``execution_reference``
        set. Raises :class:`ExecutionConflictError` (409), the Agent/Task
        error model, or :class:`WorkspaceError` on pre-flight failures.
        """
        task = session.get(Task, task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} does not exist")
        if task.agent_id is None:
            raise ExecutionConflictError(
                "task has no agent assigned; assign an available agent before executing"
            )
        agent = session.get(Agent, task.agent_id)
        if agent is None:
            raise AgentNotFoundError(f"agent {task.agent_id} does not exist")
        if not is_usable_agent_status(agent.status):
            raise AgentUnavailableError(f"agent {agent.id} is not usable (status {agent.status!r})")
        project = session.get(Project, task.project_id)
        if project is None:
            raise ProjectNotFoundError(f"project {task.project_id} does not exist")

        # Workspace binding: repository is derived from the project's own
        # workspace, never from any client-supplied string.
        repository: str | None = None
        branch: str | None = None
        try:
            repository, branch = self.workspaces.resolve_repository(project)
        except WorkspaceError as exc:
            raise ExecutionConflictError(str(exc)) from exc
        adapter = self.adapter_factory(agent, repository, branch)

        # 1) Claim the task execution (CAS): only one request can move a task
        #    out of QUEUED, which prevents double execution of the same task.
        try:
            self.task_service.start_task(session, task_id)
        except TaskStateError as exc:
            raise ExecutionConflictError(str(exc)) from exc

        # 2) Claim the agent (CAS): only one execution may use the agent.
        try:
            self.agent_manager.claim_agent(session, agent.id)
        except AgentUnavailableError as exc:
            self._fail(session, task_id, error=f"agent became unavailable before execution: {exc}")
            raise ExecutionConflictError(str(exc)) from exc

        # 3) Start the real provider execution.
        request = AgentTaskRequest(
            task_id=task.id,
            project_id=task.project_id,
            objective=task.objective,
            instructions=task.instructions,
            constraints=task.constraints or [],
            success_criteria=task.success_criteria or [],
        )
        try:
            handle = adapter.start_task(request)
        except AgentError as exc:
            self._fail(
                session,
                task_id,
                error=f"provider start failed: {_safe_exc(exc)}",
                release=True,
            )
            raise

        # 4) Persist the handle and move to the asynchronous wait state.
        task.execution_reference = handle.reference
        task.execution_status = AgentExecutionState.RUNNING.value
        try:
            self.task_service.wait_for_agent(session, task_id)
        except (TaskStateError, TaskError) as exc:
            # e.g. a concurrent cancel moved the task out of RUNNING while the
            # provider call was in flight. Release the agent and report the
            # conflict truthfully; the provider conversation is left running
            # and can be found via the stored reference if it was persisted.
            self.agent_manager.release_agent(session, agent.id)
            session.commit()
            raise ExecutionConflictError(str(exc)) from exc
        logger.info(
            "execution started",
            extra={"task_id": str(task_id), "agent_id": str(agent.id), "repository": repository},
        )
        return task

    def get_execution(self, session: Session, task_id: uuid.UUID) -> tuple[Task, None]:
        """Return the stored execution state of a task without polling.

        Read-only: the provider is not contacted and no transition happens.
        """
        task = session.get(Task, task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} does not exist")
        return task, None

    def refresh_execution(
        self, session: Session, task_id: uuid.UUID
    ) -> tuple[Task, AgentStatusResult]:
        """Poll the provider and advance the task according to the result.

        Only tasks in WAITING_FOR_AGENT with a stored execution reference are
        refreshed. Terminal provider states advance the task (and release the
        agent); running/unknown states leave the task waiting. Raises
        :class:`ExecutionNotRunningError` when there is nothing to refresh.
        """
        task = session.get(Task, task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} does not exist")
        if task.status != WAITING_FOR_AGENT or not task.execution_reference:
            raise ExecutionNotRunningError(
                f"task {task_id} has no in-flight execution (status {task.status!r})"
            )
        if task.agent_id is None:
            raise ExecutionConflictError(
                f"task {task_id} lost its agent reference; execution cannot be tracked"
            )
        agent = session.get(Agent, task.agent_id)
        if agent is None:
            raise AgentNotFoundError(f"agent {task.agent_id} does not exist")

        adapter = self.adapter_factory(agent, None, None)
        handle = AgentTaskHandle(
            task_id=task.id,
            provider=AgentProvider(agent.provider),
            reference=task.execution_reference,
        )
        try:
            status = adapter.get_status(handle)
        except AgentError as exc:
            self._fail(
                session,
                task_id,
                error=f"provider status check failed: {_safe_exc(exc)}",
                release=True,
            )
            raise

        # Orchestrator-side patience boundary: if the provider keeps running
        # past the configured maximum, stop tracking it truthfully.
        if status.state in (AgentExecutionState.RUNNING, AgentExecutionState.UNKNOWN):
            if self._execution_timed_out(task):
                self._fail(
                    session,
                    task_id,
                    error=(
                        f"execution timed out at the orchestrator boundary after "
                        f"{self.max_execution_seconds:g}s; the provider conversation "
                        "may still be running in the OpenHands UI"
                    ),
                    release=True,
                )
                task = session.get(Task, task_id)
                status = AgentStatusResult(
                    task_id=task_id,
                    state=AgentExecutionState.FAILED,
                    detail="orchestrator execution timeout",
                )
                return task, status
            task.execution_status = status.state.value
            session.commit()
            return task, status

        if status.state is AgentExecutionState.COMPLETED:
            task.execution_status = AgentExecutionState.COMPLETED.value
            task.result = f"Agent finished. {status.detail}".strip()[:4000]
            self.task_service.submit_for_review(session, task_id)
            self.agent_manager.release_agent(session, agent.id)
            session.commit()
            logger.info(
                "execution completed",
                extra={"task_id": str(task_id), "agent_id": str(agent.id)},
            )
            return task, status

        if status.state is AgentExecutionState.FAILED:
            self._fail(
                session,
                task_id,
                error=status.detail or "provider reported failure",
                release=True,
            )
            return session.get(Task, task_id), status

        # CANCELLED (unreachable in Phase 5) or UNKNOWN without timeout.
        task.execution_status = status.state.value
        session.commit()
        return task, status

    def execution_out(self, task: Task, status: AgentStatusResult | None = None) -> dict:
        """Build the provider-independent execution summary for API responses."""
        return {
            "task_id": task.id,
            "task_status": task.status,
            "execution_status": task.execution_status,
            "reference": task.execution_reference,
            "detail": status.detail if status is not None else "",
        }

    # ---- internals -----------------------------------------------------------

    def _execution_timed_out(self, task: Task) -> bool:
        if task.started_at is None:
            return False
        started_at = task.started_at
        # SQLite does not persist timezone info: values read back from the DB
        # are naive. Treat them as UTC before comparing.
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        elapsed = (_now_utc() - started_at).total_seconds()
        return elapsed > self.max_execution_seconds

    def _fail(
        self, session: Session, task_id: uuid.UUID, *, error: str, release: bool = False
    ) -> None:
        """Move the task to FAILED through the state machine and optionally
        release the agent claim.

        ``release=False`` is used when we never claimed the agent (a failed
        claim must not free someone else's claim).
        """
        try:
            self.task_service.fail_task(session, task_id, error=error[:4000])
        except (TaskError, TaskStateError):
            # A concurrent transition (e.g. cancel) already moved the task.
            session.rollback()
        if release:
            if task_agent_id := session.get(Task, task_id).agent_id:
                try:
                    self.agent_manager.release_agent(session, task_agent_id)
                    session.commit()
                except AgentError:
                    session.rollback()


def _safe_exc(exc: Exception) -> str:
    """Return a bounded, secret-free error description."""
    text = str(exc) or type(exc).__name__
    return text[:1000]
