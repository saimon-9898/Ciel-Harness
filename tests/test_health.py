"""Tests for application startup, /health, database init, and configuration loading."""

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
