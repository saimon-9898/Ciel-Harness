"""Workspace service: safe per-project filesystem isolation.

Every project owns a dedicated directory under the configured projects root
(e.g. ``projects/project-a/``). All paths are resolved and verified to remain
inside the owning project's workspace, which prevents path traversal,
absolute-path injection, symlink escapes, and cross-project access.

The projects root is server configuration (``WORKSPACES_ROOT``), never an API
parameter. Project names are validated at creation time and re-validated here
(defense in depth) because they become directory names.

Phase 5 adds git-repository resolution: the owner/repo string required by the
OpenHands Cloud API is derived from the workspace's own git ``origin`` remote
URL (server-side, never from an HTTP parameter), which enforces workspace
security.
"""

import logging
import re
import subprocess
from pathlib import Path

from .models import Project

logger = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Pattern for a safe "owner/repo" string (GitHub-style).
_REPO_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
# Pattern for a safe git branch name (rejects ".." and leading "-").
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


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

    def resolve_repository(self, project: Project) -> tuple[str, str]:
        """Return ``(owner/repo, branch)`` for OpenHands execution.

        The repository is derived **from the workspace's git ``origin`` remote
        URL**, never from an HTTP parameter: the execution layer passes the
        validated workspace (or its project) and this method returns the
        provider-facing repository string. A workspace that is not a git clone
        with an ``origin`` remote is rejected with :class:`WorkspaceError` --
        execution must never be given an arbitrary path or URL supplied by a
        client.

        ``branch`` is the project's ``default_branch`` (server field),
        validated to be a safe git branch name.
        """
        workspace = self.get_workspace(project)
        origin_url = _git_remote_origin(workspace)
        if origin_url is None:
            raise WorkspaceError(
                "project workspace is not a git clone with an 'origin' remote; "
                "OpenHands Cloud execution requires the workspace to be a git "
                "repository whose 'origin' remote names the target repository"
            )
        repository = _parse_repository(origin_url)
        if repository is None:
            raise WorkspaceError(
                "could not parse a safe owner/repo from the workspace "
                f"'origin' remote {origin_url!r}"
            )
        branch = project.default_branch or "main"
        if not _is_safe_branch(branch):
            raise WorkspaceError(f"project default_branch {branch!r} is not a safe git branch name")
        return repository, branch


def _git_remote_origin(workspace: Path) -> str | None:
    """Return the workspace git ``origin`` remote URL, or None when absent.

    Uses ``git remote get-url origin`` with an argument list (no shell), so a
    malicious remote URL can never be interpreted as a command.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # git missing, workspace unreadable, or git hung: treat as no remote.
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def _parse_repository(url: str) -> str | None:
    """Extract a safe ``owner/repo`` from a git remote URL.

    Supports https, ssh (scp-like and ssh://), and plain ``owner/repo`` forms.
    The result must match the safe owner/repo pattern; anything else (extra
    path segments, traversal, non-GitHub hosts with odd paths) is rejected.
    """
    candidate: str | None = None
    if url.startswith("git@"):
        # scp-like: git@host:owner/repo[.git]
        after_host = url.split(":", 1)[1] if ":" in url else url
        candidate = after_host
    elif url.startswith(("https://", "http://", "ssh://", "git://")):
        path = url.split("://", 1)[1]
        # strip host (first '/')
        if "/" in path:
            path = path.split("/", 1)[1]
        candidate = path
    elif "/" in url and ":" not in url:
        # bare owner/repo
        candidate = url
    if candidate is None:
        return None
    candidate = candidate.strip()
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    candidate = candidate.rstrip("/")
    if candidate.count("/") != 1:
        return None
    if not _REPO_PATTERN.match(candidate):
        return None
    owner, repo = candidate.split("/", 1)
    if owner in ("..", ".") or repo in ("..", "."):
        return None
    if ".." in owner or ".." in repo:
        return None
    return candidate


def _is_safe_branch(branch: str) -> bool:
    """Return True when ``branch`` is a safe git branch name."""
    if not _BRANCH_PATTERN.match(branch):
        return False
    if ".." in branch or branch.startswith("-"):
        return False
    return True
