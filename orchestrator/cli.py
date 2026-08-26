"""`orchestrator` command: run / resume / replan / demo / status.

This is the primary trigger surface. It loads a requirement, asks the Planner
for a DAG, drives the Kernel until completion or a human-approval checkpoint,
and prints a readable summary + metrics.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agents import Planner

from .context import RunStore, latest_run
from .demo_catalog import DuplicateValidationError, refresh_demo_requirements
from .kernel import Kernel
from .requirements_store import (
    ExecutionStatus,
    RequirementConflict,
    RequirementNotFound,
    RequirementRecord,
    RequirementsRepository,
)
from .state import Run

app = typer.Typer(add_completion=False, help="Agentic SDLC orchestrator")
console = Console()
REQUIREMENTS_SEED_DIR = Path("workspace/requirements")
ORPHAN_GRACE_SECONDS = 300


def requirements_repository() -> RequirementsRepository:
    return RequirementsRepository(
        os.getenv("WORKSPACE_DIR", "workspace"),
        seed_dir=REQUIREMENTS_SEED_DIR,
    )


def load_requirement(
    requirement_id: str,
    repository: RequirementsRepository | None = None,
) -> dict:
    repository = repository or requirements_repository()
    return repository.get(requirement_id).to_requirement_dict()


def _reconcile_active_registration(
    repository: RequirementsRepository,
    record: RequirementRecord,
) -> RequirementRecord:
    """Recover a busy database row from its current file-backed run snapshot."""
    if record.execution_status not in {
        ExecutionStatus.IN_PROGRESS,
        ExecutionStatus.AWAITING_APPROVAL,
    }:
        return record
    workspace = Path(os.getenv("WORKSPACE_DIR", "workspace"))
    runs_root = (workspace / "runs").resolve()
    run_id = record.current_run_id or ""
    run_root = (runs_root / run_id).resolve()
    state_path = run_root / "state.json"
    context_path = run_root / "context.json"
    try:
        if run_root.parent != runs_root:
            raise ValueError("invalid run path")
        state = json.loads(state_path.read_text())
        context = json.loads(context_path.read_text())
        if (
            state.get("id") != run_id
            or state.get("requirement_id") != record.requirement_id
            or (context.get("requirement") or {}).get("id") != record.requirement_id
        ):
            raise ValueError("run identity mismatch")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        age_seconds = (datetime.now(UTC) - record.updated_at).total_seconds()
        if age_seconds < ORPHAN_GRACE_SECONDS:
            return record
        return repository.sync_execution(
            record.requirement_id,
            run_id,
            ExecutionStatus.STOPPED,
        )

    nodes = list((state.get("nodes") or {}).values())
    node_states = {node.get("state") for node in nodes}
    terminal = {"PASSED", "ROLLED_BACK", "SKIPPED", "STOPPED"}
    snapshot_age_seconds = max(0.0, time.time() - state_path.stat().st_mtime)
    if "STOPPED" in node_states:
        recovered_status = ExecutionStatus.STOPPED
    elif node_states & {"ROLLED_BACK", "FAILED"}:
        recovered_status = (
            ExecutionStatus.STOPPED
            if snapshot_age_seconds >= ORPHAN_GRACE_SECONDS
            else record.execution_status
        )
    elif nodes and all(node.get("state") in terminal for node in nodes):
        recovered_status = ExecutionStatus.IMPLEMENTED
    elif "AWAITING_APPROVAL" in node_states:
        recovered_status = ExecutionStatus.AWAITING_APPROVAL
    else:
        recovered_status = ExecutionStatus.IN_PROGRESS
    if recovered_status != record.execution_status:
        return repository.sync_execution(record.requirement_id, run_id, recovered_status)
    return record


def _prepare_run(
    requirement_id: str,
    *,
    force: bool = False,
    publish_changes: bool = False,
) -> tuple[Run, RunStore, RequirementsRepository]:
    repository = requirements_repository()
    record = _reconcile_active_registration(repository, repository.get(requirement_id))
    requirement = record.to_requirement_dict()
    run = Run.new(requirement_id=record.requirement_id, scenario=record.requirement_type)
    store = RunStore(run.id, os.getenv("WORKSPACE_DIR", "workspace"))
    repository.mark_run_started(record.requirement_id, run.id, force=force)
    try:
        Planner().plan(
            run,
            requirement,
            store,
            requirements_repository=repository,
            publish_changes=publish_changes,
        )
        store.save_run(run)
        store.write_context("requirement", requirement)
    except Exception:
        try:
            repository.sync_execution(record.requirement_id, run.id, ExecutionStatus.STOPPED)
        except RequirementConflict:
            pass
        raise
    return run, store, repository


def _config() -> dict:
    return {k: v for k, v in os.environ.items()}


def _summarize(run: Run, store: RunStore) -> None:
    table = Table(title=f"Run {run.id}  ·  scenario={run.scenario}")
    table.add_column("Node"); table.add_column("Stage"); table.add_column("State"); table.add_column("Attempts")
    for n in run.nodes.values():
        table.add_row(n.id, n.stage, n.state.value, str(n.attempts))
    console.print(table)
    metrics_path = store.root / "metrics.json"
    if metrics_path.exists():
        console.print(f"[bold]metrics[/bold] → {metrics_path}")
        console.print(metrics_path.read_text())
    if run.is_blocked():
        console.print("[yellow]⏸ Run is blocked on a human approval.[/yellow] "
                      "Approve via dashboard or approval file, then `orchestrator resume`.")


@app.command()
def run(
    req: str = typer.Option(..., help="Database requirement id, for example REQ-002"),
    force: bool = typer.Option(False, "--force", help="Run draft or implemented requirements"),
):
    """Plan and execute a single requirement until completion or an approval gate."""
    try:
        r, store, repository = _prepare_run(req, force=force)
    except (RequirementConflict, RequirementNotFound) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except Exception as exc:
        console.print(f"[red]Planning failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Planned[/green] {len(r.nodes)} nodes for {r.requirement_id}")
    status = Kernel(
        r,
        store,
        _config(),
        requirements_repository=repository,
    ).run_until_blocked()
    console.print(f"[bold]Result:[/bold] {status}")
    _summarize(r, store)


@app.command()
def resume(run_id: str = typer.Option(None, "--run", help="Run id (defaults to latest)")):
    """Resume a blocked run after human decisions have been recorded."""
    run_id = run_id or latest_run(os.getenv("WORKSPACE_DIR", "workspace"))
    if not run_id:
        console.print("[red]No run found.[/red]"); raise typer.Exit(1)
    r, store = rehydrate_run(run_id)
    status = Kernel(
        r,
        store,
        _config(),
        requirements_repository=requirements_repository(),
    ).resume()
    console.print(f"[bold]Result:[/bold] {status}")
    _summarize(r, store)


@app.command()
def replan(run_id: str = typer.Option(..., "--run"), req: str = typer.Option(..., "--req")):
    """Re-plan a run after a requirement change (dynamic, governed re-planning)."""
    try:
        repository = requirements_repository()
        requirement = load_requirement(req, repository)
        r, store = rehydrate_run(run_id)
        if r.requirement_id != requirement["id"]:
            raise RequirementConflict(
                f"Run {run_id} does not belong to requirement {requirement['id']}"
            )
        previous = repository.get(requirement["id"]).execution_status
        repository.mark_replan_started(requirement["id"], run_id)
        try:
            store.write_context("requirement", requirement)
            planner = Planner()
            status = Kernel(
                r,
                store,
                _config(),
                requirements_repository=repository,
            ).replan(requirement["id"], lambda run: planner.replan(run))
        except Exception:
            repository.sync_execution(requirement["id"], run_id, previous)
            raise
    except (RequirementConflict, RequirementNotFound) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[bold]Re-plan result:[/bold] {status}")
    _summarize(r, store)


@app.command("refresh-demo-requirements")
def refresh_demo_requirements_command(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Confirm deletion of verified REQ-004/REQ-005 duplicates",
    ),
):
    """Back up Governance SQLite and replace duplicate expiry demos."""
    if not yes:
        console.print("[red]Catalog refresh requires --yes.[/red]")
        raise typer.Exit(1)
    workspace = Path(os.getenv("WORKSPACE_DIR", "workspace"))
    try:
        result = refresh_demo_requirements(workspace)
    except (DuplicateValidationError, FileNotFoundError, sqlite3.Error) as exc:
        console.print(f"[red]Catalog refresh failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    removed = ", ".join(f"REQ-{item:03d}" for item in result.removed_ids)
    created = ", ".join(f"REQ-{item:03d}" for item in result.created_ids)
    console.print(f"Removed: {removed}")
    console.print(f"Created: {created}")
    console.print(f"Backup: {result.backup_path}")


@app.command()
def demo(
    scenarios: str = typer.Option("greenfield,brownfield,ambiguous"),
    force: bool = typer.Option(False, "--force", help="Required because demo re-runs seeded items"),
):
    """Run the three required scenarios back-to-back."""
    if not force:
        console.print("[red]The database-backed demo requires --force.[/red]")
        raise typer.Exit(1)
    mapping = {
        "greenfield": "REQ-001",
        "brownfield": "REQ-002",
        "ambiguous": "REQ-003",
    }
    for name in [s.strip() for s in scenarios.split(",")]:
        console.rule(f"[bold cyan]Scenario: {name}")
        try:
            r, store, repository = _prepare_run(mapping[name], force=True)
        except Exception as exc:
            console.print(f"[red]Planning failed for {name}:[/red] {exc}")
            raise typer.Exit(1) from exc
        status = Kernel(
            r,
            store,
            _config(),
            requirements_repository=repository,
        ).run_until_blocked()
        console.print(f"[bold]{name} → {status}[/bold]")
        _summarize(r, store)


@app.command()
def status(run_id: str = typer.Option(None, "--run")):
    """Show the current state + metrics of a run."""
    run_id = run_id or latest_run(os.getenv("WORKSPACE_DIR", "workspace"))
    if not run_id:
        console.print("[red]No run found.[/red]"); raise typer.Exit(1)
    r, store = rehydrate_run(run_id)
    _summarize(r, store)


def rehydrate_run(run_id: str) -> tuple[Run, RunStore]:
    """Reconstruct a Run object from its persisted state.json."""
    from .state import Gate, Node, NodeState
    store = RunStore(run_id, os.getenv("WORKSPACE_DIR", "workspace"))
    data = json.loads((store.root / "state.json").read_text())
    r = Run(id=data["id"], requirement_id=data["requirement_id"],
            scenario=data["scenario"], created_at=data["created_at"])
    for nid, nd in data["nodes"].items():
        node = Node(
            id=nd["id"], stage=nd["stage"], agent=nd["agent"], title=nd["title"],
            depends_on=nd["depends_on"], parallel_group=nd.get("parallel_group"),
            impact=nd.get("impact", "low"), attempts=nd.get("attempts", 0),
            outputs=nd.get("outputs", {}), started_at=nd.get("started_at"),
            ended_at=nd.get("ended_at"), state=NodeState(nd["state"]),
        )
        if nd.get("entry_gate"):
            node.entry_gate = Gate(**nd["entry_gate"])
        if nd.get("exit_gate"):
            node.exit_gate = Gate(**nd["exit_gate"])
        r.nodes[nid] = node
    return r, store


def main():  # console-script friendly entry
    app()


if __name__ == "__main__":
    app()
