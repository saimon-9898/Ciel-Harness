# AI CTO Hub - orchestrator service image.
# Phase 1: minimal backend foundation. No agent execution is bundled here.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app ./app

# Run as an unprivileged user. The host ./data and ./projects directories are
# bind-mounted to /app/data and /app/projects by docker-compose so the
# database file and project workspaces persist on the host.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data /app/logs /app/projects \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
