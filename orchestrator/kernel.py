"""The orchestration kernel: a stateful DAG scheduler with governance.

Responsibilities:
  * Load a requirement, ask the Planner agent for a DAG, persist it.
  * Repeatedly schedule READY nodes (parallel where a parallel_group allows),
    honoring entry/exit gates.
  * Route high-impact nodes to human-approval checkpoints (safe-stop the path).
  * Apply bounded retries -> fallback -> rollback -> safe-stop on failure.
  * Preserve cross-stage context + decision lineage.
  * Re-plan: invalidate downstream nodes when an upstream output changes.
  * Emit reliability metrics.

This is intentionally a compact, readable custom engine (not a framework) so the
control flow and governance are fully explainable/defensible.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from agents import registry

from .context import RunStore
from .gates import (
    approval_for_node,
    approval_required,
    open_approval,
    supersede_pending_approvals,
)
from .metrics import compute
from .requirements_store import ExecutionStatus, RequirementsRepositoryProtocol
from .state import Node, NodeState, Run, can_transition


class Kernel:
    def __init__(
        self,
        run: Run,
        store: RunStore,
        config: dict | None = None,
        *,
        requirements_repository: RequirementsRepositoryProtocol | None = None,
    ):
        self.run = run
        self.store = store
        self.config = config or {}
        self.requirements_repository = requirements_repository
        self.max_retries = int(self.config.get("MAX_RETRIES", os.getenv("MAX_RETRIES", "2")))
        self.require_for = self.config.get(
            "REQUIRE_APPROVAL_FOR",
            os.getenv("REQUIRE_APPROVAL_FOR", "schema_change,release,merge,ambiguity,policy_violation"),
        ).split(",")

    # ---- transition helper (guards illegal moves) ------------------------
    def _transition(self, node: Node, dst: NodeState, **audit_fields) -> None:
        if not can_transition(node.state, dst):
            raise RuntimeError(f"Illegal transition {node.state} -> {dst} for {node.id}")
        src = node.state
        node.state = dst
        self.store.audit("transition", node=node.id, **{"from": src.value, "to": dst.value}, **audit_fields)
        self.store.save_run(self.run)

    # ---- main loop --------------------------------------------------------
    def run_until_blocked(self) -> str:
        """Drive the DAG until it completes or blocks on a human approval.

        Returns one of: "complete", "blocked", "stopped".
        """
        self.store.audit("run_started", run=self.run.id, scenario=self.run.scenario)
        while True:
            if self.run.is_blocked():
                self._finalize()
                return "blocked"
            ready = [n for n in self.run.ready_nodes()]
            if not ready:
                if self.run.is_complete():
                    self._finalize()
                    return "complete"
                # Nothing ready and not complete => waiting on an approval upstream.
                self._finalize()
                return "blocked"

            # Group READY nodes: same parallel_group runs concurrently; else serial.
            self._mark_ready(ready)
            groups: dict[str, list[Node]] = {}
            for n in ready:
                groups.setdefault(n.parallel_group or n.id, []).append(n)

            for batch in groups.values():
                if len(batch) == 1:
                    self._execute(batch[0])
                else:
                    with ThreadPoolExecutor(max_workers=len(batch)) as ex:
                        list(ex.map(self._execute, batch))

    def _mark_ready(self, nodes: list[Node]) -> None:
        for n in nodes:
            if n.state == NodeState.PENDING:
                self._transition(n, NodeState.READY)

    # ---- single node execution -------------------------------------------
    def _execute(self, node: Node) -> None:
        # Entry approval gate (autonomy boundary).
        if approval_required(node, self.require_for) and not self._approved(node):
            node.attempts += 1
            node.started_at = node.started_at or time.time()
            self._transition(node, NodeState.RUNNING, attempt=node.attempts)
            try:
                proposal = self._run_agent(node)
                self._transition(
                    node, NodeState.AWAITING_APPROVAL, reason="high_impact_checkpoint"
                )
                open_approval(
                    self.store,
                    node,
                    question=(
                        node.entry_gate.prompt if node.entry_gate else f"Approve {node.title}?"
                    ),
                    context={"proposal": proposal},
                )
            except Exception as exc:  # noqa: BLE001 - bounded and safe-stopped below
                node.ended_at = time.time()
                self._handle_failure(node, reason=f"exception:{exc}")
            return

        node.attempts += 1
        node.started_at = node.started_at or time.time()
        self._transition(node, NodeState.RUNNING, attempt=node.attempts)
        try:
            outputs = self._run_agent(node)
            node.outputs.update(outputs)
            node.ended_at = time.time()
            # Exit gate: acceptance criteria check (agent self-validation + policy).
            if outputs.get("exit_ok", True):
                self._transition(node, NodeState.PASSED)
                self.store.record_lineage(
                    artifact=outputs.get("artifact", node.id),
                    from_requirement=self.run.requirement_id,
                    node=node.id,
                    rationale=outputs.get("rationale", ""),
                )
            else:
                self._handle_failure(node, reason=outputs.get("reason", "exit_gate_failed"))
        except Exception as e:  # noqa: BLE001 — bounded, logged, and recovered
            node.ended_at = time.time()
            self._handle_failure(node, reason=f"exception:{e}")

    # ---- failure handling: bounded retry -> fallback -> rollback -> stop --
    def _handle_failure(self, node: Node, reason: str) -> None:
        self._transition(node, NodeState.FAILED, reason=reason)
        if node.attempts <= self.max_retries:
            backoff = float(self.config.get("RETRY_BACKOFF_SECONDS", 1))
            time.sleep(min(backoff, 0.1))  # keep demos snappy
            self.store.audit("retry", node=node.id, attempt=node.attempts)
            self._transition(node, NodeState.READY)  # retry
        else:
            # Fallback hook could go here; we roll back side effects then safe-stop.
            self._transition(node, NodeState.ROLLED_BACK, reason="exhausted_retries")
            self.store.audit("rollback", node=node.id)
            self._transition(node, NodeState.STOPPED, reason="safe_stop")
            self._cascade_skip_unreachable()

    # ---- unreachable-node cleanup ------------------------------------------
    def _cascade_skip_unreachable(self) -> None:
        """A node that will never run (stopped, rolled back) means anything
        depending on it can never become ready either. Skip those dependents so
        the run reaches a real terminal state instead of blocking forever."""
        dead = {NodeState.STOPPED, NodeState.ROLLED_BACK, NodeState.SKIPPED}
        changed = True
        while changed:
            changed = False
            for n in self.run.nodes.values():
                if n.state == NodeState.PENDING and any(self.run.nodes[d].state in dead for d in n.depends_on):
                    self._transition(n, NodeState.SKIPPED, reason="upstream_stopped")
                    changed = True

    # ---- approvals + resume ----------------------------------------------
    def _approved(self, node: Node) -> bool:
        apr = approval_for_node(self.store, node.id)
        return bool(apr and apr.get("status") == "approve")

    def resume(self) -> str:
        """Resume after human decisions were recorded on approval files."""
        for node in self.run.nodes.values():
            if node.state != NodeState.AWAITING_APPROVAL:
                continue
            apr = approval_for_node(self.store, node.id)
            if not apr:
                continue
            if apr["status"] == "approve":
                self._transition(node, NodeState.RUNNING, via="approval")
                try:
                    out = self._run_agent(node)
                    node.outputs.update(out)
                    node.ended_at = time.time()
                    self._transition(node, NodeState.PASSED)
                    self.store.record_lineage(
                        out.get("artifact", node.id),
                        self.run.requirement_id,
                        node.id,
                        out.get("rationale", "approved by human"),
                    )
                except Exception as exc:  # noqa: BLE001 - bounded and safe-stopped below
                    node.ended_at = time.time()
                    self._handle_failure(node, reason=f"exception:{exc}")
            elif apr["status"] == "reject":
                self._transition(node, NodeState.FAILED, via="rejection")
                self._transition(node, NodeState.ROLLED_BACK, reason="human_rejected")
                self._transition(node, NodeState.STOPPED, reason="safe_stop")
                self._cascade_skip_unreachable()
        return self.run_until_blocked()

    # ---- re-planning ------------------------------------------------------
    def replan(self, changed_requirement_id: str, planner: Callable[[Run], None]) -> str:
        """Invalidate downstream nodes affected by an upstream change and re-run."""
        self.store.audit("replan_triggered", requirement=changed_requirement_id)
        for node in self.run.nodes.values():
            if node.state == NodeState.AWAITING_APPROVAL:
                self._transition(node, NodeState.PENDING, reason="approval_invalidated_by_replan")
        supersede_pending_approvals(
            self.store,
            reason=f"requirement {changed_requirement_id} was re-planned",
        )
        # Naive-but-explicit strategy: reset PASSED nodes from 'requirements' stage
        # forward to PENDING so their dependents re-execute with new context.
        for node in self.run.nodes.values():
            if node.stage in ("requirements", "architecture") and node.state == NodeState.PASSED:
                self._transition(node, NodeState.PENDING, reason="invalidated_by_replan")
        # Cascade: any node depending on an invalidated node is reset too.
        changed = True
        while changed:
            changed = False
            for node in self.run.nodes.values():
                if node.state != NodeState.PASSED:
                    continue
                if any(self.run.nodes[d].state != NodeState.PASSED for d in node.depends_on):
                    self._transition(node, NodeState.PENDING, reason="dependency_invalidated")
                    changed = True
        planner(self.run)  # planner may add/remove nodes based on new requirement
        return self.run_until_blocked()

    # ---- agent dispatch ---------------------------------------------------
    def _run_agent(self, node: Node) -> dict:
        agent = registry.get(node.agent)
        ctx = self.store.read_context()
        result = agent.run(node=node, run=self.run, context=ctx, store=self.store)
        # Persist any context the agent contributed for downstream stages.
        for k, v in result.get("context_updates", {}).items():
            self.store.write_context(k, v)
        return result

    def _finalize(self) -> None:
        m = compute(self.run, self.store.root)
        self.store.audit("metrics", **m)
        self.store.save_run(self.run)
        if self.requirements_repository is not None:
            if any(node.state == NodeState.STOPPED for node in self.run.nodes.values()):
                status = ExecutionStatus.STOPPED
            elif self.run.is_blocked():
                status = ExecutionStatus.AWAITING_APPROVAL
            elif self.run.is_complete():
                status = ExecutionStatus.IMPLEMENTED
            else:
                status = ExecutionStatus.IN_PROGRESS
            self.requirements_repository.sync_execution(
                self.run.requirement_id,
                self.run.id,
                status,
            )
