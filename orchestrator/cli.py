"""`orchestrator` command: run / resume / replan / demo / status.

This is the primary trigger surface. It loads a requirement, asks the Planner
for a DAG, drives the Kernel until completion or a human-approval checkpoint,
and prints a readable summary + metrics.
"""
from __future__ import annotations

import os
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from .state import Run
from .context import RunStore, latest_run
from .kernel import Kernel
from agents import Planner

app = typer.Typer(add_completion=False, help="Agentic SDLC orchestrator")
console = Console()


def _load_requirement(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    return data


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
def run(req: str = typer.Option(..., help="Path to a REQ-*.yaml requirement file")):
    """Plan and execute a single requirement until completion or an approval gate."""
    requirement = _load_requirement(req)
    r = Run.new(requirement_id=requirement["id"], scenario=requirement.get("type", "greenfield"))
    store = RunStore(r.id, os.getenv("WORKSPACE_DIR", "workspace"))
    Planner().plan(r, requirement)
    store.save_run(r)
    store.write_context("requirement", requirement)
    console.print(f"[green]Planned[/green] {len(r.nodes)} nodes for {requirement['id']}")
    status = Kernel(r, store, _config()).run_until_blocked()
    console.print(f"[bold]Result:[/bold] {status}")
    _summarize(r, store)


@app.command()
def resume(run_id: str = typer.Option(None, "--run", help="Run id (defaults to latest)")):
    """Resume a blocked run after human decisions have been recorded."""
    run_id = run_id or latest_run(os.getenv("WORKSPACE_DIR", "workspace"))
    if not run_id:
        console.print("[red]No run found.[/red]"); raise typer.Exit(1)
    r, store = _rehydrate(run_id)
    status = Kernel(r, store, _config()).resume()
    console.print(f"[bold]Result:[/bold] {status}")
    _summarize(r, store)


@app.command()
def replan(run_id: str = typer.Option(..., "--run"), req: str = typer.Option(..., "--req")):
    """Re-plan a run after a requirement change (dynamic, governed re-planning)."""
    requirement = _load_requirement(req)
    r, store = _rehydrate(run_id)
    store.write_context("requirement", requirement)
    planner = Planner()
    status = Kernel(r, store, _config()).replan(requirement["id"], lambda run: planner.replan(run))
    console.print(f"[bold]Re-plan result:[/bold] {status}")
    _summarize(r, store)


@app.command()
def demo(scenarios: str = typer.Option("greenfield,brownfield,ambiguous")):
    """Run the three required scenarios back-to-back."""
    mapping = {
        "greenfield": "workspace/requirements/REQ-001-greenfield.yaml",
        "brownfield": "workspace/requirements/REQ-002-brownfield.yaml",
        "ambiguous": "workspace/requirements/REQ-003-ambiguous.yaml",
    }
    for name in [s.strip() for s in scenarios.split(",")]:
        console.rule(f"[bold cyan]Scenario: {name}")
        requirement = _load_requirement(mapping[name])
        r = Run.new(requirement["id"], requirement.get("type", name))
        store = RunStore(r.id, os.getenv("WORKSPACE_DIR", "workspace"))
        Planner().plan(r, requirement)
        store.save_run(r); store.write_context("requirement", requirement)
        status = Kernel(r, store, _config()).run_until_blocked()
        console.print(f"[bold]{name} → {status}[/bold]")
        _summarize(r, store)


@app.command()
def status(run_id: str = typer.Option(None, "--run")):
    """Show the current state + metrics of a run."""
    run_id = run_id or latest_run(os.getenv("WORKSPACE_DIR", "workspace"))
    if not run_id:
        console.print("[red]No run found.[/red]"); raise typer.Exit(1)
    r, store = _rehydrate(run_id)
    _summarize(r, store)


def _rehydrate(run_id: str) -> tuple[Run, RunStore]:
    """Reconstruct a Run object from its persisted state.json."""
    import json
    from .state import Node, NodeState, Gate
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
