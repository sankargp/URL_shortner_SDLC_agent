"""Tests for the persistent Governance requirements repository."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from orchestrator.requirements_store import (
    AnalysisStatus,
    AuthoringStatus,
    ExecutionStatus,
    RequirementConflict,
    RequirementsRepository,
)


def _write_seed(directory: Path, requirement: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{requirement['id']}.yaml").write_text(
        yaml.safe_dump(requirement, sort_keys=False)
    )


def _seed_directory(tmp_path: Path) -> Path:
    seed_dir = tmp_path / "seed"
    _write_seed(
        seed_dir,
        {
            "id": "REQ-001",
            "type": "greenfield",
            "title": "Core shortener",
            "intent": "Shorten URLs",
            "acceptance": ["Create code"],
            "constraints": ["SQLite"],
            "status": "approved",
        },
    )
    _write_seed(
        seed_dir,
        {
            "id": "REQ-002",
            "type": "brownfield",
            "title": "Aliases",
            "intent": "Add aliases",
            "acceptance": ["Custom alias"],
            "constraints": ["Backward compatible"],
            "status": "approved",
        },
    )
    _write_seed(
        seed_dir,
        {
            "id": "REQ-003",
            "type": "ambiguous",
            "title": "Reliability",
            "intent": "Improve reliability",
            "acceptance": [],
            "possible_interpretations": ["Rate limiting", "Caching"],
            "status": "proposed",
        },
    )
    return seed_dir


def test_seed_is_idempotent_and_preserves_database_statuses(tmp_path):
    seed_dir = _seed_directory(tmp_path)
    workspace = tmp_path / "workspace"

    first = RequirementsRepository(workspace, seed_dir=seed_dir)
    seeded = first.list_requirements()

    assert [record.requirement_id for record in seeded] == ["REQ-001", "REQ-002", "REQ-003"]
    assert seeded[0].authoring_status == AuthoringStatus.READY
    assert seeded[0].execution_status == ExecutionStatus.IMPLEMENTED
    assert seeded[1].authoring_status == AuthoringStatus.READY
    assert seeded[1].execution_status == ExecutionStatus.NOT_STARTED
    assert seeded[2].authoring_status == AuthoringStatus.DRAFT
    assert seeded[2].execution_status == ExecutionStatus.NOT_STARTED
    assert seeded[2].possible_interpretations == ["Rate limiting", "Caching"]

    first.transition_authoring("REQ-002", AuthoringStatus.ARCHIVED)
    second = RequirementsRepository(workspace, seed_dir=seed_dir)

    assert second.get("REQ-002").authoring_status == AuthoringStatus.ARCHIVED
    assert len(second.list_requirements()) == 3


def test_concurrent_initialization_seeds_each_requirement_once(tmp_path):
    seed_dir = _seed_directory(tmp_path)
    workspace = tmp_path / "workspace"

    with ThreadPoolExecutor(max_workers=2) as executor:
        repositories = list(
            executor.map(
                lambda _: RequirementsRepository(workspace, seed_dir=seed_dir),
                range(2),
            )
        )

    assert len(repositories[0].list_requirements()) == 3
    assert len(repositories[1].list_requirements()) == 3


def test_create_generates_next_public_id_and_persists_json_fields(tmp_path):
    seed_dir = _seed_directory(tmp_path)
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace, seed_dir=seed_dir)

    created = repository.create(
        requirement_type="greenfield",
        title="  Add reporting  ",
        intent="  Report usage  ",
        acceptance=["Daily report", "", "CSV export"],
        constraints=["No paid services"],
        possible_interpretations=["Email", "Dashboard"],
    )

    assert created.requirement_id == "REQ-004"
    assert created.title == "Add reporting"
    assert created.intent == "Report usage"
    assert created.acceptance == ["Daily report", "CSV export"]
    assert created.constraints == ["No paid services"]
    assert created.possible_interpretations == ["Email", "Dashboard"]
    assert created.authoring_status == AuthoringStatus.DRAFT
    assert created.execution_status == ExecutionStatus.NOT_STARTED
    assert created.analysis_status == AnalysisStatus.NOT_REQUESTED

    reopened = RequirementsRepository(workspace, seed_dir=seed_dir)
    assert reopened.get("REQ-004") == created


def test_lifecycle_and_execution_transitions_are_guarded(tmp_path):
    repository = RequirementsRepository(tmp_path / "workspace")
    requirement = repository.create(
        requirement_type="greenfield",
        title="Lifecycle",
        intent="Exercise transitions",
    )

    ready = repository.transition_authoring(requirement.requirement_id, AuthoringStatus.READY)
    assert ready.authoring_status == AuthoringStatus.READY

    running = repository.mark_run_started(requirement.requirement_id, "run-001")
    assert running.execution_status == ExecutionStatus.IN_PROGRESS
    assert running.current_run_id == "run-001"

    with pytest.raises(RequirementConflict):
        repository.transition_authoring(requirement.requirement_id, AuthoringStatus.ARCHIVED)
    with pytest.raises(RequirementConflict):
        repository.mark_run_started(requirement.requirement_id, "run-duplicate", force=True)

    awaiting = repository.sync_execution(
        requirement.requirement_id,
        "run-001",
        ExecutionStatus.AWAITING_APPROVAL,
    )
    assert awaiting.execution_status == ExecutionStatus.AWAITING_APPROVAL

    stopped = repository.sync_execution(
        requirement.requirement_id,
        "run-001",
        ExecutionStatus.STOPPED,
    )
    assert stopped.execution_status == ExecutionStatus.STOPPED

    archived = repository.transition_authoring(requirement.requirement_id, AuthoringStatus.ARCHIVED)
    assert archived.authoring_status == AuthoringStatus.ARCHIVED
    restored = repository.transition_authoring(requirement.requirement_id, AuthoringStatus.DRAFT)
    assert restored.authoring_status == AuthoringStatus.DRAFT


def test_force_rules_and_stale_run_updates(tmp_path):
    seed_dir = _seed_directory(tmp_path)
    repository = RequirementsRepository(tmp_path / "workspace", seed_dir=seed_dir)

    with pytest.raises(RequirementConflict):
        repository.mark_run_started("REQ-001", "run-implemented")
    forced = repository.mark_run_started("REQ-001", "run-implemented", force=True)
    assert forced.execution_status == ExecutionStatus.IN_PROGRESS

    with pytest.raises(RequirementConflict):
        repository.sync_execution("REQ-001", "run-stale", ExecutionStatus.STOPPED)

    repository.sync_execution("REQ-001", "run-implemented", ExecutionStatus.IMPLEMENTED)
    repository.transition_authoring("REQ-001", AuthoringStatus.ARCHIVED)
    with pytest.raises(RequirementConflict):
        repository.mark_run_started("REQ-001", "run-archived", force=True)


def test_analysis_success_and_failure_are_persisted(tmp_path):
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace)
    requirement = repository.create(
        requirement_type="ambiguous",
        title="Analyze",
        intent="Clarify intent",
    )
    analysis = {
        "acceptance": ["Chosen behavior"],
        "ambiguous": True,
        "ambiguities": ["Scope"],
        "interpretations": ["Option A"],
    }

    ready = repository.record_analysis(requirement.requirement_id, analysis=analysis)
    assert ready.analysis_status == AnalysisStatus.READY
    assert ready.analysis == analysis
    assert ready.analysis_error is None

    failed = repository.record_analysis(requirement.requirement_id, error="provider unavailable")
    assert failed.analysis_status == AnalysisStatus.FAILED
    assert failed.analysis is None
    assert failed.analysis_error == "provider unavailable"

    reopened = RequirementsRepository(workspace)
    assert reopened.get(requirement.requirement_id) == failed


def test_analysis_rejects_active_runs_and_stale_results(tmp_path):
    repository = RequirementsRepository(tmp_path / "workspace")
    requirement = repository.create(
        requirement_type="greenfield",
        title="Analyze safely",
        intent="Keep analysis consistent",
    )
    stale_version = requirement.revision
    repository.record_analysis(
        requirement.requirement_id,
        analysis={"acceptance": ["newer"]},
        expected_revision=stale_version,
    )

    with pytest.raises(RequirementConflict):
        repository.record_analysis(
            requirement.requirement_id,
            analysis={"acceptance": ["stale"]},
            expected_revision=stale_version,
        )

    repository.transition_authoring(requirement.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(requirement.requirement_id, "run-analysis")
    with pytest.raises(RequirementConflict):
        repository.record_analysis(
            requirement.requirement_id,
            analysis={"acceptance": ["ui overwrite"]},
        )

    planned = repository.record_analysis(
        requirement.requirement_id,
        analysis={"acceptance": ["run-owned"]},
        expected_run_id="run-analysis",
    )
    assert planned.analysis == {"acceptance": ["run-owned"]}


def test_replan_registration_requires_current_active_unarchived_run(tmp_path):
    repository = RequirementsRepository(tmp_path / "workspace")
    requirement = repository.create(
        requirement_type="greenfield",
        title="Replan safely",
        intent="Preserve requirement identity",
    )
    repository.transition_authoring(requirement.requirement_id, AuthoringStatus.READY)
    repository.mark_run_started(requirement.requirement_id, "run-current")
    repository.sync_execution(
        requirement.requirement_id,
        "run-current",
        ExecutionStatus.AWAITING_APPROVAL,
    )

    replanning = repository.mark_replan_started(requirement.requirement_id, "run-current")
    assert replanning.execution_status == ExecutionStatus.IN_PROGRESS
    with pytest.raises(RequirementConflict):
        repository.mark_replan_started(requirement.requirement_id, "run-stale")

    repository.sync_execution(
        requirement.requirement_id,
        "run-current",
        ExecutionStatus.STOPPED,
    )
    repository.transition_authoring(requirement.requirement_id, AuthoringStatus.ARCHIVED)
    with pytest.raises(RequirementConflict):
        repository.mark_replan_started(requirement.requirement_id, "run-current")
