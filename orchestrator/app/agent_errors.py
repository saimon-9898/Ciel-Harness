"""Structured exceptions for the Agent abstraction (Phase 4).

The failure model is explicit so callers (and the future supervisor) can
distinguish: missing agent, unsupported provider, unconfigured provider,
unavailable agent, timeouts, provider errors, malformed responses, and
cancellation failures.  No failure is ever converted into a success.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base class for all Agent engine errors."""


class AgentNotFoundError(AgentError):
    """The referenced agent record does not exist."""


class AgentNameConflictError(AgentError):
    """An agent with the same name already exists."""


class UnsupportedProviderError(AgentError):
    """The stored provider has no adapter and cannot be used."""


class InvalidAgentConfigurationError(AgentError):
    """Agent configuration failed validation (e.g. a secret key)."""


class AgentUnavailableError(AgentError):
    """The agent exists but cannot be used (unavailable/busy/error/disabled)."""


class ProviderNotConfiguredError(AgentError):
    """The provider adapter exists but is not integrated yet.

    Phase 4 adapters raise this instead of pretending to start work.
    """


class AgentTimeoutError(AgentError):
    """A provider call exceeded its time budget."""


class AgentProviderError(AgentError):
    """The provider reported an error."""


class AgentMalformedResponseError(AgentError):
    """The provider returned an unparseable or invalid response."""


class AgentCancellationError(AgentError):
    """Cancelling a task on the provider failed."""
