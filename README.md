# Agentic SDLC System — URL Shortener

A working prototype of an **agentic software-engineering system**: a stateful,
governed orchestration engine that turns a requirement into a reviewable
engineering outcome across the full SDLC (requirements → architecture →
implementation → testing → docs → release). The **target system** it builds and
enhances is a **URL shortener**.

> The orchestrator is the product; the URL shortener is the test case.

---

## Quick start (5 steps)

```bash
git clone <repo> && cd agentic-sdlc
make setup                 # venv + deps + .env  (needs Python 3.11+)
# edit .env to paste an API key, OR leave LLM_MODE=mock to run fully offline
make demo                  # runs greenfield -> brownfield -> ambiguous
make dashboard             # open http://localhost:8000 to approve gates + see metrics
```

Then serve the built shortener:

```bash
make serve-app             # http://localhost:8080  (POST /shorten, GET /{code})
```

**No API key? No network?** Leave `LLM_MODE=mock` in `.env`. The whole pipeline
runs deterministically offline so the demo never breaks.

> If your environment doesn't expose the console scripts on `PATH`, every command
> also works as a module: `python -m orchestrator.cli demo`,
> `python -m uvicorn target-app.main:app --port 8080`, `python -m ui.app`.

---

## What runs where

| Command | What it does | Port |
|---------|--------------|------|
| `make demo` | Orchestrator drives the 3 scenarios, pausing at human gates | — |
| `make dashboard` | Approvals + live reliability metrics + audit log | 8000 |
| `make serve-app` | The URL shortener the agents produced | 8080 |
| `make test` | Orchestrator + target-app tests | — |

---

## Architecture (at a glance)

```
Requirement (REQ-*.yaml)
        │
   ┌────▼─────┐   Planner agent emits an explicit dependency graph (DAG)
   │ Planner  │   + entry/exit gates + parallel groups + lineage
   └────┬─────┘
        │
┌───────▼─────────────────────────────────────────────────────────┐
│ Orchestration Kernel  (stateful DAG scheduler + governance)      │
│                                                                  │
│  req ──▶ arch ──▶ impl ──┬─▶ unit  ─┐                            │
│                          └─▶ docs  ─┴─▶ release (human sign-off) │
│                                                                  │
│  • entry/exit gates      • bounded retry → rollback → safe-stop  │
│  • parallel + sync join  • decision lineage + audit log          │
│  • human approval gates  • reliability metrics                   │
└───────┬───────────────────────────────┬─────────────────────────┘
        │ specialist agents             │ governance surface
        ▼                               ▼
 requirements / architect /        Dashboard (approvals + metrics)
 implementer / tester / docs /     Audit log + lineage + state.json
 release                           workspace/runs/<run-id>/
```

### Key components

| Component | File | Role |
|-----------|------|------|
| **State machine** | `orchestrator/state.py` | Node states + legal transitions (guards against silent linear chaining) |
| **Kernel** | `orchestrator/kernel.py` | Scheduler: parallel/serial execution, gates, retries, rollback, re-plan |
| **Run store** | `orchestrator/context.py` | Blackboard, decision lineage, append-only audit log, artifacts |
| **Gates** | `orchestrator/gates.py` | Entry/exit + human-approval checkpoints |
| **Metrics** | `orchestrator/metrics.py` | Success rate, retry/rollback freq, MTTR, e2e latency |
| **Planner** | `agents/planner.py` | Requirement → DAG (scenario-aware) |
| **Agents** | `agents/*.py` | requirements, architect, implementer, tester, docs, release |
| **Policies** | `policies/guardrails.yaml` | Security/compliance/change-control + autonomy boundary |
| **Dashboard** | `ui/app.py` | Approvals + live metrics + audit view |
| **Target app** | `target-app/main.py` | The URL shortener (FastAPI + SQLite) |

---

## The three scenarios

Each shows **decomposition → orchestration → validation**.

| Scenario | Requirement | What it demonstrates |
|----------|-------------|----------------------|
| **Greenfield** | `REQ-001` build core shorten/redirect/stats | Full pipeline from scratch; parallel test‖docs; release sign-off gate |
| **Brownfield** | `REQ-002` add custom alias + expiry | **Codebase reasoning**: impacted modules, schema change → approval gate, regression focus |
| **Ambiguous** | `REQ-003` "make it more reliable" | **Ambiguity detection**: engine surfaces interpretations and blocks at a human gate before design |

Run one at a time:

```bash
orchestrator run --req workspace/requirements/REQ-002-brownfield.yaml
```

---

## How governance works (the differentiator)

1. **Explicit DAG** with entry/exit gates per node — not linear chaining.
2. **Parallel + sync**: unit tests and docs run concurrently, then join at release.
3. **Human checkpoints** on high-impact actions (schema change, release, ambiguity).
   The run enters `AWAITING_APPROVAL`, persists an approval request, and **halts
   that path** until a decision is recorded (dashboard, file, or CLI).
4. **Bounded retries → rollback → safe-stop** on failure (`MAX_RETRIES` in `.env`).
5. **Decision lineage**: every artifact traces to a requirement + rationale
   (`workspace/runs/<id>/lineage.json`).
6. **Audit-grade observability**: every transition is appended to `audit.log`.
7. **Dynamic re-planning**: change a requirement, and the kernel invalidates
   affected downstream nodes and re-runs them under the same governance.

```bash
# Approve the blocking gate (file-based, auditable), then continue:
#   edit workspace/runs/<id>/approvals/APR-001.json -> "status": "approve"
orchestrator resume --run <id>

# Re-plan after a requirement change:
orchestrator replan --run <id> --req workspace/requirements/REQ-001-greenfield.yaml
```

---

## Autonomy boundary

Defined explicitly in `policies/guardrails.yaml`:

- **Unattended:** requirements normalization, greenfield design, code generation,
  test generation, documentation.
- **Requires a human:** schema/migration changes, merges, release, ambiguity
  resolution, any policy violation.

---

## Testing approach

- **Unit** — state-machine legality, gate routing, planner topology
  (`orchestrator/tests/`).
- **Integration** — target-app shorten/redirect/stats + validation
  (`target-app/tests/`).
- **Acceptance** — modeled as node **exit gates**; a failing gate drives the
  bounded-retry path.

```bash
make test
```

---

## Reliability & LLM modes

`LLM_MODE` isolates all model calls (see `.env`):

| Mode | Behavior |
|------|----------|
| `mock` | Canned, offline, deterministic (default — demo-safe) |
| `replay` | Reuse cached agent outputs from a prior run |
| `live` | Real provider calls (`anthropic`/`openai`); the implementer asks the model to rewrite `target-app/main.py` from the approved design, falling back to the known-good hardcoded implementation if the call errors or the response isn't valid code |

This keeps `make demo` runnable with **no key and no network**, and doubles as a
reproducibility feature.

`live` mode needs `LLM_API_KEY` set in `.env` (`LLM_PROVIDER`/`LLM_MODEL` pick the
provider + model). Other agents still call the LLM for rationale text but keep
deterministic, templated artifacts — only the implementer's generated code is
LLM-authored, and only when the call succeeds.

---

## Limitations & trade-offs

- **Custom engine over a framework (LangGraph, etc.):** chosen for explainability
  and defensibility of the control flow, at the cost of some built-in features.
- **Mock agents by default:** artifacts are representative, not LLM-authored,
  unless `LLM_MODE=live` — and even then only the implementer's
  `target-app/main.py` is LLM-authored; other agents' artifacts stay
  deterministic/templated so they remain reviewable. This is a deliberate
  demo-reliability trade-off.
- **In-memory rate limiting** in the target app is per-process (fine for a
  prototype; use Redis for multi-instance).
- **Re-planning** resets/re-runs affected nodes rather than diffing artifacts —
  correct and governed, but coarser than a production impact-diff.
- **Single-run dashboard:** shows the latest run; multi-run history is out of scope.

---

## Repository layout

```
agentic-sdlc/
├── orchestrator/   # DAG kernel, state machine, gates, metrics, CLI
├── agents/         # planner + specialist agents (bounded roles)
├── policies/       # guardrails + autonomy boundary
├── ui/             # approval + metrics dashboard
├── target-app/     # the URL shortener (FastAPI + SQLite)
├── workspace/      # requirements + per-run state/artifacts/audit/metrics
├── pyproject.toml  # deps + console entrypoints (orchestrator, dashboard)
├── Makefile        # setup / run / demo / dashboard / test
└── docker-compose.yml
```

---

## Setup (Docker, clean-room path)

Guarantees the prototype runs regardless of host machine/Python version — no
local venv needed. Native `make setup && make demo` remains the primary path;
Docker is insurance.

```bash
docker compose up      # orchestrator demo + dashboard(:8000) + target-app(:8080)
```

One image (`agentic-sdlc:latest`) is built and run as three services:

| Service | Command it runs | Port | Notes |
|---------|------------------|------|-------|
| `orchestrator` | `orchestrator demo --scenarios greenfield,brownfield,ambiguous` | — | Runs all 3 scenarios once, then exits |
| `dashboard` | `dashboard` | `8000` | Approvals + live metrics + audit view |
| `target-app` | `uvicorn target-app.main:app --host 0.0.0.0 --port 8080` | `8080` | The URL shortener the agents produced |

Useful variants:

```bash
# Run a single scenario instead of the default demo
docker compose run --rm orchestrator orchestrator run --req workspace/requirements/REQ-002-brownfield.yaml

# Resume after approving a gate (workspace/runs/<id>/approvals/APR-*.json)
docker compose run --rm orchestrator orchestrator resume --run <id>

# Just the dashboard + target-app, without re-running the demo
docker compose up dashboard target-app

# Rebuild after changing orchestrator/agents/ui code (baked into the image)
docker compose build

# Tear down (the workspace volume keeps runs/artifacts/audit log)
docker compose down
```

**Live LLM mode in Docker:** set `LLM_MODE=live` and `LLM_API_KEY=...` in `.env`
(`env_file: .env` wires it into the `orchestrator` and `dashboard` services).
`target-app` is bind-mounted (`./target-app:/app/target-app`), so code the
implementer regenerates lands on the host too — restart the `target-app`
service (`docker compose restart target-app`) to pick it up. `workspace` is a
named volume shared by all services, so runs, artifacts, approvals, and the
audit log persist across `docker compose up` runs.
