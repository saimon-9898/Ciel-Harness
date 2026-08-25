# AI CTO Hub — Phase 3: Task Engine

A self-hosted AI coding-agent orchestration platform. This repository contains
the backend foundation that will later orchestrate OpenHands and other coding
agents.

**Phase 1 delivered:** a minimal FastAPI backend with structured logging,
database initialization, configuration, error handling, a liveness endpoint,
and Docker infrastructure.

**Phase 2 delivered:** the project model, a projects API, and a
security-hardened workspace service that gives every project its own isolated
directory on disk.

**Phase 3 delivers:** the Task engine — tasks are created under a project,
transitioned through a deterministic state machine, queried, and cancelled.
There is **no agent execution yet**: tasks are stored and tracked only; nothing
runs them.

No agent execution, no autonomous mode, and no dashboard are implemented yet.

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

## Tasks API

| Endpoint                        | Method | Description                                   |
|---------------------------------|--------|-----------------------------------------------|
| `/tasks`                        | POST   | Create a task under a project                 |
| `/tasks/{task_id}`              | GET    | Fetch a single task by UUID                   |
| `/projects/{project_id}/tasks`  | GET    | List all tasks of a project                   |
| `/tasks/{task_id}/cancel`       | POST   | Cancel a cancellable task                     |

### Create a task

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{
        "project_id": "<project-uuid>",
        "parent_task_id": null,
        "objective": "Fix the login redirect",
        "instructions": "Reproduce, then patch tests/test_auth.py.",
        "constraints": ["No new dependencies"],
        "success_criteria": ["All tests pass"]
      }'
```

A new task is always created in state `CREATED`.

### Fetch a task

```bash
curl http://localhost:8000/tasks/<task-id>
```

### List a project's tasks

```bash
curl http://localhost:8000/projects/<project-id>/tasks
```

Tasks are returned in a deterministic order (`created_at`, `id`).

### Cancel a task

```bash
curl -X POST http://localhost:8000/tasks/<task-id>/cancel
```

Cancellation is **idempotent**: cancelling an already-cancelled task returns
`200` with the unchanged task; cancelling a terminal task (`COMPLETED`,
`FAILED`, `CANCELLED`) is rejected with `409`.

### Task state machine

The ten states and their allowed transitions are enforced by
`orchestrator/app/task_states.py`:

```
CREATED ──► PLANNED ──► QUEUED ──► RUNNING ──► WAITING_FOR_AGENT
                                             │
                                             ▼
                                        WAITING_FOR_REVIEW ──► COMPLETED
                                             │
                                             ▼
                                        WAITING_FOR_APPROVAL

Any non-terminal state may transition to CANCELLED.
```

`COMPLETED`, `FAILED`, and `CANCELLED` are terminal. Transitions are guarded by
a database-level compare-and-swap (`UPDATE ... WHERE status = <expected>`), so
concurrent transitions can never corrupt state: exactly one wins, the loser
receives `409`.

### Task fields

- `id`, `project_id`, `parent_task_id` — the task is owned by a project and may
  optionally sit under a parent task **in the same project**.
- `objective` (required, ≤ 1000 chars), `instructions` (optional, ≤ 4000),
  `constraints` / `success_criteria` (JSON lists, ≤ 50 items, ≤ 500 chars each).
- `status`, `created_at`, `updated_at`, `started_at`, `completed_at`,
  `agent_id`, `result`, `error` — all **server-controlled**.

### Parent / child rules

- A parent must exist and belong to the **same project** as its child.
- A task cannot be its own parent, and parent chains are checked for cycles.
- There is no task-update endpoint; parent/child relationships are fixed at
  creation.

### Validation and errors

| Condition                                              | HTTP status |
|--------------------------------------------------------|-------------|
| Project or task does not exist                         | `404`       |
| Malformed UUID, missing/oversized/blank fields         | `422`       |
| Invalid parent (missing, other project, self, cycle)   | `409`       |
| Illegal or conflicting state transition                | `409`       |

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
│       ├── api.py             # Projects + Tasks API routers
│       ├── config.py          # Pydantic-settings configuration
│       ├── db.py              # SQLAlchemy engine, session, helpers
│       ├── logging_config.py  # Structured JSON logging
│       ├── models.py          # Project + Task models
│       ├── schemas.py         # Pydantic request/response schemas
│       ├── task_states.py     # Task state machine (transitions, guards)
│       ├── task_service.py    # TaskService (create/query/transition/cancel)
│       └── workspaces.py      # WorkspaceService (path isolation)
├── tests/
│   ├── conftest.py            # Shared test fixtures
│   ├── test_health.py         # Phase 1 test suite
│   ├── test_projects.py       # Project + workspace isolation tests
│   ├── test_task_states.py    # State machine tests
│   ├── test_task_service.py   # Service-level Task tests
│   └── test_tasks_api.py      # Tasks API + adversarial tests
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

## Known limitations (Phase 3)

- Docker is not installed on the current development machine — the Dockerfile
  and docker-compose.yml are syntactically validated but not run. The
  Dockerfile layout (working directory `/app`, `app/` package, `data/` and
  `projects/` bind mounts) was validated by running the app from an identical
  local layout with `uvicorn`.
- **No agent execution**: tasks are stored and tracked; nothing executes them.
- **No task-update endpoint**: the state machine is advanced only by internal
  service calls, and only creation + cancellation are exposed over HTTP.
- `WAITING_FOR_APPROVAL` is reachable only from `WAITING_FOR_REVIEW` (a single
  added edge so the specified state is reachable at all).
- API-created tasks cannot form self-parent or cycle relationships (there is no
  update endpoint); the service layer still guards against them for future
  phases.
- SQLite is the only database driver provided; PostgreSQL requires a driver
  (`psycopg`) in a later phase. The Task engine uses plain SQLAlchemy Core
  UPDATE statements, so the compare-and-swap works unchanged on PostgreSQL.
- OpenHands is not integrated; the architecture doc notes its REST API / SDK
  for future reference.
- Workspace path checks are synchronous and not race-free against concurrent
  symlink swaps (TOCTOU); revisit before untrusted multi-user use.
