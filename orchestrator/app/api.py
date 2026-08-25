"""Projects API router (Phase 2).

Exposes project management endpoints. Workspaces are created eagerly on
project creation; the WorkspaceService guarantees filesystem isolation.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_session
from .models import Project
from .schemas import ProjectCreate, ProjectOut
from .workspaces import WorkspaceError, WorkspaceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


def _workspace_service() -> WorkspaceService:
    return WorkspaceService(get_settings().workspaces_root)


def _get_project_or_404(session: Session, project_id: uuid.UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    session: Session = Depends(get_session),
    workspaces: WorkspaceService = Depends(_workspace_service),
) -> Project:
    """Create a project and its isolated workspace directory."""
    existing = session.scalar(select(Project).where(Project.name == payload.name))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project name already exists",
        )

    project = Project(
        name=payload.name,
        repository_url=payload.repository_url,
        repository_path=payload.repository_path,
        default_branch=payload.default_branch,
    )
    try:
        workspaces.create_workspace(project)
        session.add(project)
        session.commit()
    except WorkspaceError as exc:
        logger.error("failed to create workspace for project %r: %s", payload.name, exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except IntegrityError as exc:
        # Race: another request inserted the same unique name between the
        # pre-check and this commit. Roll back and report a clean conflict.
        session.rollback()
        workspaces.remove_workspace(project)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project name already exists",
        ) from exc
    except Exception:
        session.rollback()
        workspaces.remove_workspace(project)
        raise
    session.refresh(project)
    logger.info(
        "project created",
        extra={"project_id": str(project.id), "project_name": project.name},
    )
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(session: Session = Depends(get_session)) -> list[Project]:
    """List all projects, ordered by creation time then name."""
    return list(session.scalars(select(Project).order_by(Project.created_at, Project.name)))


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: uuid.UUID, session: Session = Depends(get_session)) -> Project:
    """Fetch a single project by id."""
    return _get_project_or_404(session, project_id)
