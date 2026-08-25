"""Tests for application startup, /health, database init, and configuration loading."""

from sqlalchemy import text

from app.config import get_settings
from app.db import check_database
from app.main import app


def _clear_caches():
    get_settings.cache_clear()


# ---------- health ----------


def test_health_returns_ok(client):
    c, _, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------- startup ----------


def test_startup_creates_database_file(client):
    """The lifespan initializer should create the SQLite database file."""
    _, db_path, _ = client
    assert db_path.exists(), "Database file was not created during startup"


def test_startup_makes_database_queryable(client):
    _, _, _ = client
    assert check_database() is True


def test_startup_creates_workspace_root(client):
    """The lifespan initializer should create the projects root directory."""
    _, _, projects_root = client
    assert projects_root.is_dir(), "Workspace root was not created during startup"


# ---------- database ----------


def test_database_initialization_is_idempotent(client):
    from app.db import init_db

    _, _, _ = client
    init_db()  # second call (first happens in lifespan)
    assert check_database() is True


# ---------- configuration ----------


def test_config_loads_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'env.db'}")
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    _clear_caches()
    try:
        s = get_settings()
        assert s.database_url == f"sqlite:///{tmp_path / 'env.db'}"
        assert s.workspaces_root == str(tmp_path / "ws")
        assert s.environment == "production"
        assert s.log_level == "WARNING"
    finally:
        _clear_caches()


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKSPACES_ROOT", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    _clear_caches()
    try:
        s = get_settings()
        assert s.app_name == "AI CTO Hub Orchestrator"
        assert s.app_version == "0.1.0"
        assert s.environment == "development"
        assert s.log_level == "INFO"
        assert s.database_url == "sqlite:///./data/orchestrator.db"
        assert s.workspaces_root == "projects"
    finally:
        _clear_caches()


def test_config_host_and_port_defaults(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    _clear_caches()
    try:
        s = get_settings()
        assert s.host == "0.0.0.0"
        assert s.port == 8000
    finally:
        _clear_caches()


def test_config_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Case Sensitive Check")
    _clear_caches()
    try:
        assert get_settings().app_name == "Case Sensitive Check"
    finally:
        _clear_caches()


def test_config_ignores_unknown_environment_variables(monkeypatch):
    monkeypatch.setenv("SOME_UNKNOWN_SETTING", "should-not-break")
    monkeypatch.setenv("ANOTHER_RANDOM_VAR", "123")
    _clear_caches()
    try:
        s = get_settings()
        assert s.app_name == "AI CTO Hub Orchestrator"
    finally:
        _clear_caches()


def test_config_port_type_is_int(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    _clear_caches()
    try:
        assert get_settings().port == 9000
        assert isinstance(get_settings().port, int)
    finally:
        _clear_caches()


# ---------- database session lifecycle ----------


def test_session_is_closed_after_request(client):

    c, _, _ = client
    c.get("/projects")
    # The dependency must yield a fresh, closed session per request.
    # A second request must not hit a "session is closed" error.
    r = c.get("/projects")
    assert r.status_code == 200


def test_session_rolls_back_on_error(client, monkeypatch):
    """A failed commit must leave the database clean for the next request."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    c, _, _ = client

    def _boom(*args, **kwargs):
        raise IntegrityError("INSERT ...", {}, Exception("boom"))

    monkeypatch.setattr(Session, "commit", _boom)
    r = c.post("/projects", json={"name": "rollback-me"})
    assert r.status_code == 409
    monkeypatch.undo()
    # Next request must work with a clean session and no stale row.
    r = c.get("/projects")
    assert r.status_code == 200
    assert r.json() == []


def test_engine_pool_is_usable_after_shutdown(client):
    """After TestClient closes (lifespan dispose), the engine must still work."""
    from app.db import get_engine

    _, _, _ = client
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


# ---------- error handling ----------


def test_unhandled_error_returns_json_500(client):
    """The global exception handler should return a JSON 500 response."""
    c, _, _ = client

    # Register a temporary route for testing the error handler
    @app.get("/_boom")
    async def boom():
        raise RuntimeError("intentional test error")

    try:
        r = c.get("/_boom")
        assert r.status_code == 500
        assert r.json() == {"detail": "Internal server error"}
    finally:
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) != "/_boom"]


def test_validation_error_response_structure(client):
    """A 422 must use the standard FastAPI error shape with a field location."""
    c, _, _ = client
    r = c.post("/projects", json={"name": "../evil"})
    assert r.status_code == 422
    body = r.json()
    assert "detail" in body
    assert isinstance(body["detail"], list)
    first = body["detail"][0]
    assert set(first) >= {"type", "loc", "msg", "input"}
    assert "name" in first["loc"]
    assert "path separator" in first["msg"].lower() or "letter or digit" in first["msg"].lower()


def test_conflict_response_body(client):
    """A 409 must carry a JSON detail message, not an empty body."""
    c, _, _ = client
    c.post("/projects", json={"name": "conflict-body"})
    r = c.post("/projects", json={"name": "conflict-body"})
    assert r.status_code == 409
    assert r.json() == {"detail": "Project name already exists"}


def test_not_found_response_body(client):
    c, _, _ = client
    import uuid

    r = c.get(f"/projects/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json() == {"detail": "Project not found"}
