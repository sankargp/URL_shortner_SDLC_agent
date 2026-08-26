"""Core state model: node states, transitions, and the run/node data structures.

The state machine is the heart of the "non-linear, stateful execution with
governance" requirement. Every transition is explicit and auditable.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class NodeState(str, Enum):
    PENDING = "PENDING"                    # created, dependencies not yet satisfied
    READY = "READY"                        # dependencies satisfied, entry gate open
    RUNNING = "RUNNING"                    # agent executing
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # blocked on a human checkpoint
    PASSED = "PASSED"                      # exit gate satisfied
    FAILED = "FAILED"                      # execution or exit gate failed
    ROLLED_BACK = "ROLLED_BACK"            # side effects reverted
    SKIPPED = "SKIPPED"                    # pruned by re-planning
    STOPPED = "STOPPED"                    # safe-stop triggered


# Legal transitions. The kernel refuses any transition not listed here — this is
# what prevents "simple linear chaining" from silently corrupting run state.
LEGAL_TRANSITIONS: dict[NodeState, set[NodeState]] = {
    NodeState.PENDING: {NodeState.READY, NodeState.SKIPPED, NodeState.STOPPED},
    NodeState.READY: {NodeState.RUNNING, NodeState.SKIPPED, NodeState.STOPPED},
    NodeState.RUNNING: {
        NodeState.AWAITING_APPROVAL, NodeState.PASSED, NodeState.FAILED, NodeState.STOPPED,
    },
    NodeState.AWAITING_APPROVAL: {
        NodeState.PENDING,
        NodeState.RUNNING,
        NodeState.PASSED,
        NodeState.FAILED,
        NodeState.STOPPED,
    },
    NodeState.FAILED: {NodeState.READY, NodeState.ROLLED_BACK, NodeState.STOPPED},  # READY = retry
    NodeState.PASSED: {NodeState.PENDING, NodeState.SKIPPED},  # PENDING = invalidated by re-plan
    NodeState.ROLLED_BACK: {NodeState.PENDING, NodeState.STOPPED},
    NodeState.SKIPPED: {NodeState.PENDING},
    NodeState.STOPPED: set(),
}


def can_transition(src: NodeState, dst: NodeState) -> bool:
    return dst in LEGAL_TRANSITIONS.get(src, set())


@dataclass
class Gate:
    """Entry or exit condition for a node. Human gates block until a decision."""
    kind: str                      # "auto" | "human_approval" | "policy"
    name: str
    trigger_when: list[str] = field(default_factory=list)  # e.g. ["impact:high"]
    prompt: str = ""


@dataclass
class Node:
    id: str
    stage: str                     # requirements | architecture | implementation | testing | docs | release
    agent: str                     # which specialist agent runs this node
    title: str
    depends_on: list[str] = field(default_factory=list)
    parallel_group: str | None = None       # nodes sharing a group may run concurrently
    entry_gate: Gate | None = None
    exit_gate: Gate | None = None
    impact: str = "low"            # low | medium | high  (drives approval routing)
    state: NodeState = NodeState.PENDING
    attempts: int = 0
    outputs: dict[str, Any] = field(default_factory=dict)  # artifact refs, rationale
    started_at: float | None = None
    ended_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class Run:
    id: str
    requirement_id: str
    scenario: str                  # greenfield | brownfield | ambiguous
    nodes: dict[str, Node] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def new(requirement_id: str, scenario: str) -> Run:
        rid = f"run-{time.strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:6]}"
        return Run(id=rid, requirement_id=requirement_id, scenario=scenario)

    def ready_nodes(self) -> list[Node]:
        """Nodes whose dependencies are all PASSED and are themselves READY/PENDING."""
        out = []
        for n in self.nodes.values():
            if n.state not in (NodeState.PENDING, NodeState.READY):
                continue
            if all(self.nodes[d].state == NodeState.PASSED for d in n.depends_on):
                out.append(n)
        return out

    def is_complete(self) -> bool:
        terminal = {NodeState.PASSED, NodeState.SKIPPED, NodeState.STOPPED, NodeState.ROLLED_BACK}
        return all(n.state in terminal for n in self.nodes.values())

    def is_blocked(self) -> bool:
        return any(n.state == NodeState.AWAITING_APPROVAL for n in self.nodes.values())
