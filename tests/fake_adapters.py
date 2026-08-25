"""In-memory fake provider adapters (tests only, never production).

``FakeInMemoryAdapter`` implements the ``AgentAdapter`` contract with a
scripted, synchronous execution model. It is injected into the
:class:`ExecutionService` during orchestration tests so the entire execution
lifecycle (execute → refresh → complete) can be exercised without a real
provider.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.adapters.base import AgentAdapter
from app.agent_contracts import (
    AgentExecutionState,
    AgentHealth,
    AgentHealthState,
    AgentResult,
    AgentStatusResult,
    AgentTaskHandle,
    AgentTaskRequest,
)
from app.agent_errors import AgentCancellationError
from app.agent_providers import AgentProvider


class FakeInMemoryAdapter(AgentAdapter):
    """Scripted in-memory provider (tests only, never production).

    ``start_statuses`` controls the sequence of execution states returned by
    repeated ``get_status()`` calls.  Default: ``["running", "completed"]``
    (first call reports running, next reports completed).
    """

    provider = AgentProvider.OPENHANDS

    def __init__(
        self,
        *,
        start_statuses: list[str] | None = None,
        start_error: Exception | None = None,
        status_error: Exception | None = None,
        result_error: Exception | None = None,
        cancel_raises: bool = True,
        repository: str | None = None,
        branch: str | None = None,
    ) -> None:
        self._statuses = list(start_statuses or ["running", "completed"])
        self._status_index = 0
        self.start_error = start_error
        self.status_error = status_error
        self.result_error = result_error
        self.cancel_raises = cancel_raises
        self.repository = repository
        self.branch = branch

        #: Every ``start_task`` call is recorded here.
        self.started: list[tuple[AgentTaskRequest, AgentTaskHandle]] = []
        #: Every ``cancel_task`` call is recorded here.
        self.cancelled: list[AgentTaskHandle] = []
        #: Every ``get_status`` call is recorded here.
        self.status_calls: list[AgentTaskHandle] = []

    def check_health(self, agent_id: uuid.UUID) -> AgentHealth:
        return AgentHealth(
            agent_id=agent_id,
            provider=self.provider,
            status=AgentHealthState.AVAILABLE,
            detail="fake provider ok",
            checked_at=datetime.now(UTC),
        )

    def start_task(self, request: AgentTaskRequest) -> AgentTaskHandle:
        if self.start_error is not None:
            raise self.start_error
        handle = AgentTaskHandle(
            task_id=request.task_id,
            provider=self.provider,
            reference=f"fake-{request.task_id}",
        )
        self.started.append((request, handle))
        return handle

    def get_status(self, handle: AgentTaskHandle) -> AgentStatusResult:
        self.status_calls.append(handle)
        if self.status_error is not None:
            raise self.status_error
        idx = min(self._status_index, len(self._statuses) - 1)
        state = AgentExecutionState(self._statuses[idx])
        self._status_index += 1
        return AgentStatusResult(
            task_id=handle.task_id,
            state=state,
            detail=f"fake {state.value}",
        )

    def get_result(self, handle: AgentTaskHandle) -> AgentResult:
        if self.result_error is not None:
            raise self.result_error
        status = self.get_status(handle)
        return AgentResult(
            task_id=handle.task_id,
            state=status.state,
            output=status.detail if status.state is AgentExecutionState.COMPLETED else None,
            error=status.detail if status.state is AgentExecutionState.FAILED else None,
        )

    def cancel_task(self, handle: AgentTaskHandle) -> None:
        self.cancelled.append(handle)
        if self.cancel_raises:
            raise AgentCancellationError("fake provider does not support cancellation")
