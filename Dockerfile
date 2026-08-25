FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# System deps kept minimal; SQLite ships with Python.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY orchestrator ./orchestrator
COPY agents ./agents
COPY ui ./ui
COPY policies ./policies
COPY target-app ./target-app
COPY workspace ./workspace

RUN pip install --upgrade pip && pip install -e .

# Default to offline-safe mode inside the container.
ENV LLM_MODE=mock

# Reasonable default; compose overrides per-service.
CMD ["orchestrator", "demo", "--scenarios", "greenfield,brownfield,ambiguous"]
