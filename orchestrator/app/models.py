"""Database models.

Phase 2 adds the Project model. Future phases will add agents, tasks,
sessions, and events. Importing this module from ``db.init_db()`` registers
all models on ``Base.metadata`` so their tables are created automatically.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

__all__ = ["Base", "Project"]


class Project(Base):
    """A tracked coding project with its own isolated workspace."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    repository_url: Mapped[str | None] = mapped_column(String(2048))
    repository_path: Mapped[str | None] = mapped_column(String(1024))
    default_branch: Mapped[str] = mapped_column(String(255), default="main", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="created", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
