"""Integration tests between requirements persistence and orchestration."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agents.planner as planner_module
import orchestrator.cli as cli_module
from agents import Planner
from agents import registry as agent_registry
from orchestrator.context import RunStore
from orchestrator.gates import decide, pending_approvals
from orchestrator.kernel import Kernel
from orchestrator.requirements_store import (
    AnalysisStatus,
    AuthoringStatus,
    ExecutionStatus,
    RequirementsRepository,
)
from orchestrator.state import Gate, Node, NodeState, Run


def _create_requirement(repository: RequirementsRepository, *, ambiguous: bool = False):
    record = repository.create(
        requirement_type="ambiguous" if ambiguous else "greenfield",
        title="Persistent requirement",
        intent="Choose a behavior" if ambiguous else "Build a behavior",
        possible_interpretations=["Option A", "Option B"] if ambiguous else [],
    )
    return repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)


def test_planner_reuses_ready_database_analysis(tmp_path, monkeypatch):
    repository = RequirementsRepository(tmp_path / "workspace")
    record = _create_requirement(repository)
    analysis = {
        "acceptance": ["Stored acceptance"],
        "ambiguous": False,
        "ambiguities": [],
        "interpretations": [],
    }
    repository.record_analysis(record.requirement_id, analysis=analysis)
    run = Run.new(record.requirement_id, record.requirement_type)
    store = RunStore(run.id, str(tmp_path / "workspace"))

    def unexpected_analysis(_requirement):
        raise AssertionError("ready analysis should be reused")

    monkeypatch.setattr(planner_module, "analyze_requirement", unexpected_analysis)

    Planner().plan(
        run,
        record.to_requirement_dict(),
        store,
        requirements_repository=repository,
    )

    assert store.read_context()["requirements_analysis"] == analysis


def test_planner_persists_new_analysis(tmp_path, monkeypatch):
    repository = RequirementsRepository(tmp_path / "workspace")
    record = _create_requirement(repository)
    analysis = {
        "acceptance": ["Generated acceptance"],
        "ambiguous": False,
        "ambiguities": [],
        "interpretations": [],
    }
    monkeypatch.setattr(planner_module, "analyze_requirement", lambda _requirement: analysis)
    run = Run.new(record.requirement_id, record.requirement_type)
    store = RunStore(run.id, str(tmp_path / "workspace"))

    Planner().plan(
        run,
        record.to_requirement_dict(),
        store,
        requirements_repository=repository,
    )

    persisted = repository.get(record.requirement_id)
    assert persisted.analysis_status == AnalysisStatus.READY
    assert persisted.analysis == analysis


def test_kernel_synchronizes_awaiting_and_implemented_statuses(tmp_path):
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace)
    record = _create_requirement(repository)
    requirement = record.to_requirement_dict()
    run = Run.new(record.requirement_id, record.requirement_type)
    store = RunStore(run.id, str(workspace))
    Planner().plan(run, requirement, store, requirements_repository=repository)
    store.save_run(run)
    store.write_context("requirement", requirement)
    repository.mark_run_started(record.requirement_id, run.id)
    kernel = Kernel(run, store, dict(os.environ), requirements_repository=repository)

    assert kernel.run_until_blocked() == "blocked"
    assert repository.get(record.requirement_id).execution_status == ExecutionStatus.AWAITING_APPROVAL

    approval = pending_approvals(store)[0]
    decide(store, approval["id"], "approve")
    assert kernel.resume() == "complete"
    completed = repository.get(record.requirement_id)
    assert completed.execution_status == ExecutionStatus.IMPLEMENTED
    assert completed.current_run_id == run.id
    assert completed.implemented_at is not None


def test_kernel_synchronizes_rejected_run_as_stopped(tmp_path):
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace)
    record = _create_requirement(repository, ambiguous=True)
    requirement = record.to_requirement_dict()
    run = Run.new(record.requirement_id, record.requirement_type)
    store = RunStore(run.id, str(workspace))
    Planner().plan(run, requirement, store, requirements_repository=repository)
    store.save_run(run)
    store.write_context("requirement", requirement)
    repository.mark_run_started(record.requirement_id, run.id)
    kernel = Kernel(run, store, dict(os.environ), requirements_repository=repository)

    assert kernel.run_until_blocked() == "blocked"
    approval = pending_approvals(store)[0]
    decide(store, approval["id"], "reject")
    assert kernel.resume() == "complete"
    assert repository.get(record.requirement_id).execution_status == ExecutionStatus.STOPPED


def test_cli_loads_by_database_id_and_rejects_implemented_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(
        cli_module,
        "REQUIREMENTS_SEED_DIR",
        cli_module.Path("workspace/requirements"),
        raising=False,
    )

    result = CliRunner().invoke(cli_module.app, ["run", "--req", "REQ-001"])

    assert result.exit_code == 1
    assert "Requirement is not ready to run" in result.output


@pytest.mark.parametrize("failing_write", ["save_run", "requirement_context"])
def test_prepare_run_compensates_when_run_persistence_fails(
    tmp_path, monkeypatch, failing_write
):
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace)
    record = _create_requirement(repository)
    monkeypatch.setattr(cli_module, "requirements_repository", lambda: repository)

    if failing_write == "save_run":
        monkeypatch.setattr(RunStore, "save_run", lambda self, run: (_ for _ in ()).throw(OSError("disk")))
    else:
        original_write_context = RunStore.write_context

        def fail_requirement_context(self, key, value):
            if key == "requirement":
                raise OSError("disk")
            return original_write_context(self, key, value)

        monkeypatch.setattr(RunStore, "write_context", fail_requirement_context)

    with pytest.raises(OSError, match="disk"):
        cli_module._prepare_run(record.requirement_id)

    assert repository.get(record.requirement_id).execution_status == ExecutionStatus.STOPPED


def test_prepare_run_recovers_an_orphaned_active_registration(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace)
    record = _create_requirement(repository)
    repository.mark_run_started(record.requirement_id, "run-missing")
    monkeypatch.setattr(cli_module, "requirements_repository", lambda: repository)
    monkeypatch.setattr(cli_module, "ORPHAN_GRACE_SECONDS", 0)

    run, _, _ = cli_module._prepare_run(record.requirement_id)

    recovered = repository.get(record.requirement_id)
    assert recovered.current_run_id == run.id
    assert recovered.execution_status == ExecutionStatus.IN_PROGRESS


def test_reconciliation_treats_rolled_back_snapshot_as_stopped(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    repository = RequirementsRepository(workspace)
    record = _create_requirement(repository)
    run = Run.new(record.requirement_id, record.requirement_type)
    run.nodes["failed"] = Node(
        id="failed",
        stage="implementation",
        agent="implementer",
        title="Failed work",
        state=NodeState.ROLLED_BACK,
    )
    store = RunStore(run.id, str(workspace))
    store.save_run(run)
    store.write_context("requirement", record.to_requirement_dict())
    repository.mark_run_started(record.requirement_id, run.id)
    monkeypatch.setattr(cli_module, "ORPHAN_GRACE_SECONDS", 0)

    reconciled = cli_module._reconcile_active_registration(
        repository,
        repository.get(record.requirement_id),
    )

    assert reconciled.execution_status == ExecutionStatus.STOPPED


def test_reconciliation_does_not_stop_a_fresh_retry_snapshot(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    repository = RequirementsRepository(workspace)
    record = _create_requirement(repository)
    run = Run.new(record.requirement_id, record.requirement_type)
    run.nodes["retrying"] = Node(
        id="retrying",
        stage="implementation",
        agent="implementer",
        title="Retrying work",
        state=NodeState.FAILED,
    )
    store = RunStore(run.id, str(workspace))
    store.save_run(run)
    store.write_context("requirement", record.to_requirement_dict())
    repository.mark_run_started(record.requirement_id, run.id)

    reconciled = cli_module._reconcile_active_registration(
        repository,
        repository.get(record.requirement_id),
    )

    assert reconciled.execution_status == ExecutionStatus.IN_PROGRESS


def test_replan_rejects_requirement_mismatch_before_mutation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    repository = RequirementsRepository(workspace)
    first = _create_requirement(repository)
    second = _create_requirement(repository)
    run = Run.new(first.requirement_id, first.requirement_type)
    store = RunStore(run.id, str(workspace))
    store.save_run(run)
    store.write_context("requirement", first.to_requirement_dict())
    repository.mark_run_started(first.requirement_id, run.id)
    monkeypatch.setattr(cli_module, "requirements_repository", lambda: repository)

    result = CliRunner().invoke(
        cli_module.app,
        ["replan", "--run", run.id, "--req", second.requirement_id],
    )

    assert result.exit_code == 1
    assert "does not belong" in result.output
    assert store.read_context()["requirement"]["id"] == first.requirement_id


def _gated_kernel(tmp_path, repository):
    record = _create_requirement(repository)
    run = Run.new(record.requirement_id, record.requirement_type)
    run.nodes["gate"] = Node(
        id="gate",
        stage="release",
        agent="release",
        title="High impact",
        impact="high",
        entry_gate=Gate(kind="human_approval", name="signoff", prompt="Approve?"),
    )
    store = RunStore(run.id, str(tmp_path / "workspace"))
    store.save_run(run)
    repository.mark_run_started(record.requirement_id, run.id)
    kernel = Kernel(
        run,
        store,
        {"MAX_RETRIES": "0"},
        requirements_repository=repository,
    )
    return record, run, store, kernel


def test_gated_proposal_failure_safe_stops_instead_of_stranding(tmp_path, monkeypatch):
    repository = RequirementsRepository(tmp_path / "workspace")
    record, run, _, kernel = _gated_kernel(tmp_path, repository)
    monkeypatch.setattr(kernel, "_run_agent", lambda node: (_ for _ in ()).throw(RuntimeError("provider")))

    assert kernel.run_until_blocked() == "complete"
    assert run.nodes["gate"].state == NodeState.STOPPED
    assert repository.get(record.requirement_id).execution_status == ExecutionStatus.STOPPED


def test_post_approval_agent_failure_safe_stops_instead_of_stranding(tmp_path, monkeypatch):
    repository = RequirementsRepository(tmp_path / "workspace")
    record, run, store, kernel = _gated_kernel(tmp_path, repository)
    monkeypatch.setattr(
        kernel,
        "_run_agent",
        lambda node: {"artifact": "proposal", "rationale": "proposal"},
    )
    assert kernel.run_until_blocked() == "blocked"
    approval = pending_approvals(store)[0]
    decide(store, approval["id"], "approve")
    monkeypatch.setattr(kernel, "_run_agent", lambda node: (_ for _ in ()).throw(RuntimeError("provider")))

    assert kernel.resume() == "complete"
    assert run.nodes["gate"].state == NodeState.STOPPED
    assert repository.get(record.requirement_id).execution_status == ExecutionStatus.STOPPED


def test_replan_supersedes_stale_approval_and_reruns_to_a_new_gate(tmp_path):
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace)
    record = _create_requirement(repository)
    requirement = record.to_requirement_dict()
    run = Run.new(record.requirement_id, record.requirement_type)
    store = RunStore(run.id, str(workspace))
    repository.mark_run_started(record.requirement_id, run.id)
    Planner().plan(run, requirement, store, requirements_repository=repository)
    store.save_run(run)
    store.write_context("requirement", requirement)
    kernel = Kernel(run, store, dict(os.environ), requirements_repository=repository)
    assert kernel.run_until_blocked() == "blocked"
    first = pending_approvals(store)[0]
    repository.mark_replan_started(record.requirement_id, run.id)

    assert kernel.replan(record.requirement_id, lambda replanned: Planner().replan(replanned)) == "blocked"

    approvals = sorted(store.approvals.glob("APR-*.json"))
    old = json.loads(approvals[0].read_text())
    pending = pending_approvals(store)
    assert old["status"] == "superseded"
    assert len(pending) == 1
    assert pending[0]["id"] != first["id"]
    assert run.nodes["req"].attempts == 2


@pytest.mark.parametrize(
    ("title", "profile_name", "acceptance", "forbidden_design_text"),
    [
        (
            "Password-protected short links",
            "password_protection",
            [
                "POST /shorten accepts an optional password",
                "Protected links require X-Link-Password",
            ],
            "expires_at",
        ),
        (
            "Bulk URL shortening with idempotent retries",
            "bulk_idempotency",
            [
                "POST /shorten/batch accepts between 1 and 100 items",
                "Retries with the same key do not create duplicate links",
            ],
            "custom_alias, expiry_days",
        ),
    ],
)
def test_profile_workflow_reaches_release_gate_with_real_evidence(
    tmp_path,
    monkeypatch,
    title,
    profile_name,
    acceptance,
    forbidden_design_text,
):
    monkeypatch.setenv("LLM_MODE", "mock")
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace)
    record = repository.create(
        requirement_type="brownfield",
        title=title,
        intent=title,
        acceptance=acceptance,
    )
    record = repository.transition_authoring(record.requirement_id, AuthoringStatus.READY)
    requirement = record.to_requirement_dict()
    run = Run.new(record.requirement_id, record.requirement_type)
    store = RunStore(run.id, str(workspace))
    Planner().plan(run, requirement, store, requirements_repository=repository)
    store.save_run(run)
    store.write_context("requirement", requirement)
    repository.mark_run_started(record.requirement_id, run.id)

    implementer = agent_registry.get("implementer")
    monkeypatch.setattr(implementer, "app_path", tmp_path / "target-app" / "main.py")
    monkeypatch.setattr(
        implementer,
        "template_path",
        Path("agents/templates/target_app_main.py").resolve(),
    )
    kernel = Kernel(
        run,
        store,
        {"MAX_RETRIES": "0"},
        requirements_repository=repository,
    )

    assert kernel.run_until_blocked() == "blocked"
    architecture_approval = pending_approvals(store)[0]
    assert architecture_approval["node"] == "arch"
    decide(store, architecture_approval["id"], "approve")
    assert kernel.resume() == "blocked"

    context = store.read_context()
    assert context["demo_profile"] == profile_name
    assert context["implementation"]["profile"] == profile_name
    assert context["implementation"]["sha256"]
    assert context["test_report"]["return_code"] == 0
    assert context["test_report"]["failed"] == 0
    assert context["test_report"]["total"] == len(
        context["test_report"]["test_node_ids"]
    )
    assert forbidden_design_text not in str(context["design"])
    assert run.nodes["impl"].state == NodeState.PASSED
    assert run.nodes["unit"].state == NodeState.PASSED
    assert run.nodes["release"].state == NodeState.AWAITING_APPROVAL
