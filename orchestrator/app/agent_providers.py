"""Agent provider, capability, and status definitions (Phase 4).

The provider enum is the closed set of supported coding-agent providers.
Unknown provider strings are rejected at the API boundary. Each provider
has a corresponding adapter in ``adapters/`` that reports NOT_CONFIGURED
until integrated in a later phase.
"""

from __future__ import annotations

import enum
import re

# ---- Provider enum -----------------------------------------------------------

# Regex: any of these substrings in a key = secret-key reject.
_SECRET_KEY_BLOCKLIST_RE = re.compile(
    r"(?:^|[_-])("
    r"api[_-]?key"
    r"|token"
    r"|secret"
    r"|password"
    r"|passwd"
    r"|credential"
    r"|authorization"
    r"|auth"
    r"|private[_-]?key"
    r"|access[_-]?key"
    r"|client[_-]?secret"
    r")(?:[_-]|$)",
    re.IGNORECASE,
)


class AgentProvider(enum.StrEnum):
    """Supported coding-agent providers.

    Each value corresponds to a concrete adapter in ``adapters/``.
    Unknown provider strings are refused at the API boundary.
    """

    OPENHANDS = "openhands"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    GEMINI = "gemini"


# All valid provider values as a set for fast lookup.
VALID_PROVIDER_VALUES: frozenset[str] = frozenset(e.value for e in AgentProvider)


# ---- Capability enum ---------------------------------------------------------


class AgentCapability(enum.StrEnum):
    """Capabilities a provider may advertise.

    These are **advertised only** — no actual permissions are granted in
    Phase 4.  A future phase will enforce capability gating at execution
    time.
    """

    CODE = "code"
    TEST = "test"
    SHELL = "shell"
    GIT = "git"
    NETWORK = "network"


# ---- Agent status ------------------------------------------------------------

# Agent lifecycle status (stored in the DB).  These represent the operational
# state of the agent *record*, not the result of a health probe.
AVAILABLE = "AVAILABLE"
BUSY = "BUSY"
UNAVAILABLE = "UNAVAILABLE"
ERROR = "ERROR"
DISABLED = "DISABLED"

AGENT_STATUSES: frozenset[str] = frozenset({AVAILABLE, BUSY, UNAVAILABLE, ERROR, DISABLED})

# Statuses from which an agent may be assigned to a task.
USABLE_AGENT_STATUSES: frozenset[str] = frozenset({AVAILABLE})


def is_valid_agent_status(status: str) -> bool:
    """Return True when ``status`` is one of the known agent statuses."""
    return status in AGENT_STATUSES


def is_usable_agent_status(status: str) -> bool:
    """Return True when a task may be assigned to this agent."""
    return status in USABLE_AGENT_STATUSES


# ---- Configuration validation ------------------------------------------------


def _is_secret_key(key: str) -> bool:
    """Return True when ``key`` looks like a credential field."""
    return bool(_SECRET_KEY_BLOCKLIST_RE.search(key))


def validate_agent_configuration(
    config: dict[str, str] | None,
) -> dict[str, str]:
    """Validate agent configuration, rejecting secret keys and enforcing bounds.

    Returns the validated dict (or empty).  Raises ``ValueError`` on
    invalid input.
    """
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("configuration must be a dict of string to string")
    if len(config) > 20:
        raise ValueError("configuration must have at most 20 keys")
    for k, v in config.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError(f"invalid configuration key: {k!r}")
        if len(k) > 64:
            raise ValueError(f"configuration key too long: {k!r}")
        if not isinstance(v, str):
            raise ValueError(f"configuration value for {k!r} must be a string")
        if len(v) > 512:
            raise ValueError(f"configuration value for {k!r} too long (max 512)")
        if _is_secret_key(k):
            raise ValueError(
                f"configuration key {k!r} looks like a secret; "
                "secrets are not accepted in plaintext in Phase 4. "
                "Use a placeholder or wait for a secret-store integration."
            )
    return config


def redact_secrets(config: dict[str, str] | None) -> dict[str, str]:
    """Return a copy of ``config`` with known secret keys removed.

    Defense-in-depth: even if a secret key somehow made it into the DB,
    it will not be serialised in API responses.
    """
    if config is None:
        return {}
    return {k: v for k, v in config.items() if not _is_secret_key(k)}
