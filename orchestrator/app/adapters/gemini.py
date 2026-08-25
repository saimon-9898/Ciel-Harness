"""Gemini adapter (Phase 4).

Boundary only: Gemini is **not connected** in Phase 4.  Health reports
``not_configured``; execution methods raise
:class:`ProviderNotConfiguredError`.
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


class GeminiAdapter(AgentAdapter):
    """Adapter for the ``gemini`` provider (boundary only in Phase 4)."""

    provider = AgentProvider.GEMINI

    def check_health(self, agent_id: uuid.UUID) -> AgentHealth:
        return AgentHealth(
            agent_id=agent_id,
            provider=self.provider,
            status=AgentHealthState.NOT_CONFIGURED,
            detail=(
                "gemini provider is not configured: Gemini integration lands in a later phase."
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
