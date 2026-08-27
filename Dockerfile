FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# System deps kept minimal; SQLite ships with Python.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY orchestrator ./orchestrator
COPY agents ./agents
COPY ui ./ui
COPY policies ./policies
COPY target-app ./target-app
COPY workspace/requirements ./workspace/requirements

RUN mkdir -p ./workspace/runs

# pytest is a runtime dependency here, not just a dev tool: the tester agent
# shells out to `python -m pytest` inside this container to verify acceptance criteria.
RUN pip install --upgrade pip && pip install -e ".[live]" && pip install "pytest>=8.0"

# Default to offline-safe mode inside the container.
ENV LLM_MODE=mock

# Safe default; orchestration is always an explicit command.
CMD ["orchestrator", "--help"]
