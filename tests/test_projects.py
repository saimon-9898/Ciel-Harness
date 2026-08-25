"""Tests for project management and workspace isolation."""

import uuid
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import get_session_factory
from app.models import Project
from app.workspaces import WorkspaceError, WorkspaceService


def _get_project(project_id: uuid.UUID) -> Project:
    with get_session_factory()() as session:
        return session.get(Project, project_id)


def _workspace_service() -> WorkspaceService:
    return WorkspaceService(get_settings().workspaces_root)


def _create(client, name: str = "project-a", **overrides) -> dict:
    payload = {"name": name, **overrides}
    r = client.post("/projects", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- project creation ----------


def test_create_project(client):
    c, _, projects_root = client
    body = _create(
        c,
        name="project-a",
        repository_url="https://github.com/acme/project-a",
        default_branch="main",
    )
    assert body["name"] == "project-a"
    assert body["repository_url"] == "https://github.com/acme/project-a"
    assert body["default_branch"] == "main"
    assert body["status"] == "created"
    uuid.UUID(body["id"])  # must be a valid UUID
    assert body["created_at"] is not None
    assert body["updated_at"] is not None
    # workspace directory is created eagerly
    assert (projects_root / "project-a").is_dir()


def test_create_project_defaults(client):
    c, _, _ = client
    body = _create(c, name="defaults")
    assert body["default_branch"] == "main"
    assert body["status"] == "created"
    assert body["repository_url"] is None
    assert body["repository_path"] is None


def test_create_project_duplicate_name_rejected(client):
    c, _, _ = client
    _create(c, name="dup")
    r = c.post("/projects", json={"name": "dup"})
    assert r.status_code == 409


@pytest.mark.parametrize(
    "bad_name",
    ["../escape", "a/b", "a\\b", "/abs", "..", ".hidden", "a b", ""],
)
def test_create_project_rejects_unsafe_names(client, bad_name):
    c, _, _ = client
    r = c.post("/projects", json={"name": bad_name})
    assert r.status_code == 422


def test_create_project_name_boundaries(client):
    c, _, projects_root = client
    for name in ("a", "my.project_v2", "MyProject", "2nd-try", "a" * 255):
        body = _create(c, name=name)
        assert body["name"] == name
        assert (projects_root / name).is_dir()
    # one character over the limit is rejected
    r = c.post("/projects", json={"name": "a" * 256})
    assert r.status_code == 422


def test_create_project_ignores_unknown_fields(client):
    c, _, _ = client
    body = _create(c, name="clean-fields", extra_stuff="ignored", nope=123)
    assert body["name"] == "clean-fields"


# ---------- project retrieval ----------


def test_get_project(client):
    c, _, _ = client
    created = _create(c, name="retrieve-me")
    r = c.get(f"/projects/{created['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "retrieve-me"


def test_get_project_unknown_id_returns_404(client):
    c, _, _ = client
    r = c.get(f"/projects/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json() == {"detail": "Project not found"}


def test_get_project_invalid_id_returns_422(client):
    c, _, _ = client
    r = c.get("/projects/not-a-uuid")
    assert r.status_code == 422


# ---------- project listing ----------


def test_list_projects_empty(client):
    c, _, _ = client
    assert c.get("/projects").json() == []


def test_list_projects(client):
    c, _, _ = client
    _create(c, name="first")
    _create(c, name="second")
    names = [p["name"] for p in c.get("/projects").json()]
    assert names == ["first", "second"]


# ---------- workspace resolution ----------


def test_workspace_resolution(client):
    c, _, projects_root = client
    body = _create(c, name="ws-a")
    project = _get_project(uuid.UUID(body["id"]))
    service = _workspace_service()
    ws = service.get_workspace(project)
    assert ws == (projects_root / "ws-a").resolve()
    assert ws.is_relative_to(projects_root.resolve())


def test_get_workspace_rejects_unsafe_name(client):
    c, _, _ = client
    project = _get_project(uuid.UUID(_create(c, name="ok")["id"]))
    project.name = "../evil"  # defense-in-depth: name mutated after creation
    with pytest.raises(WorkspaceError):
        _workspace_service().get_workspace(project)


def test_workspace_empty_path_resolves_to_workspace(client):
    c, _, projects_root = client
    project = _get_project(uuid.UUID(_create(c, name="empty")["id"]))
    resolved = _workspace_service().validate_workspace(project, "")
    assert resolved == (projects_root / "empty").resolve()


def test_workspace_dot_segments_staying_inside_are_allowed(client):
    c, _, projects_root = client
    project = _get_project(uuid.UUID(_create(c, name="dots")["id"]))
    (projects_root / "dots" / "sub").mkdir()
    resolved = _workspace_service().validate_workspace(project, "sub/./../sub")
    assert resolved == (projects_root / "dots" / "sub").resolve()


def test_remove_workspace_is_idempotent(client):
    c, _, projects_root = client
    body = _create(c, name="cleanup")
    project = _get_project(uuid.UUID(body["id"]))
    service = _workspace_service()
    service.remove_workspace(project)
    assert not (projects_root / "cleanup").exists()
    service.remove_workspace(project)  # second call must not raise


# ---------- path traversal and absolute-path rejection ----------


@pytest.mark.parametrize(
    "bad_path",
    [
        "..",
        "../outside",
        "../../etc/passwd",
        "a/../../outside",
        "/etc/passwd",
        str(Path("/tmp")),
    ],
)
def test_validate_workspace_rejects_escape(client, bad_path):
    c, _, _ = client
    project = _get_project(uuid.UUID(_create(c, name="target")["id"]))
    with pytest.raises(WorkspaceError):
        _workspace_service().validate_workspace(project, bad_path)


def test_validate_workspace_accepts_internal_path(client):
    c, _, projects_root = client
    project = _get_project(uuid.UUID(_create(c, name="safe")["id"]))
    (projects_root / "safe" / "sub").mkdir()
    (projects_root / "safe" / "sub" / "file.txt").write_text("x")
    resolved = _workspace_service().validate_workspace(project, "sub/file.txt")
    assert resolved == (projects_root / "safe" / "sub" / "file.txt").resolve()


def test_validate_workspace_accepts_absolute_internal_path(client):
    c, _, projects_root = client
    project = _get_project(uuid.UUID(_create(c, name="abs")["id"]))
    target = projects_root / "abs" / "file.txt"
    target.write_text("x")
    resolved = _workspace_service().validate_workspace(project, str(target))
    assert resolved == target.resolve()


# ---------- symlink escape ----------


def test_validate_workspace_rejects_symlink_escape(client):
    c, _, projects_root = client
    project = _get_project(uuid.UUID(_create(c, name="symlink")["id"]))
    outside = projects_root.parent / "outside.txt"
    outside.write_text("secret")
    (projects_root / "symlink" / "link").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        _workspace_service().validate_workspace(project, "link")


# ---------- cross-project isolation ----------


def test_cross_project_workspace_isolation(client):
    c, _, projects_root = client
    a = _get_project(uuid.UUID(_create(c, name="project-a")["id"]))
    b = _get_project(uuid.UUID(_create(c, name="project-b")["id"]))
    service = _workspace_service()

    ws_a = service.get_workspace(a)
    ws_b = service.get_workspace(b)
    assert ws_a != ws_b
    assert ws_a == (projects_root / "project-a").resolve()
    assert ws_b == (projects_root / "project-b").resolve()

    # A file inside project-b's workspace must not be reachable from a.
    (projects_root / "project-b" / "secret.txt").write_text("secret")
    with pytest.raises(WorkspaceError):
        service.validate_workspace(a, "../project-b/secret.txt")
    # An absolute path into another project's workspace is also rejected.
    with pytest.raises(WorkspaceError):
        service.validate_workspace(a, str(ws_b / "secret.txt"))


def test_create_project_workspaces_are_distinct(client):
    c, _, projects_root = client
    _create(c, name="project-a")
    _create(c, name="project-b")
    ws_a = projects_root / "project-a"
    ws_b = projects_root / "project-b"
    assert ws_a.is_dir()
    assert ws_b.is_dir()
    assert ws_a != ws_b


# ---------- creation failure handling ----------


def test_create_project_commit_race_returns_409_and_cleans_workspace(client, monkeypatch):
    """A unique-name race at commit time must not 500 or orphan a workspace."""
    c, _, projects_root = client
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    def _boom(*args, **kwargs):
        raise IntegrityError("INSERT INTO projects ...", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(Session, "commit", _boom)
    r = c.post("/projects", json={"name": "race"})
    assert r.status_code == 409
    assert r.json() == {"detail": "Project name already exists"}
    assert not (projects_root / "race").exists()


# ---------- OpenAPI contract ----------


def test_openapi_schema_exposes_all_routes(client):
    c, _, _ = client
    schema = c.get("/openapi.json").json()
    assert set(schema["paths"]) == {"/health", "/projects", "/projects/{project_id}"}
    assert "post" in schema["paths"]["/projects"]
    assert "get" in schema["paths"]["/projects"]
    assert "get" in schema["paths"]["/projects/{project_id}"]
    assert "get" in schema["paths"]["/health"]
