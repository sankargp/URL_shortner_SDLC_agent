"""Reliability metrics derived from the audit log + run state.

Tracks the metrics required by the brief: success rate, retry/rollback
frequency, MTTR, and end-to-end latency. Written to metrics.json per run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import NodeState, Run


def compute(run: Run, run_root: Path) -> dict[str, Any]:
    nodes = list(run.nodes.values())
    total = len(nodes)
    passed = sum(1 for n in nodes if n.state == NodeState.PASSED)
    failed = sum(1 for n in nodes if n.state == NodeState.FAILED)
    rolled = sum(1 for n in nodes if n.state == NodeState.ROLLED_BACK)
    retries = sum(max(0, n.attempts - 1) for n in nodes)

    # MTTR: mean time a node spent between first failure and eventual pass.
    # Approximated here from node started/ended spans of nodes that retried.
    recover_spans = [
        (n.ended_at - n.started_at)
        for n in nodes
        if n.attempts > 1 and n.started_at and n.ended_at
    ]
    mttr = round(sum(recover_spans) / len(recover_spans), 3) if recover_spans else 0.0

    starts = [n.started_at for n in nodes if n.started_at]
    ends = [n.ended_at for n in nodes if n.ended_at]
    e2e_latency = round(max(ends) - min(starts), 3) if starts and ends else 0.0

    m = {
        "total_nodes": total,
        "success_rate": round(passed / total, 3) if total else 0.0,
        "passed": passed,
        "failed": failed,
        "retries": retries,
        "retry_frequency": round(retries / total, 3) if total else 0.0,
        "rollbacks": rolled,
        "rollback_frequency": round(rolled / total, 3) if total else 0.0,
        "mttr_seconds": mttr,
        "end_to_end_latency_seconds": e2e_latency,
    }
    (run_root / "metrics.json").write_text(json.dumps(m, indent=2))
    return m
