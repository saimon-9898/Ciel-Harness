"""Database models.

Phase 2 adds the Project model. Future phases will add agents, tasks,
sessions, and events. Importing this module from ``db.init_db()`` registers
all models on ``Base.metadata`` so their tables are created automatically.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

__all__ = ["Base", "Project", "Task"]


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


class Task(Base):
    """A coding task belonging to a project (Phase 3).

    Tasks are the unit of work in the orchestration engine. They are created,
    transitioned through a deterministic state machine, queried, and cancelled.
    No agent execution or automation is performed in Phase 3.

    ``agent_id`` is a future reference for the agent that will execute this
    task. It is stored but never used in Phase 3 — no Agent system exists yet.
    """

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    objective: Mapped[str] = mapped_column(String(1000), nullable=False)
    instructions: Mapped[str | None] = mapped_column(String(4000))
    constraints: Mapped[list[str] | None] = mapped_column(JSON)
    success_criteria: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="CREATED", server_default="CREATED"
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    result: Mapped[str | None] = mapped_column(String(4000))
    error: Mapped[str | None] = mapped_column(String(4000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
