"""Pydantic schemas for API request and response bodies."""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Project names become workspace directory names, so they must be safe as a
# single path component: start with a letter or digit, then letters, digits,
# '.', '_' or '-'. This rejects path separators, "..", and hidden names.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
