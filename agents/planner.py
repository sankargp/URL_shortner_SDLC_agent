"""Planner / Decomposer: turns a requirement into an explicit dependency graph.

The DAG demonstrates the required properties:
  * entry/exit gates per stage
  * sequential + parallel paths (unit tests || docs, then join at release)
  * human-approval checkpoints on high-impact nodes
  * scenario-aware decomposition (greenfield / brownfield / ambiguous)
"""
from __future__ import annotations

from orchestrator.requirements_store import (
    AnalysisStatus,
    ExecutionStatus,
    RequirementsRepositoryProtocol,
)
from orchestrator.state import Gate, Node, NodeState, Run

from .demo_profiles import resolve_demo_profile
from .requirements import analyze_requirement


def _n(**kw) -> Node:
    return Node(**kw)


class Planner:
    """Builds (or rebuilds, on re-plan) the node graph for a requirement."""

    def plan(
        self,
        run: Run,
        requirement: dict,
        store=None,
        *,
        requirements_repository: RequirementsRepositoryProtocol | None = None,
        publish_changes: bool = False,
    ) -> None:
        scenario = run.scenario
        nodes: list[Node] = []

        # --- Stage 1: Requirements understanding -------------------------------
        # Real ambiguity detection (LLM-driven, falls back to the requirement's own
        # declared fields) — not just a `type: ambiguous` label — decides the gate.
        # The scenario label still forces the gate too, for explicit demo requests.
        persisted = (
            requirements_repository.get(run.requirement_id)
            if requirements_repository is not None
            else None
        )
        analysis_write_guard = {}
        if persisted is not None:
            if (
                persisted.execution_status == ExecutionStatus.IN_PROGRESS
                and persisted.current_run_id == run.id
            ):
                analysis_write_guard = {"expected_run_id": run.id}
            else:
                analysis_write_guard = {"expected_revision": persisted.revision}
        if (
            persisted is not None
            and persisted.analysis_status == AnalysisStatus.READY
            and persisted.analysis is not None
        ):
            analysis = persisted.analysis
        else:
            try:
                analysis = analyze_requirement(requirement)
            except Exception as exc:
                if requirements_repository is not None:
                    requirements_repository.record_analysis(
                        run.requirement_id,
                        error=str(exc),
                        **analysis_write_guard,
                    )
                raise
            if requirements_repository is not None:
                requirements_repository.record_analysis(
                    run.requirement_id,
                    analysis=analysis,
                    **analysis_write_guard,
                )
        if store is not None:
            store.write_context("requirements_analysis", analysis)
            store.write_context("publish_changes", publish_changes)

        profile_resolution = resolve_demo_profile(requirement)
        selected_profile = profile_resolution.profile
        if store is not None:
            store.write_context(
                "demo_profile",
                selected_profile.name if selected_profile is not None else None,
            )
            store.write_context("demo_profile_error", profile_resolution.error)

        req_gate = None
        impact = "low"
        if analysis["ambiguous"] or scenario == "ambiguous":
            if analysis["interpretations"]:
                options = "\n".join(f"- {i}" for i in analysis["interpretations"])
                prompt = f"Requirement is ambiguous. Choose an interpretation before proceeding:\n{options}"
            else:
                prompt = "Requirement is ambiguous. Choose interpretation before proceeding."
            # Ambiguity is resolved via a human checkpoint before design proceeds.
            req_gate = Gate(kind="human_approval", name="resolve_ambiguity",
                            trigger_when=["ambiguity"], prompt=prompt)
            impact = "high"
        nodes.append(_n(id="req", stage="requirements", agent="requirements",
                        title="Interpret intent & normalize requirement",
                        impact=impact, entry_gate=req_gate))


        # --- Stage 2: Architecture / design -----------------------------------
        arch_depends = ["req"]
        if publish_changes:
            nodes.append(
                _n(
                    id="checkout",
                    stage="source_control",
                    agent="checkout",
                    title="Create isolated repository checkout",
                    depends_on=["req"],
                )
            )
            arch_depends = ["checkout"]
        arch_gate = None
        arch_impact = "low"
        if scenario == "brownfield" and selected_profile is not None:
            # Codebase reasoning: impacted modules must be understood; schema
            # changes are high-impact and require approval.
            arch_gate = Gate(
                kind="human_approval",
                name="approve_impact_analysis",
                trigger_when=list(selected_profile.tags),
                prompt="Approve architecture/impact analysis (may change data or security controls)?",
            )
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

        release_dependencies = ["unit", "docs"]
        if publish_changes:
            nodes.append(
                _n(
                    id="publish",
                    stage="source_control",
                    agent="publisher",
                    title="Commit changes and open pull request",
                    depends_on=["unit", "docs"],
                )
            )
            release_dependencies = ["publish"]

        # --- Stage 5: Release readiness (join point, human sign-off) -----------
        nodes.append(_n(id="release", stage="release", agent="release",
                        title="Release-readiness sign-off",
                        depends_on=release_dependencies, impact="high",
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
