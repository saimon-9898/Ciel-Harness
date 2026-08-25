# Architecture

## System context

```
┌─────────────────────────────────────────────────────┐
│                   AI CTO Hub                         │
│                                                      │
│  ┌──────────────┐     ┌───────────────────────────┐  │
│  │   Dashboard   │     │      Orchestrator API      │  │
│  │  (Phase 4+)   │────▶│  (FastAPI, this project)   │  │
│  └──────────────┘     │                            │  │
│                       │  - Configuration (env)      │  │
│                       │  - Structured logging       │  │
│                       │  - SQLAlchemy / SQLite      │  │
│                       │  - Error handling           │  │
│                       │  - /health endpoint         │  │
│                       │  - Projects CRUD (Phase 2)  │  │
│                       │  - Workspace isolation      │  │
│                       │  - Task engine (Phase 3)    │  │
│                       │  - Agent abstraction (P4)   │  │
│                       └───────┬────────────────────┘  │
│                               │  adapters report      │
│                               │  not_configured       │
│                       ┌───────▼────────────────────┐  │
│                       │    Coding Agents            │  │
│                       │  (OpenHands, Claude Code,   │  │
│                       │   Codex, Gemini — not       │  │
│                       │   connected until Phase 5)  │  │
│                       └────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**Phase 1** delivered the Orchestrator API box minus projects/workspaces.
**Phase 2** added project management and workspace isolation.
**Phase 3** added the Task engine: a deterministic task state machine with
creation, querying, and cancellation.
**Phase 4** adds the Agent abstraction: a closed provider enum, strict
contracts, an agent registry, honest non-fake adapters, a minimal
agent-management API, and non-executing task→agent assignment. No provider is
connected, no execution exists, no dashboard, no autonomous behaviour.

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

---

## 8. Task engine

**Phase 3 scope:** the Task engine manages task lifecycle data only. There is
**no agent execution** — nothing runs a task, and no agent is invoked. Tasks
are created, queried, transitioned through a deterministic state machine, and
cancelled.

### 8.1 Task model (`orchestrator/app/models.py`)

```python
class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID]            # primary key, server-generated
    project_id: Mapped[uuid.UUID]    # FK -> projects.id (CASCADE)
    parent_task_id: Mapped[uuid.UUID | None]  # self-FK -> tasks.id (SET NULL)
    objective: Mapped[str]           # required, <= 1000 chars
    instructions: Mapped[str | None] # optional, <= 4000 chars
    constraints: Mapped[list[str] | None]      # JSON, <= 50 items, <= 500 chars each
    success_criteria: Mapped[list[str] | None] # JSON, same bounds
    status: Mapped[str]              # default CREATED
    agent_id: Mapped[uuid.UUID | None]  # reserved; never set by the API
    result: Mapped[str | None]       # reserved; never set by the API
    error: Mapped[str | None]        # set by fail transitions (internal)
    created_at / updated_at / started_at / completed_at: datetime
```

- `project_id` has `ON DELETE CASCADE`; `parent_task_id` has `ON DELETE SET
  NULL`, so deleting a project removes its tasks and children are never
  orphaned.
- The JSON columns are SQLAlchemy `JSON` types, portable to PostgreSQL.

### 8.2 State machine (`orchestrator/app/task_states.py`)

Ten named states exactly as specified:

```
CREATED, PLANNED, QUEUED, RUNNING, WAITING_FOR_AGENT,
WAITING_FOR_REVIEW, WAITING_FOR_APPROVAL, COMPLETED, FAILED, CANCELLED
```

Transition table:

```
CREATED            -> PLANNED, CANCELLED
PLANNED            -> QUEUED, CANCELLED
QUEUED             -> RUNNING, CANCELLED
RUNNING            -> WAITING_FOR_AGENT, FAILED, CANCELLED
WAITING_FOR_AGENT  -> RUNNING, WAITING_FOR_REVIEW, FAILED, CANCELLED
WAITING_FOR_REVIEW -> COMPLETED, WAITING_FOR_APPROVAL, FAILED, CANCELLED
WAITING_FOR_APPROVAL -> RUNNING, FAILED, CANCELLED
COMPLETED / FAILED / CANCELLED -> (terminal; no outgoing edges)
```

One deliberate addition to the specified list:
`WAITING_FOR_REVIEW -> WAITING_FOR_APPROVAL`, so that `WAITING_FOR_APPROVAL`
is reachable (the specification used it only as a source state). This is
documented as a known limitation/decision.

`task_states.py` exposes `validate_transition(current, new)` (raises
`TaskStateError`), `allowed_transitions(state)`, `is_terminal(state)`, and
`can_cancel(state)`. The module is pure — no database dependency.

### 8.3 Task service (`orchestrator/app/task_service.py`)

`TaskService` implements the engine:

- **`create_task`** — validates the project exists, validates the parent
  (exists, same project, not self, no ancestor cycle), then creates a task in
  `CREATED` with a fresh server-generated UUID. All other fields are
  server-controlled.
- **Transition methods** (`plan_task`, `queue_task`, `start_task`,
  `wait_for_agent`, `resume_task`, `submit_for_review`, `request_approval`,
  `approve_task`, `complete_task`, `fail_task`, `cancel_task`) all funnel into
  **`_transition`**, which:

  1. loads the task,
  2. calls `validate_transition(current, new)`,
  3. runs a **compare-and-swap** UPDATE:
     `UPDATE tasks SET status=:new, updated_at=:now WHERE id=:id AND status=:expected`,
  4. if `rowcount == 0`, raises `TaskStateConflictError` (someone else
     transitioned concurrently) — the DB, not a Python lock, is the arbiter.

- **`cancel_task`** is idempotent for an already-cancelled task (returns the
  task, `200`) and rejects terminal tasks (`409`). This keeps duplicate
  cancels safe without client-side dedup state.
- Custom exceptions: `TaskNotFoundError`, `ProjectNotFoundError`,
  `ParentTaskNotFoundError`, `InvalidParentTaskError`,
  `TaskStateConflictError` (service layer) and `TaskStateError` (state
  machine).

### 8.4 Tasks API (`orchestrator/app/api.py`)

| Endpoint                       | Method | Success | Errors                              |
|--------------------------------|--------|---------|-------------------------------------|
| `/tasks`                       | POST   | 201     | 404 (project), 409 (parent/cycle), 422 (validation) |
| `/tasks/{task_id}`             | GET    | 200     | 404, 422                            |
| `/projects/{project_id}/tasks` | GET    | 200     | 404 (project), 422                  |
| `/tasks/{task_id}/cancel`      | POST   | 200     | 404, 409 (terminal/conflict), 422   |

- `TaskCreate` accepts **only** `project_id`, `parent_task_id`, `objective`,
  `instructions`, `constraints`, `success_criteria`. Extra keys (including
  `status`, `result`, `agent_id`, timestamps) are ignored by Pydantic —
  **mass-assignment protection** is enforced by construction: the schema has
  no such fields, so they can never be set through the API.
- There is **no PUT/PATCH** for tasks. Status changes are only possible
  through the state machine, and only `cancel` is exposed over HTTP in Phase
  3.
- Error mapping: 404 for missing resources, 409 for invalid parents,
  terminal-state cancels, and CAS conflicts, 422 for malformed input, 500 as
  an unhandled fallback. Every status is declared in OpenAPI
  (`_TASK_ERROR_RESPONSES`) and verified by contract tests.

### 8.5 Concurrency

SQLite serializes writers, but the CAS UPDATE remains correct on any
database, including PostgreSQL: two concurrent transitions of the same task
both execute, and exactly one matches `WHERE status = :expected`; the loser
observes `rowcount == 0` and returns `409`. No application-level lock is
held. Tests exercise this with real threads racing cancel-vs-fail and
duplicate cancels.

### 8.6 Audit hooks

Task transitions log a structured `task transition` event (task id, project
id, from/to status). This is the minimum hook for the Phase 9 event system;
no event table or pub/sub exists yet.

---

## 9. API layer (`orchestrator/app/main.py`)

| Endpoint   | Method | Description            |
|------------|--------|------------------------|
| `/health`  | GET    | Liveness probe         |

The router include list is: `projects_router` (Phase 2), `tasks_router`
(Phase 3), and `agents_router` (Phase 4).

**Error handling:** A global `Exception` handler catches unhandled
exceptions, logs them with structured logging, and returns a JSON 500
response.

### 9.1 Application lifecycle

Startup:
1. Configure structured logging.
2. Initialize the database (create tables if they do not exist).
3. Ensure the workspace root directory exists.

Shutdown:
1. Dispose the SQLAlchemy engine (close the connection pool).

### 9.2 Docker

- **`orchestrator/Dockerfile`** builds a `python:3.12-slim` image. The app
  runs as an unprivileged user (`appuser`, uid 1000). `/app/data`,
  `/app/logs`, and `/app/projects` are created in the image.
- **`docker-compose.yml`** exposes port 8000, mounts `./data:/app/data` and
  `./projects:/app/projects` for persistence, and includes a healthcheck that
  fetches `/health` with Python's `urllib`. Environment variables are
  configurable via `.env` and docker-compose `${VAR:-default}` substitution.

### 9.3 Security

- The container runs as a non-root user.
- The host filesystem mount is restricted to `./data` and `./projects`.
- No secrets are hard-coded; `.env` is gitignored.
- The application does not execute arbitrary commands, connect to production
  systems, or implement autonomous agent execution.
- Project names are validated by regex before becoming filesystem paths.
- The `WorkspaceService` is the single chokepoint for all filesystem access
  and enforces path containment with `Path.resolve()` + `relative_to()`.
- Task lifecycle fields (`status`, `result`, timestamps) cannot be written
  through the API — `TaskCreate` simply has no such fields
  (mass-assignment protection by construction). `agent_id` **is** a valid
  input since Phase 4: it is validated (agent must exist and be usable)
  rather than silently dropped.
- Agent `status`, `id`, and timestamps are server-controlled; `AgentCreate`
  has no such fields and the registry always starts agents `UNAVAILABLE`.
- Agent configuration rejects secret-looking keys at the boundary and
  `AgentOut` redacts them defensively; no plaintext secrets are stored.
- Unknown providers and capabilities are rejected by closed enums.

---

## 10. Agent abstraction (Phase 4)

The Agent layer is a provider-independent boundary between the Task Engine and
coding-agent providers. **It performs no execution**: it registers agent
definitions, resolves them to adapters, and probes health honestly.

### 10.1 Provider vocabulary (`orchestrator/app/agent_providers.py`)

- `AgentProvider` is a **closed `StrEnum`**: `openhands`, `claude_code`,
  `codex`, `gemini`. Unknown provider strings are rejected at the API boundary
  (422) and by `resolve_adapter` (`UnsupportedProviderError`).
- `AgentCapability` is a closed `StrEnum`: `code`, `test`, `shell`, `git`,
  `network`. Capabilities are **advertised only** in Phase 4; a later phase
  must gate them at execution time.
- Agent statuses are plain strings (`AVAILABLE`, `BUSY`, `UNAVAILABLE`,
  `ERROR`, `DISABLED`). Only `AVAILABLE` is usable for task assignment
  (`USABLE_AGENT_STATUSES`).
- `validate_agent_configuration` enforces bounds (≤20 keys, keys ≤64 chars,
  values ≤512 chars) and rejects secret-looking keys via a blocklist regex
  (`api_key`, `token`, `secret`, `password`, `auth`, `private_key`, ...).
- `redact_secrets` is defense-in-depth for responses: even if a secret key
  reached the DB, `AgentOut` strips it.

### 10.2 Contracts (`orchestrator/app/agent_contracts.py`)

Strict Pydantic models define the execution boundary for Phase 5, with no
provider-specific concepts leaking through:

- `AgentTaskRequest` — what the engine would hand to an adapter (task/project
  ids, objective, instructions, constraints, success criteria), all bounded.
- `AgentTaskHandle` — **frozen** opaque provider-side reference; the engine
  never inspects it.
- `AgentExecutionState`, `AgentStatusResult`, `AgentResult` — provider-reported
  status/result vocabulary.
- `AgentHealthState` (`available`/`unavailable`/`error`/`not_configured`/
  `unsupported`) and `AgentHealth` — probe results.

### 10.3 Errors (`orchestrator/app/agent_errors.py`)

`AgentError` base with typed subclasses: `AgentNotFoundError`,
`AgentNameConflictError`, `UnsupportedProviderError`,
`InvalidAgentConfigurationError`, `AgentUnavailableError`,
`ProviderNotConfiguredError`, `AgentTimeoutError`, `AgentProviderError`,
`AgentMalformedResponseError`, `AgentCancellationError`. No failure is ever
converted into a success.

### 10.4 Adapters (`orchestrator/app/adapters/`)

`AgentAdapter` (ABC) defines the provider-independent interface:
`check_health`, `start_task`, `get_status`, `get_result`, `cancel_task`.
Four concrete adapters (one per provider) exist. **All are honest**:

- `check_health` returns `AgentHealthState.NOT_CONFIGURED`.
- Every execution method raises `ProviderNotConfiguredError`.

No adapter fabricates a start, a status, or a result. Phase 5 will implement
the real integrations behind these boundaries.

### 10.5 Registry (`orchestrator/app/agent_manager.py`)

- `ADAPTER_REGISTRY` maps each provider to its adapter class; `resolve_adapter`
  raises `UnsupportedProviderError` for anything unknown.
- `AgentManager.register_agent` validates provider and configuration, enforces
  a unique name, and starts the record `UNAVAILABLE` — claiming availability
  when no provider is connected would be fake.
- `list_agents` is deterministic (ordered by name).
- `check_health` resolves the adapter and runs its probe.
- `get_agent_with_adapter` returns the abstraction, never a provider-specific
  implementation.

### 10.6 Agent model and API

```python
class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[uuid.UUID]      # primary key
    name: Mapped[str]          # unique, 1-100 chars
    provider: Mapped[str]      # indexed, one of the closed enum values
    status: Mapped[str]        # default "UNAVAILABLE"
    capabilities: Mapped[list[str]]  # JSON, advertised only
    configuration: Mapped[dict | None]  # JSON, non-secret settings only
    created_at / updated_at: datetime
```

Agents are **global infrastructure** — they serve the whole orchestrator and
are shared across projects. The model carries no project scope; Phase 5
execution derives the target repository from the task's own project workspace
git `origin` (server-side), which enforces workspace isolation per task
assignment.

| Endpoint                    | Method | Status | Description                              |
|-----------------------------|--------|--------|------------------------------------------|
| `/agents`                   | POST   | 201    | Register an agent (starts `UNAVAILABLE`) |
| `/agents`                   | GET    | 200    | List agents (ordered by name)            |
| `/agents/{agent_id}`        | GET    | 200    | Fetch one agent                          |
| `/agents/{agent_id}/health` | GET    | 200    | Honest provider probe (real for OpenHands, `not_configured` for the rest) |

There is **no execute endpoint on the agents router**; execution happens on
tasks via `POST /tasks/{id}/execute` (section 11.6).

### 10.7 Task→agent assignment (no execution)

`TaskCreate.agent_id` and `TaskService.create_task(agent_id=...)` record a
reference to an agent:

1. `_validate_agent_assignment` checks the agent exists
   (`AgentNotFoundError` → 404) and is `AVAILABLE`
   (`AgentUnavailableError` → 409).
2. The reference is stored on the task; the task stays in `CREATED`.

No execution, polling, or cancellation is triggered by assignment. The endpoint
catches `AgentError` alongside `TaskError`/`TaskStateError` so assignment
failures map to clean HTTP errors instead of 500s.

---

## Phase 5: real OpenHands execution

Phase 5 connects the Phase 4 agent abstraction to the **real OpenHands Cloud
API V1** and drives Task engine transitions from provider results. The whole
path is one complete execution loop: Task → Agent → OpenHands → correct
project workspace → truthful result → Task Engine. Nothing is faked.

### 11.1 OpenHands Cloud API V1 (target interface)

Verified against docs.openhands.dev (2026-08-25):

- `POST /api/v1/app-conversations` — start a conversation asynchronously.
  Returns a *start task* object with `id`, `status`, and eventually
  `app_conversation_id`.
- `GET /api/v1/app-conversations/start-tasks?ids=ID` — poll the start task
  until `READY` (conversation id populated) or `ERROR`.
- `GET /api/v1/app-conversations?ids=ID` — poll the conversation; its
  `execution_status` is `idle`/`running`/`paused`/`waiting_for_confirmation`/
  `finished`/`error`/`stuck`/`deleting`.
- `GET /api/v1/app-conversations/search?limit=N` — health probe.
- Auth: `X-Access-Token` header (reference) and `Authorization: Bearer`
  (overview guide); both are sent with the same token value.
- **No documented cancellation endpoint** — cancellation is truthfully
  unsupported.

The adapter never blocks an HTTP request on the agent's work: `start_task`
returns an opaque handle (the `app_conversation_id`) and `get_status` reports
the provider's own state. Success is reported only when the provider itself
reports `finished`, never inferred from HTTP 200.

### 11.2 Adapter (`orchestrator/app/adapters/openhands.py`)

`OpenHandsAdapter` implements the `AgentAdapter` boundary from Phase 4:

- `is_configured()` — true when a base URL and API key are set.
- `check_health()` — real authenticated probe; `AVAILABLE` only on auth'd 200,
  `UNAVAILABLE` on 401/403, `ERROR` on unreachable/malformed, `NOT_CONFIGURED`
  without a key.
- `start_task(request)` — posts the documented payload shape
  (`initial_message.content[].text`, `selected_repository`, `selected_branch`,
  `trigger`, `title`), polls the start task until READY (bounded by
  `openhands_start_timeout`), returns an `AgentTaskHandle` whose `reference`
  is the provider's `app_conversation_id`.
- `get_status(handle)` — maps `execution_status` to the provider-independent
  `AgentExecutionState` (finished→COMPLETED, error/stuck or sandbox
  error→FAILED, waiting_for_confirmation→RUNNING (blocking), idle/running/
  paused→RUNNING, deleting→UNKNOWN).
- `get_result(handle)` — provider-reported terminal state; COMPLETED only for
  `finished`.
- `cancel_task(handle)` — raises `AgentCancellationError` truthfully.

Transport failures map to the Phase 4 error model: timeouts →
`AgentTimeoutError`; 401/403/404/422/5xx and connection errors →
`AgentProviderError`; malformed bodies → `AgentMalformedResponseError`. The
API key never appears in exception messages or logs.

### 11.3 Workspace repository resolution (`orchestrator/app/workspaces.py`)

`WorkspaceService.resolve_repository(project)` derives the `owner/repo` target
**server-side** from the project workspace's git `origin` remote:

- `git -C <workspace> remote get-url origin` runs with an argument list (no
  shell), so a malicious remote URL can never be interpreted as a command.
- `_parse_repository` supports https, ssh (scp-like and `ssh://`), and bare
  `owner/repo` forms and requires a strict `owner/repo` pattern. Traversal,
  extra path segments, option-shaped values, and shell metacharacters are
  rejected.
- The branch is the project's `default_branch` (server field), validated
  against a safe git branch pattern.
- A workspace without an `origin` remote makes `execute` fail with `409`.

A client cannot influence the repository string through any API parameter;
the workspace is the single source of truth.

### 11.4 Execution service (`orchestrator/app/execution_service.py`)

`ExecutionService` is the only place that starts, polls, and finishes provider
work. It composes `TaskService` (never bypassing the state machine),
`AgentManager`, and `WorkspaceService`.

`execute_task(session, task_id)`:

1. Validates the task exists, has an agent, the agent is usable
   (`AVAILABLE`), and the project exists.
2. Resolves `(repository, branch)` from the workspace git origin.
3. Builds the adapter via `adapter_factory` (default wires only OpenHands for
   real).
4. Claims the task execution with `TaskService.start_task` (CAS
   `QUEUED -> RUNNING`) — this is the lock that prevents double execution.
5. Claims the agent with `AgentManager.claim_agent` (CAS `AVAILABLE -> BUSY`)
   — one agent never runs two executions at once.
6. Calls `adapter.start_task(request)`; on any `AgentError` the task is
   failed and the agent released before re-raising.
7. Persists `task.execution_reference` / `task.execution_status` and moves the
   task to `WAITING_FOR_AGENT`.

`refresh_execution(session, task_id)` polls only `WAITING_FOR_AGENT` tasks
with a stored reference:

- RUNNING/UNKNOWN → stay waiting; the orchestrator-side patience limit
  (`openhands_max_execution_seconds`, checked against `started_at`) fails the
  task truthfully (the provider conversation may still run in the OpenHands
  UI).
- COMPLETED → `WAITING_FOR_REVIEW`, agent released.
- FAILED → `FAILED`, agent released.
- Provider error/timeout while polling → `FAILED`, agent released.

`get_execution(session, task_id)` is read-only; it never polls the provider.

### 11.5 Lifecycle and concurrency

```
Task:    QUEUED -> RUNNING -> WAITING_FOR_AGENT -> WAITING_FOR_REVIEW  (finished)
         QUEUED -> RUNNING -> FAILED                                   (start failure)
         WAITING_FOR_AGENT -> FAILED                                   (provider error/timeout)
Agent:   AVAILABLE -> BUSY (claim) -> AVAILABLE (release on terminal state)
```

Both the task transition and the agent claim are DB-safe compare-and-swap
operations (`UPDATE ... WHERE status = <expected>`), so concurrent `execute`
calls can never start the same task twice nor put one agent on two
executions.

### 11.6 Execution API (`orchestrator/app/api.py`)

| Endpoint                             | Method | Success | Errors                            |
|--------------------------------------|--------|---------|-----------------------------------|
| `/tasks/{task_id}/execute`           | POST   | 202     | 404/409/502/504                   |
| `/tasks/{task_id}/execution`         | GET    | 200     | 404/409                           |
| `/tasks/{task_id}/execution/refresh` | POST   | 200     | 404/409/502/504                   |

Error mapping: task/agent/project missing → 404; provider not configured,
agent not usable, task not `QUEUED`, workspace not a git clone, in-flight
cancel attempt → 409; provider rejection → 502; provider timeouts → 504.

`Task` gained `execution_reference` (opaque provider conversation id) and
`execution_status` (last-known provider state). Both are server-controlled.

### 11.7 Configuration

`OpenHandsSettings` (in `config.py`) reads `OPENHANDS_BASE_URL`,
`OPENHANDS_API_KEY` (`repr=False` — never logged), `OPENHANDS_TIMEOUT`,
`OPENHANDS_START_TIMEOUT`, `OPENHANDS_POLL_INTERVAL`, and
`OPENHANDS_MAX_EXECUTION_SECONDS`. The API key stays in the environment (or
`.env`); it is never stored in the database, returned by an endpoint, or
written to agent `configuration`.

### 11.8 Testing strategy

Nothing in production is faked, so the tests are split into three layers:

1. **Wire-protocol tests** (`tests/test_openhands_adapter.py`) drive the real
   `OpenHandsAdapter` against `tests/fake_openhands.py`, an in-memory fake of
   the OpenHands Cloud API V1 over `httpx.MockTransport`. They verify the
   documented payload shape, auth headers, status translation, error mapping,
   and that success is never inferred from HTTP 200.
2. **Orchestration tests** (`tests/test_execution.py`) inject an in-memory
   fake adapter (`tests/fake_adapters.py`) into the `ExecutionService` (and
   via `app.dependency_overrides` into the API) and exercise the whole
   lifecycle: execute → refresh → completed, failure/timeout paths, agent
   claim/release, duplicate-execution rejection, and the three endpoints.
3. **Workspace tests** (`tests/test_workspace_repository.py`) build real tiny
   git repositories and verify origin parsing, branch validation, and
   adversarial origin rejection.

`tests/fake_openhands.py` and `tests/fake_adapters.py` live exclusively under
`tests/` and are never registered as production providers.

---

## PostgreSQL migration path

1. Add `psycopg` (or `asyncpg`) to `orchestrator/requirements.txt`.
2. Set `DATABASE_URL` to
   `postgresql+psycopg://user:password@host:5432/orchestrator`.
3. Remove the SQLite-specific `check_same_thread` connect_args (the engine
   creation logic already handles this via URL prefix detection).
4. No code changes are needed — SQLAlchemy handles dialect differences. The
   Task engine's CAS UPDATE is dialect-neutral.

---

## Known limitations

- Docker is not available on the current development machine, so
  containerised verification was not performed; the Dockerfile layout was
  validated by running the app from an identical local layout with `uvicorn`.
- The database layer is synchronous; an async engine (e.g. `aiosqlite`) can
  be added later if needed for high-concurrency scenarios.
- No migration tool (Alembic) is configured yet — tables are created via
  `Base.metadata.create_all()`. Alembic should be introduced when models are
  added.
- `WAITING_FOR_APPROVAL` is reachable via the single added edge
  `WAITING_FOR_REVIEW -> WAITING_FOR_APPROVAL` (see section 8.2).
- Self-parent and cycle relationships cannot be formed through the Phase 3
  API (no task-update endpoint); `TaskService` still validates and rejects
  them, and tests cover the guards directly.
- **Real OpenHands end-to-end execution is blocked in this environment**: no
  `OPENHANDS_API_KEY` is available and no OpenHands runtime (Docker or CLI) is
  installed. The complete request→provider→result path is exercised offline
  against a fake OpenHands Cloud API server implementing the real V1 wire
  protocol and an in-memory fake adapter (section 11.8). Without an API key
  the adapter truthfully reports `not_configured` and `execute` returns `409`.
- **Cancellation of in-flight executions is unsupported**: OpenHands Cloud API
  V1 documents no cancellation endpoint; `cancel` on an executing task returns
  `409` and the adapter raises `AgentCancellationError`.
- Only OpenHands has a real adapter; `claude_code`, `codex`, and `gemini`
  remain honest boundaries reporting `not_configured`.
- The Task engine exposes only creation, listing, reading, cancellation, and
  the three execution endpoints over HTTP; the remaining state machine edges
  are exercised internally by the service layer and covered by tests.
- No background worker exists yet: `refresh` polls the provider on demand, so
  a task stays `WAITING_FOR_AGENT` until a client calls refresh or the
  orchestrator-side patience limit is exceeded on the next refresh. A
  supervisor/autonomous loop is explicitly out of scope for Phase 5.
- Agents are global infrastructure; execution derives the target repository
  from the task's own project workspace git `origin` (server-side), enforcing
  per-task workspace isolation (section 11.3).
- Workspace path checks have a TOCTOU gap (symlink swaps between resolution
  and use). Documented in detail in section 7; acceptable for single-user
  operation and mitigated by post-mkdir re-verification. Revisit before
  adding any endpoint that writes caller-supplied paths.
