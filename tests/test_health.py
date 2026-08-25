"""Tests for application startup, /health, database init, and configuration loading."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import check_database, dispose_engine, get_engine, get_session_factory
from app.main import app


def _clear_caches():
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Yield a TestClient with a temporary SQLite database."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ENVIRONMENT", "test")
    _clear_caches()
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, db_path
    finally:
        dispose_engine()
        _clear_caches()


# ---------- health ----------


def test_health_returns_ok(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------- startup ----------


def test_startup_creates_database_file(client):
    """The lifespan initializer should create the SQLite database file."""
    c, db_path = client
    assert db_path.exists(), "Database file was not created during startup"


def test_startup_makes_database_queryable(client):
    c, _ = client
    assert check_database() is True


# ---------- database ----------


def test_database_initialization_is_idempotent(client):
    c, _ = client
    # init_db called once during startup; calling it again should not raise.
    from app.db import init_db

    init_db()  # second call
    assert check_database() is True


# ---------- configuration ----------


def test_config_loads_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'env.db'}")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    _clear_caches()
    try:
        s = get_settings()
        assert s.database_url == f"sqlite:///{tmp_path / 'env.db'}"
        assert s.environment == "production"
        assert s.log_level == "WARNING"
    finally:
        _clear_caches()


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
    finally:
        _clear_caches()


# ---------- error handling ----------


def test_unhandled_error_returns_json_500(client):
    """The global exception handler should return a JSON 500 response."""
    c, _ = client

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
