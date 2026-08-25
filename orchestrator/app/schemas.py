"""Pydantic schemas for API request and response bodies."""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    """

    project_id: uuid.UUID
    parent_task_id: uuid.UUID | None = None
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
