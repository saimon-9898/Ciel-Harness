"""Pydantic schemas for API request and response bodies."""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent_contracts import AgentHealthState
from .agent_providers import (
    AGENT_STATUSES,
    AgentCapability,
    AgentProvider,
    redact_secrets,
    validate_agent_configuration,
)

# Project names become workspace directory names, so they must be safe as a
# single path component: start with a letter or digit, then letters, digits,
# '.', '_' or '-'. This rejects path separators, "..", and hidden names.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Task payload bounds. These cap every client-supplied field so no endpoint
# can be used to submit arbitrarily large payloads.
_TASK_OBJECTIVE_MAX = 1000
_TASK_INSTRUCTIONS_MAX = 4000
_TASK_LIST_MAX_ITEMS = 50
_TASK_LIST_ITEM_MAX = 500


class ProjectCreate(BaseModel):
    """Payload for creating a project."""

    name: str = Field(min_length=1, max_length=255)
    repository_url: str | None = Field(default=None, max_length=2048)
    repository_path: str | None = Field(default=None, max_length=1024)
    default_branch: str = Field(default="main", max_length=255)

    @field_validator("name")
    @classmethod
    def _name_must_be_safe(cls, value: str) -> str:
        if not _NAME_PATTERN.match(value):
            raise ValueError(
                "name must start with a letter or digit and contain only "
                "letters, digits, '.', '_' or '-'"
            )
        return value


class ProjectOut(BaseModel):
    """Project representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    repository_url: str | None
    repository_path: str | None
    default_branch: str
    status: str
    created_at: datetime
    updated_at: datetime


class TaskCreate(BaseModel):
    """Payload for creating a task.

    Only fields appropriate for task creation may be supplied. Status,
    timestamps, result, error and agent_id are server-controlled and are not
    accepted here (they are either absent from this schema or ignored, and the
    created row always gets the server's values).

    ``agent_id`` may reference an existing, usable agent.  Assigning an agent
    **never executes anything** in Phase 4; it only records the reference.
    """

    project_id: uuid.UUID
    parent_task_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    objective: str = Field(min_length=1, max_length=_TASK_OBJECTIVE_MAX)
    instructions: str | None = Field(default=None, max_length=_TASK_INSTRUCTIONS_MAX)
    constraints: list[str] | None = Field(default=None, max_length=_TASK_LIST_MAX_ITEMS)
    success_criteria: list[str] = Field(min_length=1, max_length=_TASK_LIST_MAX_ITEMS)

    @field_validator("objective", "instructions")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("constraints", "success_criteria")
    @classmethod
    def _list_items_bounded(cls, items: list[str] | None) -> list[str] | None:
        if items is None:
            return None
        for item in items:
            if not item.strip():
                raise ValueError("each item must be a non-blank string")
            if len(item) > _TASK_LIST_ITEM_MAX:
                raise ValueError(f"each item must be at most {_TASK_LIST_ITEM_MAX} characters")
        return items


class TaskOut(BaseModel):
    """Task representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    parent_task_id: uuid.UUID | None
    objective: str
    instructions: str | None
    constraints: list[str] | None
    success_criteria: list[str]
    status: str
    agent_id: uuid.UUID | None
    result: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class TaskExecuteOut(BaseModel):
    """Response of ``POST /tasks/{id}/execute`` (202 Accepted).

    ``status`` is the task state after the provider accepted the work
    (WAITING_FOR_AGENT).  ``execution_status`` is the last-known provider
    execution state and ``reference`` is the opaque execution handle -- an
    opaque string the adapter uses to track the provider conversation, not a
    provider-internal type.
    """

    task_id: uuid.UUID
    status: str
    execution_status: str | None = None
    reference: str | None = None


class TaskExecutionOut(BaseModel):
    """Provider-independent execution summary returned by the execution
    status/refresh endpoints.

    ``execution_status`` is the last-known provider execution state
    (queued/running/completed/failed/cancelled/unknown) and ``detail`` is the
    adapter's human-readable status text.  ``reference`` is the opaque
    execution handle.
    """

    task_id: uuid.UUID
    task_status: str
    execution_status: str | None = None
    detail: str = ""
    reference: str | None = None


# ---------------------------------------------------------------------------
# Agent schemas (Phase 4)
# ---------------------------------------------------------------------------

#: Maximum number of capability values an agent may advertise.
_AGENT_CAPABILITIES_MAX = 10


class AgentCreate(BaseModel):
    """Payload for registering an agent.

    Only operator-supplied fields are accepted: name, provider, capabilities,
    and configuration.  Status, timestamps, and any internal provider state
    are server-controlled and cannot be injected.
    """

    name: str = Field(min_length=1, max_length=100)
    provider: AgentProvider
    capabilities: list[AgentCapability] = Field(
        default_factory=list, max_length=_AGENT_CAPABILITIES_MAX
    )
    configuration: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("must not contain control characters")
        return value

    @field_validator("configuration")
    @classmethod
    def _configuration_validated(cls, value: dict[str, str]) -> dict[str, str]:
        try:
            return validate_agent_configuration(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class AgentOut(BaseModel):
    """Agent representation returned by the API.

    Secret-looking configuration keys are redacted defensively, even though
    the create schema already rejects them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    provider: str
    status: str
    capabilities: list[str]
    configuration: dict[str, str]
    created_at: datetime
    updated_at: datetime

    @field_validator("configuration", mode="before")
    @classmethod
    def _redact(cls, value: dict[str, str] | None) -> dict[str, str]:
        return redact_secrets(value)

    @field_validator("status")
    @classmethod
    def _status_validated(cls, value: str) -> str:
        if value not in AGENT_STATUSES:
            raise ValueError(f"unknown agent status {value!r}")
        return value


class AgentHealthOut(BaseModel):
    """Structured result of a health probe, as returned by the API.

    ``status`` is the probe result (available/unavailable/error/
    not_configured/unsupported), distinct from the agent's stored lifecycle
    status.
    """

    agent_id: uuid.UUID
    provider: AgentProvider
    status: AgentHealthState
    detail: str = Field(default="", max_length=2000)
    checked_at: datetime
