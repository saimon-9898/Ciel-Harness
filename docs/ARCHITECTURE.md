# Architecture

## System context

```
┌─────────────────────────────────────────────────────┐
│                   AI CTO Hub                         │
│                                                      │
│  ┌──────────────┐     ┌───────────────────────────┐  │
│  │   Dashboard   │     │      Orchestrator API      │  │
│  │  (Phase 2+)   │────▶│  (FastAPI, this project)   │  │
│  └──────────────┘     │                            │  │
│                       │  - Configuration (env)      │  │
│                       │  - Structured logging       │  │
│                       │  - SQLAlchemy / SQLite      │  │
│                       │  - Error handling           │  │
│                       │  - /health endpoint         │  │
│                       └───────┬────────────────────┘  │
│                               │                      │
│                       ┌───────▼────────────────────┐  │
│                       │    Coding Agents            │  │
│                       │  (OpenHands, etc. Phase 2+) │  │
│                       └────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**Phase 1 delivers only the Orchestrator API box.** No dashboard, no agent
execution, no autonomous behaviour.

## Layer overview

### 1. Configuration (`orchestrator/app/config.py`)

- Uses **pydantic-settings** to read from environment variables and an
  optional `.env` file at the repository root.
- `Settings` class with typed fields: `database_url`, `environment`,
  `log_level`, `host`, `port`, etc.
- `get_settings()` is cached with `@lru_cache`; tests can call
  `cache_clear()` to load overrides.

**Key setting:** `DATABASE_URL` defaults to
`sqlite:///./data/orchestrator.db`. Relative SQLite paths resolve against the
process working directory, so run local development from the repository root.
Docker Compose supplies an absolute path (`sqlite:////app/data/orchestrator.db`).

### 2. Structured logging (`orchestrator/app/logging_config.py`)

- Zero third-party dependencies. A custom `JsonFormatter` emits each log
  record as a single JSON line to stdout.
- Extra keyword arguments passed to `logger.info("msg", extra={...})` are
  included in the JSON payload automatically.
- Configurable via `LOG_LEVEL`.

### 3. Database layer (`orchestrator/app/db.py`)

- **SQLAlchemy 2.0** `DeclarativeBase` for future ORM models.
- Engine and session factory are created lazily and cached with `@lru_cache`.
- `init_db()` imports models and calls `Base.metadata.create_all()`. This is
  invoked during application startup.
- `check_database()` executes `SELECT 1` — used by tests and the health check.
- `get_session()` is a FastAPI dependency ready for future route handlers.
- **PostgreSQL migration:** change `DATABASE_URL` and add the appropriate
  driver (`psycopg`) to `requirements.txt`. SQLAlchemy abstracts the rest.

### 4. API layer (`orchestrator/app/main.py`)

| Endpoint   | Method | Description                  |
|------------|--------|------------------------------|
| `/health`  | GET    | Liveness probe               |

**Error handling:** A global `Exception` handler catches unhandled
exceptions, logs them with structured logging, and returns a JSON 500
response. This is a foundation; specific handlers for HTTP errors,
validation, etc. are added in later phases.

### 5. Application lifecycle

Startup:
1. Configure structured logging.
2. Initialize the database (create tables if they do not exist).

Shutdown:
1. Dispose the SQLAlchemy engine (close the connection pool).

### 6. Docker

- **`orchestrator/Dockerfile`** builds a `python:3.12-slim` image. The app
  runs as an unprivileged user (`appuser`, uid 1000).
- **`docker-compose.yml`** exposes port 8000, mounts `./data:/app/data` for
  database persistence, and includes a healthcheck that curls `/health`.
- Environment variables are configurable via `.env` and docker-compose
  `${VAR:-default}` substitution.

### 7. Security

- The container runs as a non-root user.
- The host filesystem mount is restricted to `./data` only.
- No secrets are hard-coded; `.env` is gitignored.
- The application does not execute arbitrary commands, connect to production
  systems, or implement autonomous agent execution.

## Future integration: OpenHands

OpenHands provides:

- **REST API (Cloud):** documented at
  https://docs.openhands.dev/openhands/usage/cloud/cloud-api
- **Software Agent SDK:** Python and REST APIs for building agents that work
  with code. See https://docs.openhands.dev/sdk

Phase 2+ will integrate one or both of these interfaces. No OpenHands
dependency is installed in Phase 1.

## PostgreSQL migration path

1. Add `psycopg` (or `asyncpg`) to `orchestrator/requirements.txt`.
2. Set `DATABASE_URL` to
   `postgresql+psycopg://user:password@host:5432/orchestrator`.
3. Remove the SQLite-specific `check_same_thread` connect_args (the engine
   creation logic already handles this via URL prefix detection).
4. No code changes are needed — SQLAlchemy handles dialect differences.

## Known limitations

- Docker is not available on the current development machine, so
  containerised verification was not performed.
- The database layer is synchronous; an async engine (e.g. `aiosqlite`) can
  be added later if needed for high-concurrency scenarios.
- No migration tool (Alembic) is configured yet — tables are created via
  `Base.metadata.create_all()`. Alembic should be introduced when models are
  added.