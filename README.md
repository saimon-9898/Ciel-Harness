# AI CTO Hub — Phase 2: Project Management & Workspace Isolation

A self-hosted AI coding-agent orchestration platform. This repository contains
the backend foundation that will later orchestrate OpenHands and other coding
agents.

**Phase 1 delivered:** a minimal FastAPI backend with structured logging,
database initialization, configuration, error handling, a liveness endpoint,
and Docker infrastructure.

**Phase 2 delivers:** the project model, a projects API, and a
security-hardened workspace service that gives every project its own isolated
directory on disk.

No task or agent execution, no autonomous mode, and no dashboard are
implemented yet.

---

## Requirements

- **Python 3.12+**
- **Docker & Docker Compose** (for containerized deployment)
- Local development uses **SQLite**; PostgreSQL migration is a configuration
  change in a later phase.

---

## Installation (local development)

```bash
# 1. Clone / enter the project.
cd ai-cto

# 2. Create a virtual environment and activate it.
python3 -m venv .venv
source .venv/bin/activate

# 3. Install runtime and development dependencies.
pip install -r orchestrator/requirements-dev.txt

# 4. (Optional) Copy the environment template.
cp .env.example .env
```

---

## Running with Docker Compose

```bash
cd ai-cto
docker compose up -d
```

The orchestrator is reachable at **http://localhost:8000**.

### Health check

```bash
curl http://localhost:8000/health
# → {"status": "ok"}
```

### Shutdown

```bash
docker compose down
```

---

## Running locally (without Docker)

```bash
cd ai-cto
source .venv/bin/activate
uvicorn orchestrator.app.main:app --reload
```

> **Note:** The default `DATABASE_URL` (`sqlite:///./data/orchestrator.db`) and
> `WORKSPACES_ROOT` (`projects/`) are relative to the process working
> directory. Run from the repository root so `./data` and `./projects` resolve
> correctly. Alternatively, set absolute paths.

---

## Projects API

| Endpoint                | Method | Description                              |
|-------------------------|--------|------------------------------------------|
| `/projects`             | POST   | Create a project and its workspace       |
| `/projects`             | GET    | List all projects                        |
| `/projects/{project_id}`| GET    | Fetch a single project by UUID           |

### Create a project

```bash
curl -X POST http://localhost:8000/projects \
  -H "Content-Type: application/json" \
  -d '{"name": "project-a", "repository_url": "https://github.com/acme/project-a"}'
```

```json
{
  "id": "3f2c...uuid...",
  "name": "project-a",
  "repository_url": "https://github.com/acme/project-a",
  "repository_path": null,
  "default_branch": "main",
  "status": "created",
  "created_at": "2026-08-25T11:00:00Z",
  "updated_at": "2026-08-25T11:00:00Z"
}
```

### List projects

```bash
curl http://localhost:8000/projects
```

### Get a project

```bash
curl http://localhost:8000/projects/<project-id>
```

### Project names

Project names become workspace directory names, so they must start with a
letter or digit and contain only letters, digits, `.`, `_` or `-`. Names
containing separators, `..`, spaces, or other unsafe characters are rejected
with `422`.

---

## Workspace isolation

Every project owns a dedicated workspace directory:

```
projects/
├── project-a/
└── project-b/
```

The `WorkspaceService` (`orchestrator/app/workspaces.py`) is the only way to
resolve filesystem paths for a project. It enforces:

- **Path traversal rejection** — `..` cannot escape the workspace.
- **Absolute-path injection rejection** — absolute paths are allowed only when
  they resolve inside the owning project's workspace.
- **Symlink escape rejection** — symlinks that resolve outside the workspace
  are rejected.
- **Cross-project isolation** — paths are verified against the owning
  project's workspace directory, so project A can never reach project B's
  files.

The projects root is server configuration (`WORKSPACES_ROOT`), never an API
parameter. No API endpoint in Phase 2 exposes raw filesystem access.

---

## Testing

```bash
cd ai-cto
source .venv/bin/activate
python -m pytest
```

### Linting and formatting

```bash
ruff check .                   # Lint
ruff format --check .          # Check formatting (dry-run)
ruff format .                  # Format
```

---

## Project structure

```
ai-cto/
├── README.md
├── .gitignore
├── .env.example               # Environment variable template
├── docker-compose.yml
├── pyproject.toml             # pytest + ruff config
├── conftest.py                # pytest root conftest (sys.path bootstrap)
├── docs/
│   └── ARCHITECTURE.md        # Architecture documentation
├── orchestrator/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   └── app/
│       ├── __init__.py
│       ├── main.py            # FastAPI app, routes, error handlers
│       ├── api.py             # Projects API router
│       ├── config.py          # Pydantic-settings configuration
│       ├── db.py              # SQLAlchemy engine, session, helpers
│       ├── logging_config.py  # Structured JSON logging
│       ├── models.py          # Project model (more models in later phases)
│       ├── schemas.py         # Pydantic request/response schemas
│       └── workspaces.py      # WorkspaceService (path isolation)
├── tests/
│   ├── conftest.py            # Shared test fixtures
│   ├── test_health.py         # Phase 1 test suite
│   └── test_projects.py       # Project + workspace isolation tests
├── data/                      # SQLite database (gitignored, bind-mounted)
├── projects/                  # Per-project workspaces (gitignored, bind-mounted)
└── logs/                      # Reserved for future use
```

---

## Security notes

- The container runs as an unprivileged user (`appuser`, uid 1000).
- Only `./data` and `./projects` are bind-mounted into the container.
- No secrets, credentials, or API keys are hard-coded anywhere.
- The `.env` file is gitignored; only `.env.example` is tracked.
- Filesystem access is mediated by `WorkspaceService`; API parameters can
  never become unrestricted filesystem paths.

---

## Known limitations (Phase 2)

- Docker is not installed on the current development machine — the Dockerfile
  and docker-compose.yml are syntactically validated but not run.
- No task/agent execution, no dashboard, no autonomous behaviour.
- SQLite is the only database driver provided; PostgreSQL requires a driver
  (`psycopg`) in a later phase.
- OpenHands is not integrated; the architecture doc notes its REST API / SDK
  for future reference.
- Workspace path checks are synchronous and not race-free against concurrent
  symlink swaps (TOCTOU); revisit before untrusted multi-user use.
