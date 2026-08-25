"""Shared pytest fixtures for the orchestrator."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import dispose_engine, get_engine, get_session_factory
from app.main import app


def _clear_caches():
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with an isolated temp database and temp projects root.

    Yields ``(client, db_path, projects_root)``.
    """
    db_path = tmp_path / "test.db"
    projects_root = tmp_path / "projects"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("WORKSPACES_ROOT", str(projects_root))
    monkeypatch.setenv("ENVIRONMENT", "test")
    _clear_caches()
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c, db_path, projects_root
    finally:
        dispose_engine()
        _clear_caches()
