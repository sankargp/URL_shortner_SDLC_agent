"""Behavior tests for the database-backed Governance dashboard."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from orchestrator.context import RunStore
from orchestrator.requirements_store import (
    AuthoringStatus,
    ExecutionStatus,
    RequirementsRepository,
)
from orchestrator.state import NodeState
from ui import app as dashboard


def _configure_dashboard(
    tmp_path: Path, monkeypatch
) -> tuple[Path, RequirementsRepository, TestClient]:
    workspace = tmp_path / "workspace"
    (workspace / "runs").mkdir(parents=True)
    repository = RequirementsRepository(workspace)
    monkeypatch.setattr(dashboard, "WORKSPACE", str(workspace))
    monkeypatch.setattr(dashboard, "requirements_repository", lambda: repository)
    return workspace, repository, TestClient(dashboard.app)


def _create(
    repository: RequirementsRepository,
    title: str,
    *,
    requirement_type: str = "greenfield",
    intent: str = "Deliver the requested behavior",
):
    return repository.create(
        requirement_type=requirement_type,
        title=title,
        intent=intent,
    )


def _write_run(
    workspace: Path,
    *,
    run_id: str,
    requirement_id: str,
    node_state: str,
    approval_question: str | None = None,
) -> Path:
    root = workspace / "runs" / run_id
    (root / "approvals").mkdir(parents=True)
    (root / "artifacts").mkdir()
    state = {
        "id": run_id,
        "requirement_id": requirement_id,
        "scenario": "ambiguous",
        "created_at": 1,
        "nodes": {
            "req": {
                "id": "req",
                "stage": "requirements",
                "agent": "requirements",
                "title": "Interpret requirement",
                "depends_on": [],
                "parallel_group": None,
                "impact": "high",
                "attempts": 0,
                "outputs": {},
                "started_at": None,
                "ended_at": None,
                "state": node_state,
            }
        },
    }
    (root / "state.json").write_text(json.dumps(state))
    if approval_question is not None:
        approval = {
            "id": "APR-001",
            "node": "req",
            "stage": "requirements",
            "impact": "high",
            "question": approval_question,
            "options": ["approve", "reject", "modify"],
            "context": {},
            "status": "pending",
            "created_at": "2026-08-26T10:00:00",
            "decided_by": None,
            "decided_at": None,
        }
        (root / "approvals" / "APR-001.json").write_text(json.dumps(approval))
    return root


def test_home_lists_complete_backlog_in_operational_order(tmp_path, monkeypatch):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    awaiting = _create(repository, "Awaiting")
    repository.transition_authoring(awaiting.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(awaiting.requirement_id, "run-awaiting")
    repository.sync_execution(
        awaiting.requirement_id, "run-awaiting", ExecutionStatus.AWAITING_APPROVAL
    )
    progressing = _create(repository, "Progressing")
    repository.transition_authoring(progressing.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(progressing.requirement_id, "run-progress")
    ready = _create(repository, "Ready")
    repository.transition_authoring(ready.requirement_id, AuthoringStatus.READY)
    draft = _create(repository, "Draft")
    stopped = _create(repository, "Stopped")
    repository.transition_authoring(stopped.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(stopped.requirement_id, "run-stopped")
    repository.sync_execution(stopped.requirement_id, "run-stopped", ExecutionStatus.STOPPED)
    implemented = _create(repository, "Implemented")
    repository.transition_authoring(implemented.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(implemented.requirement_id, "run-implemented")
    repository.sync_execution(
        implemented.requirement_id, "run-implemented", ExecutionStatus.IMPLEMENTED
    )
    archived = _create(repository, "Archived")
    repository.transition_authoring(archived.requirement_id, AuthoringStatus.ARCHIVED)

    response = client.get("/")

    assert response.status_code == 200
    expected = [
        awaiting.requirement_id,
        progressing.requirement_id,
        ready.requirement_id,
        draft.requirement_id,
        stopped.requirement_id,
        implemented.requirement_id,
        archived.requirement_id,
    ]
    assert all(requirement_id in response.text for requirement_id in expected)
    positions = [response.text.index(requirement_id) for requirement_id in expected]
    assert positions == sorted(positions)
    assert "Add requirement" in response.text
    assert "Start" not in response.text


def test_home_renders_independent_collapsed_accordions(tmp_path, monkeypatch):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    record = _create(repository, "Expandable requirement")

    response = client.get("/")

    assert response.status_code == 200
    assert '<details class="create">' in response.text
    assert "<summary><h2>Add requirement</h2></summary>" in response.text
    card_tag = (
        f'<details class="card" id="requirement-{record.requirement_id}" '
        f'data-requirement-id="{record.requirement_id}">'
    )
    assert card_tag in response.text
    assert (
        f"<summary><h3>{record.requirement_id} · Expandable requirement</h3></summary>"
        in response.text
    )
    card_start = response.text.index(card_tag)
    card_end = response.text.index("</details>", card_start)
    assert response.text.index("<b>Identity:</b>", card_start, card_end) > response.text.index(
        "</summary>", card_start, card_end
    )
    assert '<details class="create" open>' not in response.text
    assert '<details class="card" open>' not in response.text
    assert "<title>Agentic SDLC Governance</title>" in response.text
    assert "<h1>Agentic SDLC Governance</h1>" in response.text
    assert "—" not in response.text


def test_home_renders_responsive_application_shell(tmp_path, monkeypatch):
    _, _, client = _configure_dashboard(tmp_path, monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in response.text
    assert '<header class="masthead">' in response.text
    assert '<svg class="brand-icon" viewBox="0 0 24 24"' in response.text
    assert '<nav class="service-nav" aria-label="Service links">' in response.text
    assert (
        '<a href="http://localhost:8080/docs" target="_blank" '
        'rel="noopener noreferrer">Swagger</a>' in response.text
    )
    assert (
        '<a href="http://localhost:8080" target="_blank" '
        'rel="noopener noreferrer">URL shortener</a>' in response.text
    )
    assert '<main class="app-shell">' in response.text
    assert '<section class="backlog" aria-labelledby="backlog-heading">' in response.text
    assert "grid-template-columns: minmax(0, 1fr) 300px" in response.text
    assert "@media (max-width: 860px)" in response.text
    assert ".app-shell { grid-template-columns: 1fr; }" in response.text
    assert "min-width: 320px" in response.text
    assert 'details[open] > summary::after { content: "-"; transform: none; }' in response.text
    assert '<aside class="run-panel" aria-label="Latest run summary">' not in response.text


def test_home_focuses_one_requirement_and_marks_approval_for_enhancement(
    tmp_path, monkeypatch
):
    workspace, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    focused = _create(repository, "Focused approval", requirement_type="ambiguous")
    other = _create(repository, "Other requirement")
    repository.transition_authoring(focused.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(focused.requirement_id, "run-focused")
    repository.sync_execution(
        focused.requirement_id,
        "run-focused",
        ExecutionStatus.AWAITING_APPROVAL,
    )
    _write_run(
        workspace,
        run_id="run-focused",
        requirement_id=focused.requirement_id,
        node_state="AWAITING_APPROVAL",
        approval_question="Approve focused requirement?",
    )

    response = client.get("/", params={"focus": focused.requirement_id})

    assert response.status_code == 200
    assert (
        f'<details class="card" id="requirement-{focused.requirement_id}" '
        f'data-requirement-id="{focused.requirement_id}" open>' in response.text
    )
    assert (
        f'<details class="card" id="requirement-{other.requirement_id}" '
        f'data-requirement-id="{other.requirement_id}">' in response.text
    )
    assert 'data-approval-form' in response.text
    assert 'id="approval-status" aria-live="polite"' in response.text
    assert 'data-approval-enhancement' in response.text

    invalid = client.get("/", params={"focus": '<script>alert("focus")</script>'})
    assert '<details class="card" id="requirement-' in invalid.text
    assert 'data-requirement-id="&lt;script&gt;' not in invalid.text
    assert '<details class="card" id="requirement-' in invalid.text
    assert ' open>' not in invalid.text


def test_home_renders_latest_run_as_summary_panel(tmp_path, monkeypatch):
    workspace, _, client = _configure_dashboard(tmp_path, monkeypatch)
    run_root = workspace / "runs" / "run-latest"
    run_root.mkdir()
    (run_root / "state.json").write_text(json.dumps({"created_at": 1}))
    (run_root / "metrics.json").write_text(json.dumps({"success_rate": 1.0}))

    response = client.get("/")

    assert response.status_code == 200
    assert '<aside class="run-panel" aria-label="Latest run summary">' in response.text
    assert '<ul class="metrics"><li><b>success_rate</b>: 1.0</li></ul>' in response.text


def test_add_requirement_validates_and_round_trips_fields(tmp_path, monkeypatch):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)

    response = client.post(
        "/requirements",
        data={
            "requirement_type": "ambiguous",
            "title": "Choose storage",
            "intent": "Select durable storage",
            "acceptance": "Survives restart\nHandles retries",
            "constraints": "No external service",
            "interpretations": "SQLite\nPostgreSQL",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    created = repository.list_requirements()[0]
    assert created.authoring_status == AuthoringStatus.DRAFT
    assert created.execution_status == ExecutionStatus.NOT_STARTED
    assert created.acceptance == ["Survives restart", "Handles retries"]
    assert created.constraints == ["No external service"]
    assert created.possible_interpretations == ["SQLite", "PostgreSQL"]
    assert client.post(
        "/requirements",
        data={"requirement_type": "unknown", "title": "Bad", "intent": "Bad"},
    ).status_code == 422
    assert client.post(
        "/requirements",
        data={"requirement_type": "greenfield", "title": "   ", "intent": "Bad"},
    ).status_code == 422


def test_lifecycle_actions_and_conflicts(tmp_path, monkeypatch):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    draft = _create(repository, "Lifecycle")

    assert client.post(f"/requirements/{draft.requirement_id}/ready").status_code == 200
    assert repository.get(draft.requirement_id).authoring_status == AuthoringStatus.READY
    assert client.post(f"/requirements/{draft.requirement_id}/ready").status_code == 409
    assert client.post(f"/requirements/{draft.requirement_id}/archive").status_code == 200
    assert repository.get(draft.requirement_id).authoring_status == AuthoringStatus.ARCHIVED
    assert client.post(f"/requirements/{draft.requirement_id}/restore").status_code == 200
    assert repository.get(draft.requirement_id).authoring_status == AuthoringStatus.DRAFT
    assert client.post("/requirements/REQ-999/ready").status_code == 404

    busy = _create(repository, "Busy")
    repository.transition_authoring(busy.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(busy.requirement_id, "run-busy")
    assert client.post(f"/requirements/{busy.requirement_id}/archive").status_code == 409


def test_analyze_persists_success_failure_and_retry_without_automatic_calls(
    tmp_path, monkeypatch
):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    good = _create(repository, "Good")
    flaky = _create(repository, "Flaky")
    calls: list[str] = []

    def analyze(requirement):
        calls.append(requirement["id"])
        if requirement["id"] == flaky.requirement_id and calls.count(flaky.requirement_id) == 1:
            raise RuntimeError("provider <offline>")
        return {
            "acceptance": [f"Analyzed {requirement['id']}", "<safe>"],
            "ambiguous": False,
            "ambiguities": [],
            "interpretations": ["Model <option>"],
        }

    monkeypatch.setattr(dashboard, "analyze_requirement", analyze)
    assert client.get("/").status_code == 200
    assert calls == []

    assert client.post(f"/requirements/{good.requirement_id}/analyze").status_code == 200
    assert client.post(f"/requirements/{flaky.requirement_id}/analyze").status_code == 200
    failed_page = client.get("/")
    assert "provider &lt;offline&gt;" in failed_page.text
    assert f"Analyzed {good.requirement_id}" in failed_page.text
    assert "Model &lt;option&gt;" in failed_page.text
    assert "Model <option>" not in failed_page.text

    assert client.post(f"/requirements/{flaky.requirement_id}/analyze").status_code == 200
    retried = repository.get(flaky.requirement_id)
    assert retried.analysis_status.value == "ready"
    assert retried.analysis_error is None


def test_successful_analysis_reveals_implement_action(tmp_path, monkeypatch):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    record = _create(repository, "Ready to implement")
    repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
    monkeypatch.setattr(
        dashboard,
        "analyze_requirement",
        lambda requirement: {
            "acceptance": [f"Analyzed {requirement['id']}"],
            "ambiguous": False,
            "ambiguities": [],
            "interpretations": [],
        },
    )

    before_analysis = client.get("/")
    assert f'action="/requirements/{record.requirement_id}/implement"' not in before_analysis.text

    response = client.post(f"/requirements/{record.requirement_id}/analyze")

    assert response.status_code == 200
    assert f'action="/requirements/{record.requirement_id}/implement"' in response.text
    assert ">Implement</button>" in response.text


def test_implement_requires_eligible_requirement_and_runs_workflow(tmp_path, monkeypatch):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    record = _create(repository, "Implement safely")
    repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
    prepared: list[str] = []
    run = SimpleNamespace(id="run-implement")
    store = SimpleNamespace(run_id="run-implement")

    def prepare_run(requirement_id: str, *, publish_changes: bool = False):
        prepared.append(f"{requirement_id}:{publish_changes}")
        return run, store, repository

    executions: list[tuple[object, object, RequirementsRepository]] = []

    class RecordingKernel:
        def __init__(self, selected_run, selected_store, config, *, requirements_repository):
            executions.append((selected_run, selected_store, requirements_repository))

        def run_until_blocked(self):
            return "completed"

    monkeypatch.setattr(dashboard, "_prepare_run", prepare_run)
    monkeypatch.setattr(dashboard, "Kernel", RecordingKernel)
    monkeypatch.setattr(
        dashboard.RepositoryConfig,
        "discover",
        lambda: SimpleNamespace(github_repository="acme/widget"),
    )
    queued: list[tuple[object, tuple]] = []

    class RecordingExecutor:
        def submit(self, function, *args):
            queued.append((function, args))

    monkeypatch.setattr(dashboard, "_IMPLEMENT_EXECUTOR", RecordingExecutor())

    not_analyzed = client.post(
        f"/requirements/{record.requirement_id}/implement",
        follow_redirects=False,
    )
    assert not_analyzed.status_code == 409
    assert prepared == []

    repository.record_analysis(
        record.requirement_id,
        analysis={
            "acceptance": [],
            "ambiguous": False,
            "ambiguities": [],
            "interpretations": [],
        },
    )
    response = client.post(
        f"/requirements/{record.requirement_id}/implement",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert prepared == [f"{record.requirement_id}:True"]
    assert executions == []
    assert len(queued) == 1

    function, arguments = queued[0]
    function(*arguments)

    assert executions == [(run, store, repository)]


def test_stopped_requirement_can_retry_and_displays_publication(tmp_path, monkeypatch):
    workspace, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    record = _create(repository, "Retry publishing")
    repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
    repository.record_analysis(
        record.requirement_id,
        analysis={
            "acceptance": [],
            "ambiguous": False,
            "ambiguities": [],
            "interpretations": [],
        },
    )
    repository.mark_run_started(record.requirement_id, "run-published")
    repository.sync_execution(record.requirement_id, "run-published", ExecutionStatus.STOPPED)
    run_root = _write_run(
        workspace,
        run_id="run-published",
        requirement_id=record.requirement_id,
        node_state="STOPPED",
    )
    (run_root / "context.json").write_text(
        json.dumps(
            {
                "publication": {
                    "outcome": "pr_opened",
                    "pr_url": "https://github.com/acme/widget/pull/17?x=<unsafe>",
                    "pr_number": 17,
                }
            }
        )
    )

    response = client.get("/")

    assert ">Retry implementation</button>" in response.text
    assert "Pull request #17" in response.text
    assert "&lt;unsafe&gt;" in response.text
    assert "<unsafe>" not in response.text


def test_publication_link_rejects_a_non_github_url(tmp_path, monkeypatch):
    workspace, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    record = _create(repository, "Unsafe publication")
    repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(record.requirement_id, "run-unsafe-link")
    run_root = _write_run(
        workspace,
        run_id="run-unsafe-link",
        requirement_id=record.requirement_id,
        node_state="RUNNING",
    )
    (run_root / "context.json").write_text(
        json.dumps(
            {
                "publication": {
                    "outcome": "pr_opened",
                    "pr_url": "javascript:alert(1)",
                    "pr_number": 99,
                }
            }
        )
    )

    response = client.get("/")

    assert "javascript:alert(1)" not in response.text
    assert "Pull request #99" not in response.text


def test_background_worker_stops_and_audits_an_unhandled_failure(tmp_path, monkeypatch):
    workspace, repository, _ = _configure_dashboard(tmp_path, monkeypatch)
    record = _create(repository, "Fail safely")
    repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(record.requirement_id, "run-failed")
    run = SimpleNamespace(id="run-failed", requirement_id=record.requirement_id)
    store = RunStore("run-failed", str(workspace))

    class FailingKernel:
        def __init__(self, *args, **kwargs):
            pass

        def run_until_blocked(self):
            raise RuntimeError("worker exploded")

    monkeypatch.setattr(dashboard, "Kernel", FailingKernel)

    dashboard._run_implementation(run, store, repository)

    assert repository.get(record.requirement_id).execution_status == ExecutionStatus.STOPPED
    assert "background_run_failed" in (store.root / "audit.log").read_text()


def test_home_escapes_database_content(tmp_path, monkeypatch):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    record = repository.create(
        requirement_type="greenfield",
        title="Build <script>alert(1)</script>",
        intent='Render <img src=x onerror="bad"> safely',
        acceptance=["Never emit <unsafe>"],
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "&lt;img src=x onerror=&quot;bad&quot;&gt;" in response.text
    assert "Never emit &lt;unsafe&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert record.requirement_id in response.text


def test_analyze_rejects_active_requirement_before_calling_model(tmp_path, monkeypatch):
    _, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    record = _create(repository, "Active analysis")
    repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(record.requirement_id, "run-active")
    calls = []
    monkeypatch.setattr(dashboard, "analyze_requirement", lambda requirement: calls.append(requirement))

    response = client.post(f"/requirements/{record.requirement_id}/analyze")

    assert response.status_code == 409
    assert calls == []


def test_decision_targets_submitted_run_and_syncs_through_repository(tmp_path, monkeypatch):
    workspace, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    first = _create(repository, "One", requirement_type="ambiguous")
    second = _create(repository, "Two", requirement_type="ambiguous")
    for record, run_id in ((first, "run-one"), (second, "run-two")):
        repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
        repository.mark_run_started(record.requirement_id, run_id)
        repository.sync_execution(record.requirement_id, run_id, ExecutionStatus.AWAITING_APPROVAL)
    first_root = _write_run(
        workspace,
        run_id="run-one",
        requirement_id=first.requirement_id,
        node_state="AWAITING_APPROVAL",
        approval_question="Approve one?",
    )
    second_root = _write_run(
        workspace,
        run_id="run-two",
        requirement_id=second.requirement_id,
        node_state="AWAITING_APPROVAL",
        approval_question="Approve two?",
    )
    monkeypatch.setattr(
        dashboard,
        "rehydrate_run",
        lambda run_id: (
            SimpleNamespace(nodes={"req": SimpleNamespace(state=NodeState.AWAITING_APPROVAL)}),
            RunStore(run_id, str(workspace)),
        ),
    )
    repositories = []

    class NoopKernel:
        def __init__(self, run, store, config, *, requirements_repository=None):
            self.store = store
            repositories.append(requirements_repository)

        def resume(self):
            return "blocked"

    monkeypatch.setattr(dashboard, "Kernel", NoopKernel)

    response = client.post(
        "/decide",
        data={"run_id": "run-one", "approval": "APR-001", "decision": "approve"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/?focus={first.requirement_id}#requirement-{first.requirement_id}"
    )
    assert repositories == [repository]
    assert json.loads((first_root / "approvals" / "APR-001.json").read_text())["status"] == "approve"
    assert json.loads((second_root / "approvals" / "APR-001.json").read_text())["status"] == "pending"
    assert client.post(
        "/decide",
        data={"run_id": "../outside", "approval": "APR-001", "decision": "approve"},
        follow_redirects=False,
    ).status_code == 404
    assert not (tmp_path / "outside").exists()


def test_concurrent_decisions_resume_once_and_audit_is_escaped(tmp_path, monkeypatch):
    workspace, repository, client = _configure_dashboard(tmp_path, monkeypatch)
    record = _create(repository, "Race", requirement_type="ambiguous")
    repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(record.requirement_id, "run-race")
    repository.sync_execution(record.requirement_id, "run-race", ExecutionStatus.AWAITING_APPROVAL)
    run_root = _write_run(
        workspace,
        run_id="run-race",
        requirement_id=record.requirement_id,
        node_state="AWAITING_APPROVAL",
        approval_question="Choose once?",
    )
    monkeypatch.setattr(
        dashboard,
        "rehydrate_run",
        lambda run_id: (
            SimpleNamespace(nodes={"req": SimpleNamespace(state=NodeState.AWAITING_APPROVAL)}),
            RunStore(run_id, str(workspace)),
        ),
    )
    resumes: list[str] = []

    class CountingKernel:
        def __init__(self, run, store, config, *, requirements_repository=None):
            self.store = store

        def resume(self):
            resumes.append(self.store.run_id)
            return "blocked"

    monkeypatch.setattr(dashboard, "Kernel", CountingKernel)

    def submit(decision: str):
        return client.post(
            "/decide",
            data={"run_id": "run-race", "approval": "APR-001", "decision": decision},
            follow_redirects=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, ("approve", "reject")))

    assert sorted(response.status_code for response in responses) == [303, 409]
    successful = next(response for response in responses if response.status_code == 303)
    assert successful.headers["location"] == (
        f"/?focus={record.requirement_id}#requirement-{record.requirement_id}"
    )
    assert len(resumes) == 1
    (run_root / "audit.log").write_text('<script>alert("audit")</script>')
    audit = client.get("/audit")
    assert "&lt;script&gt;alert(&quot;audit&quot;)&lt;/script&gt;" in audit.text
    assert '<script>alert("audit")</script>' not in audit.text
