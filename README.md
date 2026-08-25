# AI CTO Hub — Phase 1: Foundation

A self-hosted AI coding-agent orchestration platform. This repository contains
the backend foundation that will later orchestrate OpenHands and other coding
agents.

**Phase 1 delivers:** a minimal FastAPI backend with structured logging,
database initialization, configuration, error handling, a liveness endpoint,
and Docker infrastructure. No task or agent execution is implemented yet.

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

> **Note:** The default `DATABASE_URL` (`sqlite:///./data/orchestrator.db`) is
> relative to the process working directory. Run from the repository root so
> `./data` resolves correctly. Alternatively, set an absolute path.

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
│       ├── config.py          # Pydantic-settings configuration
│       ├── db.py              # SQLAlchemy engine, session, helpers
│       ├── logging_config.py  # Structured JSON logging
│       └── models.py          # Placeholder for future ORM models
├── tests/
│   └── test_health.py         # Phase 1 test suite
├── data/                      # SQLite database (gitignored, bind-mounted)
└── logs/                      # Reserved for future use
```

---

## Security notes

- The container runs as an unprivileged user (`appuser`, uid 1000).
- Only the `./data` directory is bind-mounted into the container.
- No secrets, credentials, or API keys are hard-coded anywhere.
- The `.env` file is gitignored; only `.env.example` is tracked.

---

## Known limitations (Phase 1)

- Docker is not installed on the current development machine — the Dockerfile
  and docker-compose.yml are syntactically validated but not run.
- No task/agent execution, no dashboard, no autonomous behaviour.
- SQLite is the only database driver provided; PostgreSQL requires a driver
  (`psycopg`) in a later phase.
- OpenHands is not integrated; the architecture doc notes its REST API / SDK
  for future reference.