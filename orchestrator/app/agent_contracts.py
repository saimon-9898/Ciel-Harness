"""Strict data contracts for agent operations (Phase 4).

These are the provider-independent request/response types used by the Agent
interface.  No provider-specific concept (OpenHands session id, Claude Code
process id, etc.) may appear here; adapters map between these contracts and
their own world in a later phase.

No execution happens in Phase 4: the execution contracts exist to define the
boundary that Phase 5 will implement.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent_providers import AgentProvider


class AgentTaskRequest(BaseModel):
    """Everything the Task Engine hands to a provider adapter to start work."""

    task_id: uuid.UUID
    project_id: uuid.UUID
    objective: str = Field(min_length=1, max_length=1000)
    instructions: str | None = Field(default=None, max_length=4000)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    success_criteria: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("objective", "instructions")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value


class AgentTaskHandle(BaseModel):
    """Opaque reference to a started task on a provider.

    ``reference`` is an opaque provider-side handle.  The Task Engine never
    inspects it.
    """

    model_config = ConfigDict(frozen=True)

    task_id: uuid.UUID
    provider: AgentProvider
    reference: str = Field(min_length=1, max_length=512)


class AgentExecutionState(enum.StrEnum):
    """State of a task as reported by a provider.

    This is the *agent-side* execution state, distinct from the Task state
    machine in ``task_states.py``.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class AgentStatusResult(BaseModel):
    """Provider-reported execution status for a task handle."""

    task_id: uuid.UUID
    state: AgentExecutionState
    detail: str = Field(default="", max_length=2000)


class AgentResult(BaseModel):
    """Provider-reported final result for a completed task."""

    task_id: uuid.UUID
    state: AgentExecutionState = Field(description="final state")
    output: str | None = Field(default=None, max_length=4000)
    error: str | None = Field(default=None, max_length=4000)


class AgentHealthState(enum.StrEnum):
    """Result of a health probe against a provider adapter.

    ``NOT_CONFIGURED`` means the adapter exists but the provider is not
    integrated yet (Phase 4).  ``UNSUPPORTED`` means the stored provider has
    no adapter at all.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"


class AgentHealth(BaseModel):
    """Provider health probe result."""

    agent_id: uuid.UUID
    provider: AgentProvider
    status: AgentHealthState
    detail: str = Field(default="", max_length=2000)
    checked_at: datetime
