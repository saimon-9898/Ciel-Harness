"""Database models.

Phase 2 adds the Project model. Phase 3 adds the Task model. Phase 4 adds the
Agent model and the task->agent reference. Importing this module from
``db.init_db()`` registers all models on ``Base.metadata`` so their tables are
created automatically.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

__all__ = ["Agent", "Base", "Project", "Task"]


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
    No agent execution or automation is performed — agents are only referenced.

    ``agent_id`` records the agent that will execute this task. Phase 4
    validates that the referenced agent exists and is usable, but assignment
    never executes anything.
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
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result: Mapped[str | None] = mapped_column(String(4000))
    error: Mapped[str | None] = mapped_column(String(4000))
    # Opaque provider handle for the in-flight execution (Phase 5).  This is
    # the ``AgentTaskHandle.reference`` -- a provider-independent opaque
    # string that the adapter uses to poll status.  ``None`` when no
    # execution has started.
    execution_reference: Mapped[str | None] = mapped_column(String(512))
    # Last-known provider execution state (``AgentExecutionState`` value).
    # ``None`` means no execution has ever been started on this task.
    execution_status: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Agent(Base):
    """A registered coding-agent provider instance (Phase 4).

    Agents are **global** infrastructure resources: providers (an OpenHands
    instance, a Claude Code install, ...) serve the whole orchestrator and are
    shared across projects.  No project scope exists on the agent itself;
    Phase 5 execution must verify workspace isolation per task assignment.

    ``configuration`` stores **non-secret** settings only.  Secret-looking
    keys are rejected at the API boundary; no plaintext secrets are stored.
    """

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="UNAVAILABLE", server_default="UNAVAILABLE"
    )
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    configuration: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
