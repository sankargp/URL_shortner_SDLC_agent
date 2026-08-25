"""Planner / Decomposer: turns a requirement into an explicit dependency graph.

The DAG demonstrates the required properties:
  * entry/exit gates per stage
  * sequential + parallel paths (unit tests || docs, then join at release)
  * human-approval checkpoints on high-impact nodes
  * scenario-aware decomposition (greenfield / brownfield / ambiguous)
"""
from __future__ import annotations

from orchestrator.state import Run, Node, Gate, NodeState


def _n(**kw) -> Node:
    return Node(**kw)


class Planner:
    """Builds (or rebuilds, on re-plan) the node graph for a requirement."""

    def plan(self, run: Run, requirement: dict) -> None:
        scenario = run.scenario
        nodes: list[Node] = []

        # --- Stage 1: Requirements understanding -------------------------------
        req_gate = None
        impact = "low"
        if scenario == "ambiguous":
            # Ambiguity is resolved via a human checkpoint before design proceeds.
            req_gate = Gate(kind="human_approval", name="resolve_ambiguity",
                            trigger_when=["ambiguity"],
                            prompt="Requirement is ambiguous. Choose interpretation before proceeding.")
            impact = "high"
        nodes.append(_n(id="req", stage="requirements", agent="requirements",
                        title="Interpret intent & normalize requirement",
                        impact=impact, entry_gate=req_gate))

        # --- Stage 2: Architecture / design -----------------------------------
        arch_depends = ["req"]
        arch_gate = None
        arch_impact = "low"
        if scenario == "brownfield":
            # Codebase reasoning: impacted modules must be understood; schema
            # changes are high-impact and require approval.
            arch_gate = Gate(kind="human_approval", name="approve_impact_analysis",
                             trigger_when=["schema_change"],
                             prompt="Approve architecture/impact analysis (may change data schema)?")
            arch_impact = "high"
        nodes.append(_n(id="arch", stage="architecture", agent="architect",
                        title="Design / impact analysis", depends_on=arch_depends,
                        impact=arch_impact, entry_gate=arch_gate))

        # --- Stage 3: Implementation ------------------------------------------
        nodes.append(_n(id="impl", stage="implementation", agent="implementer",
                        title="Produce production-quality code + API/schema",
                        depends_on=["arch"]))

        # --- Stage 4: Testing || Docs (parallel, then join) -------------------
        nodes.append(_n(id="unit", stage="testing", agent="tester",
                        title="Unit + integration tests", depends_on=["impl"],
                        parallel_group="verify"))
        nodes.append(_n(id="docs", stage="docs", agent="docs",
                        title="API docs + engineering notes", depends_on=["impl"],
                        parallel_group="verify"))

        # --- Stage 5: Release readiness (join point, human sign-off) -----------
        nodes.append(_n(id="release", stage="release", agent="release",
                        title="Release-readiness sign-off",
                        depends_on=["unit", "docs"], impact="high",
                        entry_gate=Gate(kind="human_approval", name="release_signoff",
                                        trigger_when=["release"],
                                        prompt="All gates green. Approve release readiness?")))

        run.nodes = {n.id: n for n in nodes}

    def replan(self, run: Run) -> None:
        """On re-plan we keep the graph shape but ensure invalidated nodes are
        schedulable again. New requirements could add nodes here; for the demo we
        preserve topology and let the kernel re-run reset nodes."""
        for node in run.nodes.values():
            if node.state == NodeState.PENDING:
                # already reset by kernel; nothing structural to change in this demo
                continue
