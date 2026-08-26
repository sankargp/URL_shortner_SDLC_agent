# Demo Requirements and Agent Generalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace duplicate expiry requirements with password-protection and idempotent-batch demos whose SDLC runs generate requirement-specific designs, materialize working code, and report real pytest results.

**Architecture:** A deterministic profile registry selects supported offline demos and supplies design metadata plus acceptance-test selectors. A cumulative known-good FastAPI template implements every supported demo, while live mode may replace it only after syntax validation. A transactional catalog-refresh service backs up Governance SQLite, removes only verified duplicates, and inserts fresh draft requirements.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, SQLAlchemy 2, SQLite, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-demo-requirements-and-agent-generalization-design.md`

## Global Constraints

- Preserve canonical `REQ-002` and every historical directory under `workspace/runs/`.
- Delete only verified duplicate rows `REQ-004` and `REQ-005` after an online SQLite backup.
- Keep SQLite persistence and add no password or idempotency service dependency.
- Use `hashlib.pbkdf2_hmac`, cryptographically random salts, and `hmac.compare_digest`; never persist plaintext passwords.
- Mock/replay runs must be deterministic and must safe-stop unsupported requirements instead of claiming success.
- Test execution must use a temporary SQLite database and must not mutate `target-app/urls.db`.
- Preserve all unrelated dirty-working-tree changes; stage and commit only task-owned paths.

---

## File Map

- Create `agents/demo_profiles.py`: profile definitions, deterministic matching, architecture metadata, capability names, and pytest node IDs.
- Create `agents/templates/target_app_main.py`: cumulative known-good target application source for deterministic implementation runs.
- Modify `agents/planner.py`: select a profile once and persist the result in run context.
- Modify `agents/requirements.py`: safe-stop unsupported or ambiguous profile matches in mock/replay mode.
- Modify `agents/architect.py`: emit selected-profile impact analysis and tags.
- Modify `agents/implementer.py`: atomically materialize validated deterministic/live source and write JSON provenance.
- Modify `agents/tester.py`: run selected pytest node IDs and derive pass/fail from JUnit XML.
- Modify `target-app/main.py`: configurable database path, clean-schema code allocation, passwords, batch processing, and idempotency.
- Replace `target-app/tests/test_shortener.py`: isolated acceptance tests with stable node IDs used by profiles.
- Create `orchestrator/demo_catalog.py`: backup, duplicate validation, transactional delete/insert, and result model.
- Modify `orchestrator/cli.py`: guarded `refresh-demo-requirements` command.
- Create `orchestrator/tests/test_demo_catalog.py`: catalog backup/rollback/insert tests.
- Create `orchestrator/tests/test_demo_profiles.py`: profile and profile-aware agent tests.
- Create `orchestrator/tests/test_agent_execution.py`: implementer atomicity and real tester-result tests.
- Modify `pyproject.toml`: register target-app profile markers.
- Modify `README.md`: document refreshed demos and catalog-refresh command.

---

### Task 1: Deterministic Demo Profile Registry

**Files:**
- Create: `agents/demo_profiles.py`
- Create: `orchestrator/tests/test_demo_profiles.py`

**Interfaces:**
- Produces: `DemoProfile`, `ProfileResolution`, `resolve_demo_profile(requirement, mode=None)`, and `get_demo_profile(name)`.
- Consumers: planner, requirements, architect, implementer, and tester agents.

- [ ] **Step 1: Write failing profile-resolution tests**

Create tests that build requirements titled `Add custom aliases and link expiry`, `Password-protected short links`, and `Bulk URL shortening with idempotent retries`; assert profile names `alias_expiry`, `password_protection`, and `bulk_idempotency`. Assert `greenfield` resolves to `core_shortener`, an `ambiguous` requirement resolves to `ambiguous_reliability`, and unknown/multiply matched brownfield requirements return a resolution error in mock mode.

```python
def test_password_requirement_selects_password_profile():
    result = resolve_demo_profile(
        {"type": "brownfield", "title": "Password-protected short links"},
        mode="mock",
    )
    assert result.profile.name == "password_protection"
    assert result.error is None


def test_unknown_mock_requirement_is_unsupported():
    result = resolve_demo_profile(
        {"type": "brownfield", "title": "Add social sharing"},
        mode="mock",
    )
    assert result.profile is None
    assert result.error == "unsupported_demo_profile"
```

- [ ] **Step 2: Run the new tests and verify the missing-module failure**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_demo_profiles.py -q`

Expected: collection fails because `agents.demo_profiles` does not exist.

- [ ] **Step 3: Implement immutable profiles and exact matching**

Use frozen dataclasses. Match on normalized title/intent signals, not numeric IDs. Title predicates must be mutually exclusive: alias requires both `alias` and `expir`; password requires `password` or `protected`; bulk requires (`bulk` or `batch`) and `idempoten`. Explicit `ambiguous` type and `greenfield` type match their own profiles.

```python
@dataclass(frozen=True)
class DemoProfile:
    name: str
    architecture: dict[str, object]
    tags: tuple[str, ...]
    capabilities: tuple[str, ...]
    test_node_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProfileResolution:
    profile: DemoProfile | None
    error: str | None = None
```

Each profile includes exact target-app test node IDs. The password and bulk profiles include core regression node IDs in addition to their own acceptance tests.

- [ ] **Step 4: Run profile tests**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_demo_profiles.py -q`

Expected: all profile tests pass.

- [ ] **Step 5: Commit the profile registry**

```bash
git add agents/demo_profiles.py orchestrator/tests/test_demo_profiles.py
git commit -m "feat: add deterministic SDLC demo profiles"
```

---

### Task 2: Profile-Aware Planning, Requirements, and Architecture

**Files:**
- Modify: `agents/planner.py`
- Modify: `agents/requirements.py`
- Modify: `agents/architect.py`
- Modify: `orchestrator/tests/test_demo_profiles.py`
- Modify: `orchestrator/tests/test_state_machine.py`

**Interfaces:**
- Consumes: `resolve_demo_profile()` and `DemoProfile.architecture/tags` from Task 1.
- Produces: run-context keys `demo_profile` and `demo_profile_error`; agent outputs with truthful `exit_ok`, `reason`, and profile-specific design.

- [ ] **Step 1: Add failing profile-aware planner and agent tests**

Assert the planner writes `demo_profile="password_protection"` to `RunStore`, the architect returns password columns and security tags, and an unknown mock requirement makes the requirements node return `exit_ok=False` with `reason="unsupported_demo_profile"`. Update generic greenfield fixtures to include enough requirement data to select `core_shortener`.

```python
Planner().plan(run, requirement, store)
assert store.read_context()["demo_profile"] == "password_protection"

result = ArchitectAgent().run(
    node=node,
    run=run,
    context={"demo_profile": "password_protection"},
    store=store,
)
assert "password_hash" in str(result["context_updates"]["design"])
assert "security_sensitive" in result["tags"]
```

- [ ] **Step 2: Run focused tests and verify failures**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_demo_profiles.py orchestrator/tests/test_state_machine.py -q`

Expected: failures show missing context keys and hardcoded alias/expiry architecture.

- [ ] **Step 3: Select and persist the profile in the planner**

Call `resolve_demo_profile(requirement)` after requirements analysis. Write the selected name or error to `RunStore`. Retain the ambiguity gate. Set the architecture gate trigger from the selected profile tags (`schema_change` and `security_sensitive`) instead of assuming every brownfield requirement has the same change.

- [ ] **Step 4: Make requirements and architect agents profile-aware**

Requirements output must fail unsupported mock/replay profiles. Architect output must copy the selected profile's architecture and tags. Live unknown profiles may use the LLM path, but malformed live design output returns `exit_ok=False`; it cannot fall back to alias/expiry.

- [ ] **Step 5: Run focused tests**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_demo_profiles.py orchestrator/tests/test_state_machine.py orchestrator/tests/test_requirement_integration.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit profile-aware orchestration**

```bash
git add agents/planner.py agents/requirements.py agents/architect.py orchestrator/tests/test_demo_profiles.py orchestrator/tests/test_state_machine.py
git commit -m "feat: drive SDLC design from requirement profiles"
```

---

### Task 3: Isolated Target-App Database and Core Regression Fix

**Files:**
- Modify: `target-app/main.py`
- Replace: `target-app/tests/test_shortener.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `URL_SHORTENER_DATABASE_PATH` override, `create_link(db, request)`, and a clean-database-safe generated-code path.
- Consumers: password and bulk tasks, deterministic template, and tester subprocess.

- [ ] **Step 1: Replace shared-database tests with an isolated module fixture**

Load `target-app/main.py` under a unique module name after setting `URL_SHORTENER_DATABASE_PATH` to `tmp_path / "urls.db"`; register the module in `sys.modules` before `exec_module`. Dispose the SQLAlchemy engine after each test. Mark core tests `@pytest.mark.profile_core`.

```python
@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("URL_SHORTENER_DATABASE_PATH", str(tmp_path / "urls.db"))
    name = f"target_app_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    module.engine.dispose()
    sys.modules.pop(name, None)
```

Add `test_clean_database_shorten_and_redirect`, `test_stats_increment`, and `test_invalid_url_rejected` with stable function names.

- [ ] **Step 2: Run the clean-database regression test**

Run: `.venv/Scripts/python.exe -m pytest target-app/tests/test_shortener.py::test_clean_database_shorten_and_redirect -q`

Expected: fail with `NOT NULL constraint failed: links.code`.

- [ ] **Step 3: Make the database path configurable and fix code allocation**

Read the database path from `URL_SHORTENER_DATABASE_PATH`, defaulting to `target-app/urls.db`. Allocate a provisional collision-resistant code before the first flush, then replace it with the base62 ID code after flush inside the same transaction. Check collisions against both aliases and codes and preserve existing codes.

- [ ] **Step 4: Register pytest markers and run core tests**

Add `profile_core`, `profile_alias_expiry`, `profile_password`, and `profile_bulk` markers under `[tool.pytest.ini_options]`.

Run: `.venv/Scripts/python.exe -m pytest target-app/tests/test_shortener.py -m profile_core -q`

Expected: core tests pass and no repository database timestamp changes.

- [ ] **Step 5: Commit the isolated core fix**

```bash
git add target-app/main.py target-app/tests/test_shortener.py pyproject.toml
git commit -m "fix: isolate shortener tests and support clean databases"
```

---

### Task 4: Password-Protected Links

**Files:**
- Modify: `target-app/main.py`
- Modify: `target-app/tests/test_shortener.py`

**Interfaces:**
- Produces: optional `ShortenRequest.password`, nullable `password_salt/password_hash`, `_hash_password(password)`, `_password_matches(link, supplied)`, and `X-Link-Password` redirect handling.
- Consumers: batch item creation and password profile acceptance tests.

- [ ] **Step 1: Write failing password acceptance tests**

Add `profile_password` tests for unprotected compatibility, missing/incorrect header `401`, correct header `307`, no plaintext persistence, no click increment on failed authorization, and expired protected links returning `410` before password validation.

```python
response = client.post(
    "/shorten",
    json={"url": "https://example.com/private", "password": "s3cret"},
)
code = response.json()["code"]
assert client.get(f"/{code}", follow_redirects=False).status_code == 401
assert client.get(
    f"/{code}",
    headers={"X-Link-Password": "s3cret"},
    follow_redirects=False,
).status_code == 307
```

- [ ] **Step 2: Run password tests and verify failure**

Run: `.venv/Scripts/python.exe -m pytest target-app/tests/test_shortener.py -m profile_password -q`

Expected: request/model and authorization assertions fail because passwords are unsupported.

- [ ] **Step 3: Implement password schema, migration, hashing, and redirect checks**

Use 16-byte `secrets.token_bytes` salts, PBKDF2-HMAC-SHA256 with 310,000 iterations, hexadecimal persistence, and `hmac.compare_digest`. Add SQLite columns with startup migrations. Never include password fields in stats or responses. Evaluate missing link, expiry, then password; increment clicks only after all checks pass.

- [ ] **Step 4: Run password and regression tests**

Run: `.venv/Scripts/python.exe -m pytest target-app/tests/test_shortener.py -m "profile_password or profile_core or profile_alias_expiry" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit password protection**

```bash
git add target-app/main.py target-app/tests/test_shortener.py
git commit -m "feat: add password-protected short links"
```

---

### Task 5: Idempotent Batch Shortening

**Files:**
- Modify: `target-app/main.py`
- Modify: `target-app/tests/test_shortener.py`

**Interfaces:**
- Produces: `POST /shorten/batch`, `BatchShortenRequest`, `BatchItemResult`, `IdempotencyRequest`, canonical payload digest, and stable stored response replay.
- Consumes: the single-item creation service and password fields from Tasks 3 and 4.

- [ ] **Step 1: Write failing batch acceptance tests**

Add `profile_bulk` tests for missing key, 0/101 bounds, ordered all-success response, mixed success/conflict response with `207`, same-key/same-body replay without additional links, same-key/different-body `409`, password field support, and replay after disposing/reloading the app module against the same temporary database.

```python
first = client.post(
    "/shorten/batch",
    headers={"Idempotency-Key": "retry-123"},
    json={"items": [{"url": "https://one.example"}, {"url": "https://two.example"}]},
)
second = client.post(
    "/shorten/batch",
    headers={"Idempotency-Key": "retry-123"},
    json={"items": [{"url": "https://one.example"}, {"url": "https://two.example"}]},
)
assert second.json() == first.json()
assert link_count(app_module) == 2
```

- [ ] **Step 2: Run batch tests and verify 404/failures**

Run: `.venv/Scripts/python.exe -m pytest target-app/tests/test_shortener.py -m profile_bulk -q`

Expected: fail because `/shorten/batch` and idempotency persistence do not exist.

- [ ] **Step 3: Implement batch schemas and idempotency table**

Canonicalize the request with sorted-key compact JSON after Pydantic validation. Hash the idempotency key and payload with SHA-256. Persist key digest, request digest, response JSON, status, and timestamp. Process items in input order using nested transactions so an item failure rolls back only that item. Return `200` for all success, `207` for mixed results, and `409` for key/payload mismatch.

- [ ] **Step 4: Run batch and full target-app tests**

Run: `.venv/Scripts/python.exe -m pytest target-app/tests/test_shortener.py -q`

Expected: all core, alias/expiry, password, and batch tests pass.

- [ ] **Step 5: Commit batch shortening**

```bash
git add target-app/main.py target-app/tests/test_shortener.py
git commit -m "feat: add idempotent batch shortening"
```

---

### Task 6: Truthful Implementer and Real Tester

**Files:**
- Create: `agents/templates/target_app_main.py`
- Modify: `agents/implementer.py`
- Modify: `agents/tester.py`
- Create: `orchestrator/tests/test_agent_execution.py`

**Interfaces:**
- Consumes: profile capability/test metadata and completed `target-app/main.py`.
- Produces: validated atomic code materialization, implementation-provenance JSON, JUnit-derived testing JSON, and real `exit_ok` values.

- [ ] **Step 1: Write failing implementer/tester behavior tests**

Assert deterministic implementation writes the known-good template and JSON provenance to temporary paths; invalid live code leaves the destination unchanged and returns failure; tester invokes selected node IDs, passes with a real green pytest subprocess, and fails when a selected test fails. Inject app/template paths and a subprocess runner into agent constructors or helper functions so tests do not modify repository files.

```python
result = materialize_validated_source("def broken(:\n", destination)
assert result.ok is False
assert destination.read_text() == "original"

report = run_pytest_nodes((passing_node_id,), env=test_env)
assert report.return_code == 0
assert report.failed == 0
assert report.passed == 1
```

- [ ] **Step 2: Run execution-agent tests and verify failures**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_agent_execution.py -q`

Expected: imports or assertions fail because atomic materialization and real pytest execution do not exist.

- [ ] **Step 3: Add cumulative template and atomic implementer**

Copy the now-tested `target-app/main.py` into the template as the deterministic source of truth. Validate source with `compile()`, write a sibling temporary file, flush it, then `os.replace()` the destination. In mock/replay mode use the template; in live mode use extracted model output. Invalid/unusable live output returns `exit_ok=False` and does not overwrite. Write valid JSON provenance with profile, mode, capabilities, file path, and SHA-256 digest.

- [ ] **Step 4: Replace fabricated tester counts with subprocess/JUnit results**

Run `sys.executable -m pytest` against exact profile node IDs with `--junitxml` in a temporary directory and `URL_SHORTENER_DATABASE_PATH` pointing there. Parse JUnit XML for totals. Store command arguments without secrets, return code, counts, acceptance mapping, and truncated stdout/stderr. Set `exit_ok` only when return code is zero and every configured node ID ran successfully.

- [ ] **Step 5: Run execution-agent and target tests**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_agent_execution.py target-app/tests/test_shortener.py -q`

Expected: all tests pass and `target-app/urls.db` is unchanged.

- [ ] **Step 6: Commit truthful implementation and testing agents**

```bash
git add agents/templates/target_app_main.py agents/implementer.py agents/tester.py orchestrator/tests/test_agent_execution.py
git commit -m "feat: materialize demo code and report real tests"
```

---

### Task 7: Transactional Governance Catalog Refresh

**Files:**
- Create: `orchestrator/demo_catalog.py`
- Create: `orchestrator/tests/test_demo_catalog.py`
- Modify: `orchestrator/cli.py`

**Interfaces:**
- Produces: `RefreshResult`, `refresh_demo_requirements(workspace_dir, now=None)`, and CLI command `orchestrator refresh-demo-requirements --yes`.
- Consumes: existing `requirements` schema and the approved replacement requirement definitions.

- [ ] **Step 1: Write failing cleanup, backup, and rollback tests**

Build a temporary repository seeded with REQ-001 through REQ-003, insert duplicate rows 4 and 5, and call refresh. Assert IDs 1-3 are byte-for-byte equivalent before/after, IDs 4/5 are absent, new IDs exceed 5, lifecycle states are draft/not-started/not-requested, and the returned backup opens with rows 4/5. In a second test alter title 5 and assert refresh raises `DuplicateValidationError` without deleting or inserting rows.

```python
result = refresh_demo_requirements(workspace)
assert result.removed_ids == (4, 5)
assert len(result.created_ids) == 2
assert result.backup_path.exists()
assert repository.get("REQ-002").title == "Add custom aliases and link expiry"
```

- [ ] **Step 2: Run catalog tests and verify missing-module failure**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_demo_catalog.py -q`

Expected: collection fails because `orchestrator.demo_catalog` does not exist.

- [ ] **Step 3: Implement online backup and immediate transaction**

Use `sqlite3.Connection.backup()` into `workspace/backups/governance-before-demo-refresh-<UTC timestamp>.db`. Open the live database with a 30-second timeout, re-read and normalize rows 4/5, verify canonical row 2, then `BEGIN IMMEDIATE`. Delete only 4/5 and insert both approved records with JSON arrays, UTC timestamps, revision 0, and null analysis/run fields. Verify IDs 1-3 and title uniqueness before commit; roll back on any exception.

- [ ] **Step 4: Add guarded CLI command and tests**

The command refuses mutation unless `--yes` is present. On success print removed IDs, created IDs/titles, and backup path. Add a `CliRunner` test that omission of `--yes` exits nonzero and leaves the database unchanged.

- [ ] **Step 5: Run catalog and requirements tests**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_demo_catalog.py orchestrator/tests/test_requirements_store.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the reusable catalog refresh**

```bash
git add orchestrator/demo_catalog.py orchestrator/tests/test_demo_catalog.py orchestrator/cli.py
git commit -m "feat: refresh duplicate demo requirements safely"
```

---

### Task 8: End-to-End Workflow Verification and Live Database Refresh

**Files:**
- Modify: `orchestrator/tests/test_requirement_integration.py`
- Modify: `README.md`
- Runtime mutation: `workspace/governance.db`
- Runtime creation: `workspace/backups/governance-before-demo-refresh-*.db`

**Interfaces:**
- Consumes: every preceding task.
- Produces: end-to-end evidence and the requested active Governance backlog.

- [ ] **Step 1: Write profile-specific end-to-end workflow tests**

Parameterize password and bulk requirements. In mock mode, prepare each run, approve architecture, run through implementation/testing/docs, and assert it reaches release approval with profile-specific architecture, implementation provenance, and real test report. Assert no alias/expiry architecture fields appear in password or bulk runs.

- [ ] **Step 2: Run end-to-end tests and fix only task-related failures**

Run: `.venv/Scripts/python.exe -m pytest orchestrator/tests/test_requirement_integration.py -q`

Expected: all integration tests pass.

- [ ] **Step 3: Update documentation**

Document the two new demos, deterministic/live profile behavior, truthful test artifacts, and the exact refresh command. State that the command creates a backup and preserves run history.

- [ ] **Step 4: Run static and full verification**

Run: `.venv/Scripts/python.exe -m ruff check agents orchestrator target-app ui`

Run: `.venv/Scripts/python.exe -m pytest -q`

Expected: both commands exit zero.

- [ ] **Step 5: Re-verify exact live database targets before mutation**

Read `workspace/governance.db` in SQLite read-only mode and assert:

- REQ-002 title is exactly `Add custom aliases and link expiry`;
- REQ-004 and REQ-005 titles normalize to the expiry Live Demo duplicates;
- no other duplicate expiry records exist.

Abort instead of broadening deletion if these assertions fail.

- [ ] **Step 6: Refresh the live Governance catalog**

Run: `.venv/Scripts/python.exe -m orchestrator.cli refresh-demo-requirements --yes`

Expected: output lists removed IDs 4 and 5, two newly assigned IDs, and a timestamped backup path.

- [ ] **Step 7: Verify the live database and backup**

Read both databases in SQLite read-only mode. Confirm canonical REQ-002 remains unchanged, no duplicate expiry title remains, both replacement drafts exist with correct acceptance criteria, and the backup contains the pre-refresh rows.

- [ ] **Step 8: Commit documentation and integration tests**

Do not add the runtime database, WAL files, backup, generated run artifacts, or unrelated existing changes.

```bash
git add README.md orchestrator/tests/test_requirement_integration.py
git commit -m "docs: add truthful password and batch demos"
```

- [ ] **Step 9: Record final repository state**

Run: `git status --short`

Report task-owned commits, verification commands/results, removed and created requirement IDs, backup path, and all preserved unrelated changes.
