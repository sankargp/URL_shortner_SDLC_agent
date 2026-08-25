# Agentic SDLC — one-word commands.
# Default path needs only Python 3.11+. SQLite + local files => no services to start.

PY ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
BIN := $(VENV)/bin

.DEFAULT_GOAL := help

.PHONY: help setup run resume replan demo dashboard serve-app test lint clean

help:            ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:           ## Create venv, install package (editable), seed .env
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	@test -f .env || cp .env.example .env
	@echo "\n✅ Setup complete. Edit .env (or leave LLM_MODE=mock) then run: make demo\n"

run:             ## Run the orchestrator on the default greenfield requirement
	$(BIN)/orchestrator run --req workspace/requirements/REQ-001-greenfield.yaml

resume:          ## Resume the most recent run after an approval (RUN=<id> optional)
	$(BIN)/orchestrator resume $(if $(RUN),--run $(RUN),)

replan:          ## Re-plan a run after a requirement change (RUN=<id> REQ=<path>)
	$(BIN)/orchestrator replan --run $(RUN) --req $(REQ)

demo:            ## Full end-to-end: greenfield -> brownfield -> ambiguous
	$(BIN)/orchestrator demo --scenarios greenfield,brownfield,ambiguous

dashboard:       ## Start approval + metrics UI (http://localhost:8000)
	$(BIN)/dashboard

serve-app:       ## Serve the URL shortener the agents produced (http://localhost:8080)
	$(BIN)/uvicorn target-app.main:app --port 8080 --reload

test:            ## Run orchestrator + generated target-app tests
	$(BIN)/pytest

lint:            ## Lint with ruff
	$(BIN)/ruff check .

clean:           ## Remove venv and run artifacts (keeps requirement files)
	rm -rf $(VENV)
	rm -rf workspace/runs/*
	@echo "Cleaned venv and workspace/runs."
