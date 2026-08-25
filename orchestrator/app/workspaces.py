"""Workspace service: safe per-project filesystem isolation.

Every project owns a dedicated directory under the configured projects root
(e.g. ``projects/project-a/``). All paths are resolved and verified to remain
inside the owning project's workspace, which prevents path traversal,
absolute-path injection, symlink escapes, and cross-project access.

The projects root is server configuration (``WORKSPACES_ROOT``), never an API
parameter. Project names are validated at creation time and re-validated here
(defense in depth) because they become directory names.
"""

import logging
import re
from pathlib import Path

from .models import Project

logger = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class WorkspaceError(Exception):
    """Raised when a path cannot be safely resolved inside a workspace."""


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


class WorkspaceService:
    """Resolves and validates project workspace paths."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def ensure_root(self) -> Path:
        """Create the projects root directory if it does not exist."""
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root.resolve()

    def get_workspace(self, project: Project) -> Path:
        """Return the resolved, container-verified workspace directory."""
        if not _NAME_PATTERN.match(project.name):
            raise WorkspaceError(f"project name {project.name!r} is not a safe directory name")
        base = self.root.resolve()
        workspace = (base / project.name).resolve()
        if not _is_relative_to(workspace, base):
            raise WorkspaceError(f"workspace for {project.name!r} escapes the projects root")
        return workspace

    def create_workspace(self, project: Project) -> Path:
        """Create the project's workspace directory (idempotent)."""
        workspace = self.get_workspace(project)
        workspace.mkdir(parents=True, exist_ok=True)
        resolved = workspace.resolve()
        if not _is_relative_to(resolved, self.root.resolve()):
            raise WorkspaceError(f"workspace for {project.name!r} escapes the projects root")
        return resolved

    def remove_workspace(self, project: Project) -> None:
        """Best-effort removal of a project's (empty) workspace directory.

        Used to clean up a workspace when project creation fails after the
        directory was created. Only empty directories are removed; non-empty
        directories are left untouched and logged.
        """
        workspace = self.get_workspace(project)
        try:
            workspace.rmdir()
        except OSError:
            logger.warning("could not remove workspace %s", workspace, exc_info=True)

    def validate_workspace(self, project: Project, path: str | Path) -> Path:
        """Resolve a user-supplied path inside a project's workspace.

        ``path`` may be relative (resolved against the workspace) or absolute,
        but the final resolved path must stay inside the workspace.
        Traversal (``..``), symlink escapes, and other projects' workspaces
        raise :class:`WorkspaceError`.
        """
        workspace = self.get_workspace(project)
        user_path = Path(path)
        candidate = user_path if user_path.is_absolute() else workspace / user_path
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError) as exc:
            # e.g. embedded null bytes, unreachable symlink targets, or
            # runaway symlink depth. Never leak a raw filesystem error.
            raise WorkspaceError(f"could not resolve path {str(path)!r}") from exc
        if not _is_relative_to(resolved, workspace):
            raise WorkspaceError("path escapes the project workspace")
        return resolved
