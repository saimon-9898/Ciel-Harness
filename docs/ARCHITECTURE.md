# Architecture

## System context

```
┌─────────────────────────────────────────────────────┐
│                   AI CTO Hub                         │
│                                                      │
│  ┌──────────────┐     ┌───────────────────────────┐  │
│  │   Dashboard   │     │      Orchestrator API      │  │
│  │  (Phase 3+)   │────▶│  (FastAPI, this project)   │  │
│  └──────────────┘     │                            │  │
│                       │  - Configuration (env)      │  │
│                       │  - Structured logging       │  │
│                       │  - SQLAlchemy / SQLite      │  │
│                       │  - Error handling           │  │
│                       │  - /health endpoint         │  │
│                       │  - Projects CRUD (Phase 2)  │  │
│                       │  - Workspace isolation      │  │
│                       └───────┬────────────────────┘  │
│                               │                      │
│                       ┌───────▼────────────────────┐  │
│                       │    Coding Agents            │  │
│                       │  (OpenHands, etc. Phase 3+) │  │
│                       └────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**Phase 1** delivered the Orchestrator API box minus projects/workspaces.
**Phase 2** adds project management and workspace isolation.
No dashboard, no agent execution, no autonomous behaviour.

---

## Layer overview

### 1. Configuration (`orchestrator/app/config.py`)

- Uses **pydantic-settings** to read from environment variables and an
  optional `.env` file at the repository root.
- `Settings` class with typed fields: `database_url`, `workspaces_root`,
  `environment`, `log_level`, `host`, `port`, etc.
- `get_settings()` is cached with `@lru_cache`; tests can call
  `cache_clear()` to load overrides.

**Key settings:**

| Variable          | Default                          | Description                              |
|-------------------|----------------------------------|------------------------------------------|
| DATABASE_URL      | `sqlite:///./data/orchestrator.db`| SQLAlchemy database URL                   |
| WORKSPACES_ROOT   | `projects`                       | Root directory for per-project workspaces |
| ENVIRONMENT       | `development`                    | Runtime environment label                 |
| LOG_LEVEL         | `INFO`                           | Logging verbosity                         |

Relative SQLite / workspace paths resolve against the process working
directory. Docker Compose supplies absolute container paths.

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
- `get_session()` is a FastAPI dependency yielding a database session.
- **PostgreSQL migration:** change `DATABASE_URL` and add the appropriate
  driver (`psycopg`) to `requirements.txt`. SQLAlchemy abstracts the rest.

### 4. Project model (`orchestrator/app/models.py`)

```python
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID]  # primary key, auto-generated
    name: Mapped[str]  # unique, validated as safe directory name
    repository_url: Optional[str]
    repository_path: Optional[str]
    default_branch: str  # default "main"
    status: str  # default "created"
    created_at: datetime
    updated_at: datetime
```

Project names are validated to start with a letter or digit and contain only
letters, digits, `.`, `_` or `-`. This prevents path separators, `..`, and
hidden names from becoming workspace directory names.

### 5. Schemas (`orchestrator/app/schemas.py`)

- `ProjectCreate` — input validation for POST /projects (name regex, field
  max-lengths).
- `ProjectOut` — response serialisation with `from_attributes=True` for ORM
  compatibility.

### 6. Projects API (`orchestrator/app/api.py`)

| Endpoint                | Method | Status | Description                              |
|-------------------------|--------|--------|------------------------------------------|
| `/projects`             | POST   | 201    | Create a project and its workspace       |
| `/projects`             | GET    | 200    | List all projects (ordered by created_at)|
| `/projects/{project_id}`| GET    | 200    | Fetch a single project                   |

- Duplicate project names → `409 Conflict`.
- Invalid project UUID → `422 Unprocessable Entity`.
- Unknown project UUID → `404 Not Found`.
- Unsafe project names → `422 Unprocessable Entity`.

### 7. Workspace service (`orchestrator/app/workspaces.py`)

Every project gets an isolated directory under `WORKSPACES_ROOT`:

```
projects/
├── project-a/
└── project-b/
```

The `WorkspaceService` provides the following operations:

#### `get_workspace(project) -> Path`

Returns the resolved workspace directory for the project. Validates that the
project name is safe (defense in depth) and that the resolved path stays
inside the configured projects root.

#### `create_workspace(project) -> Path`

Creates the workspace directory (idempotent) and re-verifies containment
after creation to catch symlink attacks on the directory itself.

#### `validate_workspace(project, path) -> Path`

Resolves a user-supplied path against the project's workspace and verifies
the final resolved path stays inside the workspace. This is the single
chokepoint for all filesystem access; it prevents:

| Threat                        | How it is blocked                                               |
|-------------------------------|----------------------------------------------------------------|
| Path traversal (`..`)         | `Path.resolve()` normalises `..`; containment check rejects it  |
| Absolute-path injection       | Absolute paths are allowed only if they resolve inside workspace|
| Symlink escape                | `resolve()` follows symlinks; containment check catches escapes |
| Cross-project access          | Verification is *per workspace*; project A cannot reach B's dir |

#### `ensure_root() -> Path`

Creates the configured projects root directory (idempotent) if it does not
exist. Called during application startup.

#### `remove_workspace(project) -> None`

Best-effort removal of a project's workspace directory. Only empty
directories are removed; non-empty directories are logged and left intact.
Used to clean up a workspace when project creation fails.

The projects root is server configuration (set via `WORKSPACES_ROOT` env
var), never an API parameter. No Phase 2 endpoint exposes raw filesystem
access to the caller.

**Known limitation (TOCTOU):** the containment checks are synchronous and
not race-free against concurrent symlink swaps.

- **The exact race:** `get_workspace()` resolves and verifies a path, then
  a caller uses it later. If a process that can write to the projects root
  or a workspace swaps a directory for a symlink pointing outside between
  resolution and use, the verified path can escape containment. The same
  window exists between `get_workspace()` and `mkdir()` in
  `create_workspace()`, which is why that method re-resolves and re-verifies
  the workspace *after* creation (defense in depth).
- **Why it exists:** closing the window fully requires directory-fd based
  operations (e.g. `openat2` with `RESOLVE_BENEATH`/`RESOLVE_NO_SYMLINKS`)
  or an OS-level sandbox, neither of which is portable or warranted at this
  stage.
- **Realistic impact:** LOW. Exploitation requires local write access to the
  projects root or a workspace directory, which already grants the ability
  to read and write those files directly. No Phase 2 endpoint exposes
  caller-controlled filesystem paths, so there is no untrusted-input vector
  through the API. The single-user, single-orchestrator operator is the only
  party able to write those directories.
- **Mitigation already present:** (1) `Path.resolve()` follows symlinks at
  check time; (2) `create_workspace()` re-verifies the resolved path after
  `mkdir`; (3) the projects root is server configuration, never an API
  parameter; (4) project names are regex-restricted to
  `[A-Za-z0-9._-]` so they cannot contain separators; (5) `validate_workspace`
  resolves and verifies every caller-supplied path and wraps raw filesystem
  errors. Lines 54 and 63 of `workspaces.py` (the two containment raises) are
  covered by tests, including a deterministic symlink-swap race simulation.
- **Why deferred:** eliminating the race requires an fd-based API redesign
  with `openat2`-style semantics. That is a Phase 3+ concern when
  multi-user access and agent-driven file writes make the attack surface
  real; revisit before adding any endpoint that writes caller-supplied paths.

### 8. API layer (`orchestrator/app/main.py`)

| Endpoint   | Method | Description            |
|------------|--------|------------------------|
| `/health`  | GET    | Liveness probe         |

**Error handling:** A global `Exception` handler catches unhandled
exceptions, logs them with structured logging, and returns a JSON 500
response.

### 9. Application lifecycle

Startup:
1. Configure structured logging.
2. Initialize the database (create tables if they do not exist).
3. Ensure the workspace root directory exists.

Shutdown:
1. Dispose the SQLAlchemy engine (close the connection pool).

### 10. Docker

- **`orchestrator/Dockerfile`** builds a `python:3.12-slim` image. The app
  runs as an unprivileged user (`appuser`, uid 1000). `/app/data`,
  `/app/logs`, and `/app/projects` are created in the image.
- **`docker-compose.yml`** exposes port 8000, mounts `./data:/app/data` and
  `./projects:/app/projects` for persistence, and includes a healthcheck that
  fetches `/health` with Python's `urllib`. Environment variables are
  configurable via `.env` and docker-compose `${VAR:-default}` substitution.

### 11. Security

- The container runs as a non-root user.
- The host filesystem mount is restricted to `./data` and `./projects`.
- No secrets are hard-coded; `.env` is gitignored.
- The application does not execute arbitrary commands, connect to production
  systems, or implement autonomous agent execution.
- Project names are validated by regex before becoming filesystem paths.
- The `WorkspaceService` is the single chokepoint for all filesystem access
  and enforces path containment with `Path.resolve()` + `relative_to()`.

---

## Future integration: OpenHands

OpenHands provides:

- **REST API (Cloud):** documented at
  https://docs.openhands.dev/openhands/usage/cloud/cloud-api
- **Software Agent SDK:** Python and REST APIs for building agents that work
  with code. See https://docs.openhands.dev/sdk

Phase 3+ will integrate one or both of these interfaces. No OpenHands
dependency is installed in Phase 2.

---

## PostgreSQL migration path

1. Add `psycopg` (or `asyncpg`) to `orchestrator/requirements.txt`.
2. Set `DATABASE_URL` to
   `postgresql+psycopg://user:password@host:5432/orchestrator`.
3. Remove the SQLite-specific `check_same_thread` connect_args (the engine
   creation logic already handles this via URL prefix detection).
4. No code changes are needed — SQLAlchemy handles dialect differences.

---

## Known limitations

- Docker is not available on the current development machine, so
  containerised verification was not performed.
- The database layer is synchronous; an async engine (e.g. `aiosqlite`) can
  be added later if needed for high-concurrency scenarios.
- No migration tool (Alembic) is configured yet — tables are created via
  `Base.metadata.create_all()`. Alembic should be introduced when models are
  added.
- Workspace path checks have a TOCTOU gap (symlink swaps between resolution
  and use). Documented in detail in section 7; acceptable for single-user
  operation and mitigated by post-mkdir re-verification. Revisit before
  adding any endpoint that writes caller-supplied paths.