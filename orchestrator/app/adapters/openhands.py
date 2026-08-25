"""OpenHands adapter (Phase 4).

The abstraction boundary for the future OpenHands integration exists, but
OpenHands is **not connected** in Phase 4: health reports ``not_configured``
and every execution method raises :class:`ProviderNotConfiguredError`.

No OpenHands endpoints or SDK assumptions are made here.  Phase 5 will
implement the real integration behind this boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ..agent_contracts import (
    AgentHealth,
    AgentHealthState,
    AgentResult,
    AgentStatusResult,
    AgentTaskHandle,
    AgentTaskRequest,
)
from ..agent_providers import AgentProvider
from .base import AgentAdapter


class OpenHandsAdapter(AgentAdapter):
    """Adapter for the ``openhands`` provider (boundary only in Phase 4)."""

    provider = AgentProvider.OPENHANDS

    def check_health(self, agent_id: uuid.UUID) -> AgentHealth:
        return AgentHealth(
            agent_id=agent_id,
            provider=self.provider,
            status=AgentHealthState.NOT_CONFIGURED,
            detail=(
                "openhands provider is not configured: OpenHands integration lands in Phase 5."
            ),
            checked_at=datetime.now(UTC),
        )

    def start_task(self, request: AgentTaskRequest) -> AgentTaskHandle:
        raise self._not_configured("start_task")

    def get_status(self, handle: AgentTaskHandle) -> AgentStatusResult:
        raise self._not_configured("get_status")

    def get_result(self, handle: AgentTaskHandle) -> AgentResult:
        raise self._not_configured("get_result")

    def cancel_task(self, handle: AgentTaskHandle) -> None:
        raise self._not_configured("cancel_task")
