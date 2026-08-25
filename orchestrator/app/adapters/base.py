"""Agent adapter interface (Phase 4).

``AgentAdapter`` is the provider-independent contract that the Task Engine
uses.  A concrete adapter wraps one provider (OpenHands, Claude Code, Codex,
Gemini).  Providers must not leak through this interface.

Phase 4 adapters are honest about being unconfigured: health probes report
``not_configured`` and every execution method raises
:class:`ProviderNotConfiguredError`.  No adapter ever fabricates a success.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from ..agent_contracts import (
    AgentHealth,
    AgentResult,
    AgentStatusResult,
    AgentTaskHandle,
    AgentTaskRequest,
)
from ..agent_errors import ProviderNotConfiguredError
from ..agent_providers import AgentProvider


class AgentAdapter(ABC):
    """Provider-independent interface to a coding-agent provider."""

    #: The provider this adapter talks to.  Subclasses set this.
    provider: AgentProvider

    def _not_configured(self, operation: str) -> ProviderNotConfiguredError:
        """Structured 'not implemented yet' error for this provider."""
        return ProviderNotConfiguredError(
            f"{self.provider.value!r} provider is not configured: "
            f"{operation} is unavailable in Phase 4 (integration pending)."
        )

    @abstractmethod
    def check_health(self, agent_id: uuid.UUID) -> AgentHealth:
        """Probe provider connectivity for ``agent_id``.

        Returns a structured :class:`AgentHealth`.  Phase 4 adapters return
        ``NOT_CONFIGURED`` — they never claim to be available.
        """

    @abstractmethod
    def start_task(self, request: AgentTaskRequest) -> AgentTaskHandle:
        """Ask the provider to start work on a task.

        Returns an opaque handle.  Phase 4 raises
        :class:`ProviderNotConfiguredError` instead of pretending to start.
        """

    @abstractmethod
    def get_status(self, handle: AgentTaskHandle) -> AgentStatusResult:
        """Return the provider-reported execution status of a task."""

    @abstractmethod
    def get_result(self, handle: AgentTaskHandle) -> AgentResult:
        """Return the provider-reported final result of a task."""

    @abstractmethod
    def cancel_task(self, handle: AgentTaskHandle) -> None:
        """Ask the provider to cancel a task.

        Raises :class:`AgentCancellationError` when the provider cannot
        cancel.  Phase 4 raises :class:`ProviderNotConfiguredError`.
        """
