"""Run persistence: the blackboard (shared context), decision lineage, and the
append-only audit log. Everything is written to workspace/runs/<run-id>/ as
human-readable JSON so the whole execution is inspectable and diff-able.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .state import Run, Node


class RunStore:
    """Owns the on-disk representation of a single run."""

    def __init__(self, run_id: str, workspace_dir: str = "workspace"):
        self.run_id = run_id
        self.root = Path(workspace_dir) / "runs" / run_id
        self.artifacts = self.root / "artifacts"
        self.approvals = self.root / "approvals"
        for p in (self.artifacts, self.approvals):
            p.mkdir(parents=True, exist_ok=True)

    # ---- audit log (append-only) -----------------------------------------
    def audit(self, event: str, **fields: Any) -> None:
        rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(self.root / "audit.log", "a") as f:
            f.write(json.dumps(rec) + "\n")

    # ---- blackboard / shared context -------------------------------------
    def write_context(self, key: str, value: Any) -> None:
        ctx = self.read_context()
        ctx[key] = value
        self._dump("context.json", ctx)

    def read_context(self) -> dict[str, Any]:
        return self._load("context.json", {})

    # ---- decision lineage -------------------------------------------------
    def record_lineage(self, artifact: str, from_requirement: str, node: str, rationale: str) -> None:
        lin = self._load("lineage.json", [])
        lin.append({
            "artifact": artifact,
            "from_requirement": from_requirement,
            "produced_by_node": node,
            "rationale": rationale,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self._dump("lineage.json", lin)

    # ---- run state --------------------------------------------------------
    def save_run(self, run: Run) -> None:
        data = {
            "id": run.id,
            "requirement_id": run.requirement_id,
            "scenario": run.scenario,
            "created_at": run.created_at,
            "nodes": {nid: n.to_dict() for nid, n in run.nodes.items()},
        }
        self._dump("state.json", data)

    # ---- artifacts --------------------------------------------------------
    def write_artifact(self, name: str, content: str) -> str:
        path = self.artifacts / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path.relative_to(self.root))

    # ---- helpers ----------------------------------------------------------
    def _dump(self, name: str, obj: Any) -> None:
        (self.root / name).write_text(json.dumps(obj, indent=2))

    def _load(self, name: str, default: Any) -> Any:
        p = self.root / name
        if p.exists():
            return json.loads(p.read_text())
        return default


def latest_run(workspace_dir: str = "workspace") -> str | None:
    runs_dir = Path(workspace_dir) / "runs"
    if not runs_dir.exists():
        return None
    runs = [d for d in runs_dir.iterdir() if d.is_dir() and (d / "state.json").exists()]
    if not runs:
        return None
    return max(runs, key=lambda d: os.path.getmtime(d)).name
