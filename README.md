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
make dashboard             # manage requirements at http://localhost:8000
make demo                  # explicit forceful demo: greenfield -> brownfield -> ambiguous
```

Then serve the built shortener:

```bash
make serve-app             # http://localhost:8080  (POST /shorten, GET /{code})
```

**No API key? No network?** Leave `LLM_MODE=mock` in `.env`. CLI-driven runs and
the demo remain deterministic and offline. The dashboard's **Implement** action
is the publishing path and therefore requires GitHub access and `GITHUB_TOKEN`.

> If your environment doesn't expose the console scripts on `PATH`, every command
> also works as a module: `python -m orchestrator.cli demo`,
> `python -m uvicorn target-app.main:app --port 8080`, `python -m ui.app`.

---

## What runs where

| Command | What it does | Port |
|---------|--------------|------|
| `make demo` | Orchestrator drives the 3 scenarios, pausing at human gates | — |
| `make dashboard` | Requirements backlog + analysis + approvals + metrics | 8000 |
| `make serve-app` | The URL shortener the agents produced | 8080 |
| `make test` | Orchestrator + Governance UI + target-app tests | — |

---

## Architecture (at a glance)

```
Requirement (workspace/governance.db)
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
| **Requirements store** | `orchestrator/requirements_store.py` | Canonical SQLite backlog, analysis, and lifecycle/execution statuses |
| **Gates** | `orchestrator/gates.py` | Entry/exit + human-approval checkpoints |
| **Metrics** | `orchestrator/metrics.py` | Success rate, retry/rollback freq, MTTR, e2e latency |
| **Planner** | `agents/planner.py` | Requirement → DAG (scenario-aware) |
| **Agents** | `agents/*.py` | requirements, architect, implementer, tester, docs, release |
| **Policies** | `policies/guardrails.yaml` | Security/compliance/change-control + autonomy boundary |
| **Dashboard** | `ui/app.py` | Requirement creation/lifecycle/analysis + approvals + metrics + audit |
| **Target app** | `target-app/main.py` | The URL shortener (FastAPI + SQLite) |

---

## The three scenarios

Each shows **decomposition → orchestration → validation**.

| Scenario | Requirement | What it demonstrates |
|----------|-------------|----------------------|
| **Greenfield** | `REQ-001` build core shorten/redirect/stats | Full pipeline from scratch; parallel test‖docs; release sign-off gate |
| **Brownfield** | `REQ-002` add custom alias + expiry | **Codebase reasoning**: impacted modules, schema change → approval gate, regression focus |
| **Ambiguous** | `REQ-003` "make it more reliable" | **Ambiguity detection**: engine surfaces interpretations and blocks at a human gate before design |

### Refreshable demo backlog

The Governance backlog can replace duplicate expiry-demo records with two
distinct, end-to-end brownfield demonstrations:

- **Password-protected short links** — PBKDF2 password hashing, authorization
  failures, schema/security approval, and backward-compatible public links.
- **Bulk shortening with idempotent retries** — bounded batches, ordered partial
  results, persisted response replay, and changed-payload conflict detection.

The refresh is deliberately guarded and creates an online SQLite backup before
deleting only verified `REQ-004`/`REQ-005` duplicates:

```bash
orchestrator refresh-demo-requirements --yes
```

Historical `workspace/runs/` directories are preserved. New requirements begin
as drafts so a demo can show the full ready → analyze → implement lifecycle.

Run one at a time:

```bash
orchestrator run --req REQ-002
```

The YAML files in `workspace/requirements/` are migration fixtures only. On the
first repository initialization, missing seed IDs are inserted into
`workspace/governance.db` without overwriting existing rows. All subsequent
Governance reads and execution status updates use the database exclusively.

New requirements can be added in the dashboard. They begin as
`draft/not_started`; marking one ready does not start it. Use the explicit
Analyze/Retry action to persist structured LLM analysis. Once analysis succeeds,
use the requirement's Implement action to launch the governed workflow. The
`orchestrator run --req <id>` command remains available for CLI-driven execution.

### Implement to pull request

The dashboard's **Implement** action discovers the current Git repository, uses
`origin` (or the only configured remote), and takes the current branch as its
base. It immediately marks the requirement in progress, then runs the governed
workflow in the background against an isolated clone under
`workspace/runs/<run-id>/repository`. Uncommitted source-checkout changes are
never copied, staged, or modified.

Configure a repository-scoped fine-grained GitHub token with **Contents: write**
and **Pull requests: write**:

```bash
GITHUB_TOKEN=github_pat_...
# Optional overrides:
SOURCE_REPO_PATH=.
GIT_REMOTE_NAME=
GIT_BASE_BRANCH=
IMPLEMENT_WORKERS=2
```

The source base commit must match the selected remote branch. After tests and
documentation complete, the workflow commits to
`agentic/<requirement-id>-<run-id>`, pushes the branch, opens a normal PR, and
then pauses at release sign-off. A no-diff implementation is recorded as
"No changes required" without creating an empty PR. Rejected sign-off leaves
the branch and PR open for investigation.

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
orchestrator replan --run <id> --req REQ-001
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
- **Governance UI** — backlog ordering, creation, lifecycle guards, analysis,
  escaping, and exact-run approval routing (`ui/tests/`).
- **Acceptance** — modeled as node **exit gates**; a failing gate drives the
  bounded-retry path.

Known demo requirements resolve to deterministic profiles in mock/replay mode.
Those profiles drive requirement-specific architecture, a validated cumulative
target-app template, and exact pytest node IDs. The tester records real pytest
and JUnit results; unsupported offline requirements safe-stop instead of
reporting fabricated success. Live mode may generate source, but syntax-invalid
output is rejected before atomic replacement of the existing application.

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
- **File-based run history:** requirement records and statuses are in SQLite,
  while detailed node state, artifacts, approvals, lineage, and audit logs remain
  under `workspace/runs/`.

---

## Repository layout

```
agentic-sdlc/
├── orchestrator/   # DAG kernel, state machine, gates, metrics, CLI
├── agents/         # planner + specialist agents (bounded roles)
├── policies/       # guardrails + autonomy boundary
├── ui/             # approval + metrics dashboard
├── target-app/     # the URL shortener (FastAPI + SQLite)
├── workspace/      # governance.db + seed fixtures + per-run state/artifacts/audit/metrics
├── pyproject.toml  # deps + console entrypoints (orchestrator, dashboard)
├── Makefile        # setup / run / demo / dashboard / test
└── docker-compose.yml
```

---

## Setup (Docker, clean-room path)

Guarantees the prototype runs regardless of host machine/Python version — no
local venv needed. Native `make setup && make demo` remains the primary path;
Docker is insurance. Normal startup launches only the dashboard and target app;
orchestration remains a deliberate, manually invoked action.

```bash
docker compose up      # dashboard(:8000) + target-app(:8080)
```

One image (`agentic-sdlc:latest`) is built and run as three services:

| Service | Command it runs | Port | Notes |
|---------|------------------|------|-------|
| `orchestrator` | Manual profile; explicit command only | — | Reads requirements from the shared Governance database |
| `dashboard` | `dashboard` | `8000` | Backlog, asynchronous implementation-to-PR launch, approvals, metrics, and audit |
| `target-app` | `uvicorn target-app.main:app --host 0.0.0.0 --port 8080` | `8080` | The URL shortener the agents produced |

Useful variants:

```bash
# Run a ready requirement by database ID
docker compose run --rm orchestrator orchestrator run --req REQ-002

# Retain the three-scenario demo as an explicit forceful operation
docker compose run --rm orchestrator orchestrator demo --force

# Resume after approving a gate (workspace/runs/<id>/approvals/APR-*.json)
docker compose run --rm orchestrator orchestrator resume --run <id>

# Rebuild after changing orchestrator/agents/ui code (baked into the image)
docker compose build

# Tear down (the named workspace volume keeps governance.db and run data)
docker compose down

# Destructive: also deletes governance.db and every persisted requirement/status
docker compose down -v
```

**Live LLM mode in Docker:** set `LLM_MODE=live` and `LLM_API_KEY=...` in `.env`
(`env_file: .env` wires it into the `orchestrator` and `dashboard` services).
Set `GITHUB_TOKEN` in the same file to enable the dashboard's Implement action.
Compose mounts this checkout read-only at `/source-repo`; generated branches are
built in the persistent workspace clone and pushed through the configured remote.
`target-app` is bind-mounted (`./target-app:/app/target-app`), so code the
implementer regenerates lands on the host too — restart the `target-app`
service (`docker compose restart target-app`) to pick it up. `workspace` is a
named volume shared by all services, so `governance.db`, requirement statuses,
runs, artifacts, approvals, and audit logs persist across container stops,
restarts, and ordinary `docker compose down`. Deleting the volume with
`docker compose down -v` deletes that data.
