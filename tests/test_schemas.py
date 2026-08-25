"""Direct unit tests for the Pydantic request/response schemas.

These validate field types, length constraints, name-pattern enforcement, and
ORM serialization without going through the HTTP layer.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models import Project
from app.schemas import ProjectCreate, ProjectOut

# ---------- ProjectCreate field types and defaults ----------


def test_project_create_defaults():
    payload = ProjectCreate(name="defaults")
    assert payload.name == "defaults"
    assert payload.repository_url is None
    assert payload.repository_path is None
    assert payload.default_branch == "main"


def test_project_create_full_payload():
    payload = ProjectCreate(
        name="full",
        repository_url="https://github.com/acme/full",
        repository_path="services/core",
        default_branch="dev",
    )
    assert payload.repository_url == "https://github.com/acme/full"
    assert payload.repository_path == "services/core"
    assert payload.default_branch == "dev"


# ---------- ProjectCreate name validation ----------


@pytest.mark.parametrize(
    "good",
    [
        "a",
        "A",
        "0",
        "project-a",
        "project_a",
        "project.a",
        "myProject2",
        "a" * 255,
    ],
)
def test_project_create_accepts_safe_names(good):
    ProjectCreate(name=good)


@pytest.mark.parametrize(
    "bad",
    [
        " leading",
        "trailing ",
        "-leading-dash",
        ".leading-dot",
        "a/b",
        "a\\b",
        "a b",
        "a\tb",
        "a\nb",
        "../up",
        "..",
        ".",
        "a\x00b",
        "😀",
        "café",
        "a" * 256,
        "",
        "a:b",
        "a*b",
        "a?b",
        'a"b',
        "a'b",
        "a<b",
        "a>b",
        "a|b",
    ],
)
def test_project_create_rejects_unsafe_names(bad):
    with pytest.raises(ValidationError):
        ProjectCreate(name=bad)


def test_project_create_rejects_unicode_control_characters():
    for bad in ("a\u0000", "a\u2028", "a\u2029"):
        with pytest.raises(ValidationError):
            ProjectCreate(name=bad)


# ---------- ProjectCreate field length limits ----------


def test_project_create_rejects_overlong_repository_url():
    with pytest.raises(ValidationError):
        ProjectCreate(name="ok", repository_url="x" * 2049)


def test_project_create_accepts_2048_char_repository_url():
    ProjectCreate(name="ok", repository_url="x" * 2048)


def test_project_create_rejects_overlong_repository_path():
    with pytest.raises(ValidationError):
        ProjectCreate(name="ok", repository_path="x" * 1025)


def test_project_create_accepts_1024_char_repository_path():
    ProjectCreate(name="ok", repository_path="x" * 1024)


def test_project_create_rejects_overlong_default_branch():
    with pytest.raises(ValidationError):
        ProjectCreate(name="ok", default_branch="x" * 256)


def test_project_create_rejects_non_string_name():
    with pytest.raises(ValidationError):
        ProjectCreate(name=123)


# ---------- ProjectOut serialization ----------


def test_project_out_serializes_orm_model():
    now = datetime.now(UTC)
    project = Project(
        id=uuid.uuid4(),
        name="serialize-me",
        repository_url="https://github.com/acme/serialize-me",
        repository_path=None,
        default_branch="main",
        status="created",
        created_at=now,
        updated_at=now,
    )
    out = ProjectOut.model_validate(project)
    assert out.id == project.id
    assert out.name == "serialize-me"
    assert out.repository_url == "https://github.com/acme/serialize-me"
    assert out.repository_path is None
    assert out.default_branch == "main"
    assert out.status == "created"
    assert out.created_at == now
    assert out.updated_at == now


def test_project_out_model_dump_keys():
    """The JSON-serialized project must contain exactly the documented fields."""
    now = datetime.now(UTC)
    project = Project(
        id=uuid.uuid4(),
        name="dump",
        repository_url=None,
        repository_path=None,
        default_branch="main",
        status="created",
        created_at=now,
        updated_at=now,
    )
    dumped = ProjectOut.model_validate(project).model_dump()
    assert set(dumped) == {
        "id",
        "name",
        "repository_url",
        "repository_path",
        "default_branch",
        "status",
        "created_at",
        "updated_at",
    }
    assert isinstance(dumped["id"], uuid.UUID)
    assert isinstance(dumped["created_at"], datetime)


def test_project_out_dump_json_uses_isoformat():
    now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
    project = Project(
        id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        name="json",
        repository_url=None,
        repository_path=None,
        default_branch="main",
        status="created",
        created_at=now,
        updated_at=now,
    )
    import json

    raw = ProjectOut.model_validate(project).model_dump_json()
    parsed = json.loads(raw)
    assert parsed["id"] == "12345678-1234-5678-1234-567812345678"
    assert parsed["created_at"] == "2026-08-25T12:00:00Z"
