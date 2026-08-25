"""Database engine, session factory, and initialization helpers.

SQLite is the default database. The engine is created from DATABASE_URL, so
switching to PostgreSQL later is a configuration change (plus installing a
PostgreSQL driver such as psycopg).
"""

import logging
from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base class for all ORM models (added in later phases)."""


@lru_cache
def get_engine() -> Engine:
    """Create (once) the SQLAlchemy engine for the configured database URL."""
    url = get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Create (once) a session factory bound to the configured engine."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Ensure the database schema exists. Safe to call repeatedly."""
    # Import models so any defined tables register on Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())
    logger.debug("database schema ensured")


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session and closes it."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def check_database() -> bool:
    """Return True when the configured database answers a trivial query."""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


def dispose_engine() -> None:
    """Dispose the engine's connection pool (used on application shutdown)."""
    get_engine().dispose()
