"""Agent registry and manager (Phase 4).

``AgentManager`` owns the agent records and the provider adapter resolution.
The Task Engine and the API interact with agents through this module:

- ``get_agent`` / ``get_agent_with_adapter`` return the abstraction (an
  :class:`AgentAdapter`), never a provider-specific implementation.
- ``register_agent`` validates the provider and the configuration before
  persisting anything.

Phase 4 performs **no execution**.  Adapters are resolved and health-checked;
``start_task`` is never invoked by the engine.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .adapters import (
    AgentAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    GeminiAdapter,
    OpenHandsAdapter,
)
from .agent_contracts import AgentHealth
from .agent_errors import (
    AgentNameConflictError,
    AgentNotFoundError,
    AgentUnavailableError,
    InvalidAgentConfigurationError,
    UnsupportedProviderError,
)
from .agent_providers import (
    AVAILABLE,
    BUSY,
    ERROR,
    UNAVAILABLE,
    AgentCapability,
    AgentProvider,
    validate_agent_configuration,
)
from .models import Agent

logger = logging.getLogger(__name__)

#: Provider -> adapter class.  This is the single place adapters are wired.
ADAPTER_REGISTRY: dict[AgentProvider, type[AgentAdapter]] = {
    AgentProvider.OPENHANDS: OpenHandsAdapter,
    AgentProvider.CLAUDE_CODE: ClaudeCodeAdapter,
    AgentProvider.CODEX: CodexAdapter,
    AgentProvider.GEMINI: GeminiAdapter,
}


def _now_utc() -> datetime:
    return datetime.now(UTC)


def resolve_adapter(provider: AgentProvider | str) -> AgentAdapter:
    """Return an adapter instance for ``provider``.

    Raises :class:`UnsupportedProviderError` when the provider has no
    adapter (unknown or unregistered provider).
    """
    try:
        provider_enum = provider if isinstance(provider, AgentProvider) else AgentProvider(provider)
    except ValueError as exc:
        raise UnsupportedProviderError(f"unsupported agent provider: {provider!r}") from exc
    adapter_cls = ADAPTER_REGISTRY.get(provider_enum)
    if adapter_cls is None:
        raise UnsupportedProviderError(f"unsupported agent provider: {provider.value!r}")
    return adapter_cls()


class AgentManager:
    """Registry for agent records and adapter resolution."""

    def register_agent(
        self,
        session: Session,
        *,
        name: str,
        provider: AgentProvider,
        capabilities: list[AgentCapability] | None = None,
        configuration: dict[str, str] | None = None,
    ) -> Agent:
        """Register a new agent record.

        - provider must be a supported enum value (unknown -> 422 at the API
          boundary, ValueError here)
        - configuration must not contain secret keys (rejected with
          :class:`InvalidAgentConfigurationError`)
        - the agent starts in state UNAVAILABLE: no provider is connected in
          Phase 4, so claiming availability would be fake.
        """
        try:
            validated_config = validate_agent_configuration(configuration)
        except ValueError as exc:
            raise InvalidAgentConfigurationError(str(exc)) from exc

        existing = session.scalar(select(Agent).where(Agent.name == name))
        if existing is not None:
            raise AgentNameConflictError(f"agent name already exists: {name!r}")

        agent = Agent(
            id=uuid.uuid4(),
            name=name,
            provider=provider.value,
            status=UNAVAILABLE,
            capabilities=[c.value for c in (capabilities or [])],
            configuration=validated_config,
        )
        session.add(agent)
        session.commit()
        session.refresh(agent)
        logger.info(
            "agent registered",
            extra={
                "agent_id": str(agent.id),
                "agent_name": agent.name,
                "provider": agent.provider,
                "status": agent.status,
            },
        )
        return agent

    def get_agent(self, session: Session, agent_id: uuid.UUID) -> Agent:
        """Return an agent record or raise :class:`AgentNotFoundError`."""
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise AgentNotFoundError(f"agent {agent_id} does not exist")
        return agent

    def list_agents(self, session: Session) -> list[Agent]:
        """Return all agent records, ordered deterministically by name."""
        stmt = select(Agent).order_by(Agent.name, Agent.id)
        return list(session.scalars(stmt))

    def get_agent_with_adapter(
        self, session: Session, agent_id: uuid.UUID
    ) -> tuple[Agent, AgentAdapter]:
        """Return ``(agent, adapter)`` for a stored agent.

        Raises :class:`AgentNotFoundError` when the record is missing and
        :class:`UnsupportedProviderError` when the stored provider has no
        adapter.  The caller receives the abstraction, never a
        provider-specific implementation.
        """
        agent = self.get_agent(session, agent_id)
        return agent, resolve_adapter(agent.provider)

    def check_health(self, session: Session, agent_id: uuid.UUID) -> AgentHealth:
        """Run the adapter's health probe for an agent record."""
        agent, adapter = self.get_agent_with_adapter(session, agent_id)
        return adapter.check_health(agent.id)

    # ---- execution lifecycle (Phase 5) ---------------------------------------

    def claim_agent(self, session: Session, agent_id: uuid.UUID) -> Agent:
        """Claim an AVAILABLE agent for execution with a DB-safe CAS.

        Only AVAILABLE agents may be claimed. The update is conditional
        (``status = AVAILABLE``), so two concurrent claims cannot both win:
        the loser observes rowcount 0 and receives
        :class:`AgentUnavailableError`. The caller must commit.
        """
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise AgentNotFoundError(f"agent {agent_id} does not exist")
        if agent.status == BUSY:
            raise AgentUnavailableError(
                f"agent {agent_id} is busy (another execution is in flight)"
            )
        if agent.status != AVAILABLE:
            raise AgentUnavailableError(f"agent {agent_id} is not usable (status {agent.status!r})")
        stmt = (
            update(Agent)
            .where(Agent.id == agent_id, Agent.status == AVAILABLE)
            .values(status=BUSY, updated_at=_now_utc())
        )
        rowcount = session.execute(stmt).rowcount
        if rowcount != 1:
            raise AgentUnavailableError(
                f"agent {agent_id} is not usable (status changed concurrently)"
            )
        session.refresh(agent)
        logger.info(
            "agent claimed for execution",
            extra={"agent_id": str(agent_id), "status": BUSY},
        )
        return agent

    def release_agent(
        self, session: Session, agent_id: uuid.UUID, *, mark_error: bool = False
    ) -> Agent:
        """Release a BUSY agent back to AVAILABLE (or ERROR).

        The update is conditional (``status = BUSY``), so releasing an agent
        that is no longer BUSY is a safe no-op and can never free another
        execution's claim. The caller must commit.
        """
        target = ERROR if mark_error else AVAILABLE
        stmt = (
            update(Agent)
            .where(Agent.id == agent_id, Agent.status == BUSY)
            .values(status=target, updated_at=_now_utc())
        )
        rowcount = session.execute(stmt).rowcount
        agent = session.get(Agent, agent_id)
        if agent is not None:
            session.refresh(agent)
        if rowcount == 1:
            logger.info(
                "agent released after execution",
                extra={"agent_id": str(agent_id), "status": target},
            )
        return agent
