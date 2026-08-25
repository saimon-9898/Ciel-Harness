"""Workspace git-repository resolution tests (Phase 5).

The target repository for OpenHands execution must be derived server-side
from the project workspace's git ``origin`` remote, never from any
client-supplied value.  These tests build real (tiny) git repositories in a
temp dir and verify the resolution rules, including adversarial origin URLs.
"""

from __future__ import annotations

import subprocess

import pytest

from app.models import Project
from app.workspaces import WorkspaceError, WorkspaceService


def _git(workspace, *args):
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _make_git_workspace(root, name="proj", branch="main", remote_url=None):
    """Create a real git repo workspace with an optional origin remote."""
    workspace = root / name
    workspace.mkdir(parents=True, exist_ok=True)
    proc = _git(workspace, "init", "-b", branch)
    assert proc.returncode == 0, proc.stderr
    if remote_url is not None:
        proc = _git(workspace, "remote", "add", "origin", remote_url)
        assert proc.returncode == 0, proc.stderr
    return workspace


def _project(name="proj", default_branch="main") -> Project:
    return Project(name=name, default_branch=default_branch)


def test_resolve_repository_https_origin(tmp_path):
    _make_git_workspace(tmp_path, remote_url="https://github.com/acme/demo.git")
    service = WorkspaceService(tmp_path)
    repository, branch = service.resolve_repository(_project())
    assert repository == "acme/demo"
    assert branch == "main"


def test_resolve_repository_ssh_origin(tmp_path):
    _make_git_workspace(tmp_path, remote_url="git@github.com:acme/demo.git")
    service = WorkspaceService(tmp_path)
    repository, branch = service.resolve_repository(_project())
    assert repository == "acme/demo"
    assert branch == "main"


def test_resolve_repository_ssh_url_form(tmp_path):
    _make_git_workspace(tmp_path, remote_url="ssh://git@github.com/acme/demo.git")
    service = WorkspaceService(tmp_path)
    repository, _ = service.resolve_repository(_project())
    assert repository == "acme/demo"


def test_resolve_repository_bare_form(tmp_path):
    _make_git_workspace(tmp_path, remote_url="https://github.com/acme/demo")
    service = WorkspaceService(tmp_path)
    repository, _ = service.resolve_repository(_project())
    assert repository == "acme/demo"


def test_resolve_repository_plain_owner_repo(tmp_path):
    _make_git_workspace(tmp_path, remote_url="acme/demo")
    service = WorkspaceService(tmp_path)
    repository, _ = service.resolve_repository(_project())
    assert repository == "acme/demo"


def test_resolve_repository_uses_project_default_branch(tmp_path):
    _make_git_workspace(tmp_path, remote_url="https://github.com/acme/demo.git")
    service = WorkspaceService(tmp_path)
    _, branch = service.resolve_repository(_project(default_branch="release/2.x"))
    assert branch == "release/2.x"


def test_resolve_repository_rejects_unsafe_default_branch(tmp_path):
    _make_git_workspace(tmp_path, remote_url="https://github.com/acme/demo.git")
    service = WorkspaceService(tmp_path)
    with pytest.raises(WorkspaceError):
        service.resolve_repository(_project(default_branch="../evil"))


def test_resolve_repository_no_origin_remote(tmp_path):
    _make_git_workspace(tmp_path, remote_url=None)
    service = WorkspaceService(tmp_path)
    with pytest.raises(WorkspaceError, match="origin"):
        service.resolve_repository(_project())


def test_resolve_repository_not_a_git_repo(tmp_path):
    (tmp_path / "proj").mkdir(parents=True)
    service = WorkspaceService(tmp_path)
    with pytest.raises(WorkspaceError):
        service.resolve_repository(_project())


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://github.com/../../evil.git",
        "https://github.com/acme/..%2f..%2fevil.git",
        "https://github.com/acme/demo.git /bin/sh",
        "https://github.com/acme/demo.git;rm -rf /",
        "https://github.com/..",
        "https://github.com/acme/",
        "https://github.com/acme/demo.git/extra",
    ],
)
def test_resolve_repository_rejects_adversarial_origins(tmp_path, bad_url):
    _make_git_workspace(tmp_path, remote_url=bad_url)
    service = WorkspaceService(tmp_path)
    with pytest.raises(WorkspaceError):
        service.resolve_repository(_project())


def test_resolve_repository_rejects_option_form_url_via_config(tmp_path):
    # `git remote add` would reject an option-shaped URL (-oProxyCommand=...),
    # so it is planted directly in the config to exercise our parser.
    workspace = _make_git_workspace(tmp_path)
    proc = _git(workspace, "config", "remote.origin.url", "-oProxyCommand=evil")
    assert proc.returncode == 0, proc.stderr
    service = WorkspaceService(tmp_path)
    with pytest.raises(WorkspaceError):
        service.resolve_repository(_project())


def test_resolve_repository_never_invokes_remote_url_as_command(tmp_path):
    # A malicious remote URL containing shell metacharacters must be parsed,
    # never executed.  Resolution must fail safely (no origin parse) without
    # running the embedded command.
    workspace = _make_git_workspace(
        tmp_path,
        remote_url="https://github.com/acme/demo.git$(touch marker_pwned)",
    )
    service = WorkspaceService(tmp_path)
    with pytest.raises(WorkspaceError):
        service.resolve_repository(_project())
    # The marker file must NOT exist.
    assert not (workspace / "marker_pwned").exists()
