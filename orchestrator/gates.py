"""Gate evaluation + human-approval mechanics.

Gates are the governance surface. An entry gate decides whether a node may run;
an exit gate decides whether its output is acceptable. Human-approval gates
persist an approval request and block the path until a decision is recorded.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .context import RunStore
from .state import Node


# Node impact levels that force a human checkpoint. Sourced from REQUIRE_APPROVAL_FOR.
def approval_required(node: Node, require_for: list[str]) -> bool:
    if node.impact == "high":
        return True
    # stage/agent hints map onto the configured autonomy boundary
    hints = {node.stage, node.agent, *node.outputs.get("tags", [])}
    return bool(hints & set(require_for))


def open_approval(store: RunStore, node: Node, question: str, context: dict[str, Any]) -> str:
    """Persist a pending approval request. Returns the approval id."""
    apr_id = f"APR-{len([p for p in store.approvals.glob('APR-*.json')]) + 1:03d}"
    payload = {
        "id": apr_id,
        "node": node.id,
        "stage": node.stage,
        "impact": node.impact,
        "question": question,
        "options": ["approve", "reject", "modify"],
        "context": context,
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "decided_by": None,
        "decided_at": None,
    }
    (store.approvals / f"{apr_id}.json").write_text(json.dumps(payload, indent=2))
    store.audit("approval_opened", node=node.id, approval=apr_id, question=question)
    return apr_id


def pending_approvals(store: RunStore) -> list[dict]:
    out = []
    for p in sorted(store.approvals.glob("APR-*.json")):
        data = json.loads(p.read_text())
        if data.get("status") == "pending":
            out.append(data)
    return out


def decide(store: RunStore, approval_id: str, decision: str, by: str = "human") -> dict:
    """Record a human decision. decision in {approve, reject, modify}."""
    path = store.approvals / f"{approval_id}.json"
    data = json.loads(path.read_text())
    data["status"] = decision
    data["decided_by"] = by
    data["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(data, indent=2))
    store.audit("approval_decided", approval=approval_id, decision=decision, by=by)
    return data


def supersede_pending_approvals(store: RunStore, reason: str) -> None:
    """Close approval questions invalidated by a governed re-plan."""
    for path in sorted(store.approvals.glob("APR-*.json")):
        data = json.loads(path.read_text())
        if data.get("status") != "pending":
            continue
        data["status"] = "superseded"
        data["decided_by"] = "orchestrator"
        data["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data["superseded_reason"] = reason
        path.write_text(json.dumps(data, indent=2))
        store.audit("approval_superseded", approval=data.get("id"), reason=reason)


def approval_for_node(store: RunStore, node_id: str) -> dict | None:
    for p in sorted(store.approvals.glob("APR-*.json"), reverse=True):
        data = json.loads(p.read_text())
        if data.get("node") == node_id:
            return data
    return None
