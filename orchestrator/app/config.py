"""Environment-based application configuration.

Settings are read from environment variables and an optional ``.env`` file at
the project root. Values can be overridden at runtime, which keeps the
orchestrator deployable in different environments without code changes.

The default development database URL is ``sqlite:///./data/orchestrator.db``.
Relative SQLite paths resolve against the process working directory, so run
local development from the repository root (or use an absolute path).
PostgreSQL is supported later by switching DATABASE_URL (plus a driver).
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: ai-cto/ (parent of the orchestrator/ package directory).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "AI CTO Hub Orchestrator"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./data/orchestrator.db"
    # Root directory for per-project workspaces (e.g. projects/). This is
    # server configuration, never an API parameter.
    workspaces_root: str = "projects"
    host: str = "0.0.0.0"
    port: int = 8000

    # ---- OpenHands Cloud API (Phase 5) ----
    # Base URL of the OpenHands Cloud (or compatible) API V1.
    openhands_base_url: str = "https://app.all-hands.dev"
    # API key for the OpenHands Cloud API. This is a server secret: it is
    # loaded from the environment / .env file, never logged, never stored in
    # Agent.configuration (the Phase 4 secret blocklist rejects it), and
    # never serialized in API responses. ``repr=False`` keeps it out of
    # settings repr/logging.
    openhands_api_key: str | None = Field(default=None, repr=False)
    # Per-request timeout (connection + read) for OpenHands API calls.
    openhands_timeout: float = 30.0
    # How long to wait for a started conversation to become READY before
    # reporting a timeout (the Cloud API starts sandboxes asynchronously).
    openhands_start_timeout: float = 120.0
    # Interval between status polls.
    openhands_poll_interval: float = 5.0
    # Maximum wall-clock time an execution may run before the orchestrator
    # reports a timeout. This is a boundary on OUR wait, not a claim that the
    # provider stopped working.
    openhands_max_execution_seconds: float = 3600.0


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings (cacheable for test overrides)."""
    return Settings()


# Convenience singleton; import get_settings() directly when a fresh read is
# needed (e.g. after environment changes in tests).
settings = get_settings()
