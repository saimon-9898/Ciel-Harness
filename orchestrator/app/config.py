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
    host: str = "0.0.0.0"
    port: int = 8000


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings (cacheable for test overrides)."""
    return Settings()


# Convenience singleton; import get_settings() directly when a fresh read is
# needed (e.g. after environment changes in tests).
settings = get_settings()
