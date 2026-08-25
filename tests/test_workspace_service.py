"""Direct unit tests for WorkspaceService, bypassing the HTTP layer.

These exercise the service contract itself: root creation, workspace
creation/removal semantics, path containment, and symlink handling.
"""

import logging
from pathlib import Path

import pytest

from app.models import Project
from app.workspaces import WorkspaceError, WorkspaceService


def _project(name: str = "svc") -> Project:
    return Project(name=name)


def _service(root: Path) -> WorkspaceService:
    return WorkspaceService(root)


# ---------- ensure_root ----------


def test_ensure_root_creates_nested_directory(tmp_path):
    root = tmp_path / "deep" / "nested" / "projects"
    service = _service(root)
    resolved = service.ensure_root()
    assert resolved == root.resolve()
    assert resolved.is_dir()
    assert resolved.is_absolute()


def test_ensure_root_is_idempotent(tmp_path):
    root = tmp_path / "projects"
    service = _service(root)
    first = service.ensure_root()
    second = service.ensure_root()  # must not raise when it already exists
    assert first == second
    assert root.is_dir()


# ---------- get_workspace ----------


def test_get_workspace_returns_resolved_absolute_path(tmp_path):
    root = tmp_path / "projects"
    _service(root).ensure_root()
    workspace = _service(root).get_workspace(_project("my-proj"))
    assert workspace == (root / "my-proj").resolve()
    assert workspace.is_absolute()
    assert not workspace.exists()  # get_workspace must not create anything


def test_get_workspace_rejects_name_with_separator(tmp_path):
    service = _service(tmp_path / "projects")
    with pytest.raises(WorkspaceError):
        service.get_workspace(_project("a/b"))
    with pytest.raises(WorkspaceError):
        service.get_workspace(_project(".."))


def test_get_workspace_rejects_mutated_name(tmp_path):
    """Defense in depth: even a name mutated after creation must fail."""
    service = _service(tmp_path / "projects")
    project = _project("fine")
    project.name = ".hidden"  # safe at creation time, unsafe as directory name
    with pytest.raises(WorkspaceError):
        service.get_workspace(project)


# ---------- create_workspace ----------


def test_create_workspace_makes_directory(tmp_path):
    root = tmp_path / "projects"
    service = _service(root)
    created = service.create_workspace(_project("created"))
    assert created == (root / "created").resolve()
    assert created.is_dir()


def test_create_workspace_is_idempotent(tmp_path):
    root = tmp_path / "projects"
    service = _service(root)
    first = service.create_workspace(_project("idem"))
    second = service.create_workspace(_project("idem"))
    assert first == second
    assert (root / "idem").is_dir()


# ---------- remove_workspace ----------


def test_remove_workspace_removes_empty_directory(tmp_path):
    root = tmp_path / "projects"
    service = _service(root)
    project = _project("gone")
    service.create_workspace(project)
    assert (root / "gone").is_dir()
    service.remove_workspace(project)
    assert not (root / "gone").exists()


def test_remove_workspace_missing_directory_does_not_raise(tmp_path):
    root = tmp_path / "projects"
    _service(root).ensure_root()
    _service(root).remove_workspace(_project("never-existed"))


def test_remove_workspace_non_empty_directory_logs_and_keeps(tmp_path, caplog):
    root = tmp_path / "projects"
    service = _service(root)
    project = _project("busy")
    service.create_workspace(project)
    (root / "busy" / "file.txt").write_text("data")
    with caplog.at_level(logging.WARNING, logger="app.workspaces"):
        service.remove_workspace(project)
    assert (root / "busy").is_dir()  # non-empty dir must survive
    assert (root / "busy" / "file.txt").read_text() == "data"
    assert any("could not remove workspace" in r.message for r in caplog.records)


# ---------- validate_workspace ----------


def test_validate_workspace_accepts_pathlib_and_str(tmp_path):
    root = tmp_path / "projects"
    service = _service(root)
    project = _project("both")
    service.create_workspace(project)
    (root / "both" / "f.txt").write_text("x")
    as_str = service.validate_workspace(project, "f.txt")
    as_path = service.validate_workspace(project, Path("f.txt"))
    assert as_str == as_path == (root / "both" / "f.txt").resolve()


def test_validate_workspace_symlink_inside_workspace_is_allowed(tmp_path):
    root = tmp_path / "projects"
    service = _service(root)
    project = _project("inside")
    service.create_workspace(project)
    (root / "inside" / "real.txt").write_text("data")
    (root / "inside" / "alias").symlink_to(root / "inside" / "real.txt")
    resolved = service.validate_workspace(project, "alias")
    assert resolved == (root / "inside" / "real.txt").resolve()


def test_validate_workspace_rejects_symlink_chain_escaping(tmp_path):
    root = tmp_path / "projects"
    service = _service(root)
    project = _project("chain")
    service.create_workspace(project)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "chain" / "hop1").symlink_to(root / "chain" / "hop2")
    (root / "chain" / "hop2").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        service.validate_workspace(project, "hop1")


def test_validate_workspace_unicode_separator_is_literal(tmp_path):
    """A unicode lookalike separator (U+2215) is NOT a path separator on POSIX.

    pathlib treats it as an ordinary character, so the path stays inside the
    workspace as a literal component; it must not resolve anywhere else.
    """
    root = tmp_path / "projects"
    service = _service(root)
    project = _project("uni")
    service.create_workspace(project)
    resolved = service.validate_workspace(project, "..\u2215etc")
    assert resolved == (root / "uni" / "..\u2215etc").resolve()


def test_validate_workspace_percent_encoded_separator_is_literal(tmp_path):
    """URL-encoded separators are never decoded by pathlib; no escape occurs."""
    root = tmp_path / "projects"
    service = _service(root)
    project = _project("enc")
    service.create_workspace(project)
    resolved = service.validate_workspace(project, "..%2fetc")
    assert resolved == (root / "enc" / "..%2fetc").resolve()


def test_validate_workspace_null_byte_raises_workspace_error(tmp_path):
    """An embedded null byte must surface as WorkspaceError, never a raw error."""
    root = tmp_path / "projects"
    service = _service(root)
    project = _project("nul")
    service.create_workspace(project)
    with pytest.raises(WorkspaceError):
        service.validate_workspace(project, "../etc\x00file")


def test_validate_workspace_rejects_absolute_path_into_other_project(tmp_path):
    root = tmp_path / "projects"
    service = _service(root)
    a = _project("proj-a")
    b = _project("proj-b")
    service.create_workspace(a)
    service.create_workspace(b)
    (root / "proj-b" / "secret.txt").write_text("secret")
    with pytest.raises(WorkspaceError):
        service.validate_workspace(a, str(root / "proj-b" / "secret.txt"))


def test_workspace_root_inside_symlink_still_contained(tmp_path):
    """A symlinked projects root must not break containment guarantees."""
    real_root = tmp_path / "real-projects"
    real_root.mkdir()
    link_root = tmp_path / "link-projects"
    link_root.symlink_to(real_root, target_is_directory=True)
    service = _service(link_root)
    project = _project("symlinked")
    created = service.create_workspace(project)
    assert created == (real_root / "symlinked").resolve()
    assert created.is_dir()
    # a path inside the resolved workspace is accepted
    (real_root / "symlinked" / "file.txt").write_text("x")
    resolved = service.validate_workspace(project, "file.txt")
    assert resolved == (real_root / "symlinked" / "file.txt").resolve()


def test_workspace_service_does_not_require_db():
    """WorkspaceService is pure filesystem logic and needs no database."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        service = WorkspaceService(Path(tmp) / "projects")
        created = service.create_workspace(_project("no-db"))
        assert created.is_dir()
        assert created == (Path(tmp) / "projects" / "no-db").resolve()


def test_get_workspace_root_does_not_exist_yet_is_resolved(tmp_path):
    """The workspace path resolves even before the root exists."""
    root = tmp_path / "not-yet"
    project = _project("x")
    workspace = _service(root).get_workspace(project)
    assert workspace == (root / "x").resolve()
