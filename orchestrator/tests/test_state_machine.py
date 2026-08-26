"""Unit tests for the state machine + kernel governance behavior."""
import os

from agents import Planner
from orchestrator.context import RunStore
from orchestrator.kernel import Kernel
from orchestrator.state import NodeState, Run, can_transition

os.environ.setdefault("LLM_MODE", "mock")


def test_legal_transitions():
    assert can_transition(NodeState.PENDING, NodeState.READY)
    assert can_transition(NodeState.RUNNING, NodeState.AWAITING_APPROVAL)
    assert can_transition(NodeState.AWAITING_APPROVAL, NodeState.PENDING)
    assert can_transition(NodeState.FAILED, NodeState.READY)          # retry
    assert not can_transition(NodeState.PASSED, NodeState.RUNNING)    # illegal
    assert not can_transition(NodeState.STOPPED, NodeState.READY)


def test_greenfield_blocks_on_release_gate(tmp_path):
    req = {"id": "REQ-T1", "type": "greenfield"}
    r = Run.new(req["id"], "greenfield")
    store = RunStore(r.id, str(tmp_path))
    Planner().plan(r, req)
    store.save_run(r)
    status = Kernel(r, store, dict(os.environ)).run_until_blocked()
    # Release node is high-impact -> run should block awaiting human sign-off.
    assert status == "blocked"
    assert r.nodes["release"].state == NodeState.AWAITING_APPROVAL
    # Upstream stages should have passed.
    assert r.nodes["req"].state == NodeState.PASSED
    assert r.nodes["impl"].state == NodeState.PASSED


def test_parallel_group_present():
    req = {"id": "REQ-T2", "type": "greenfield"}
    r = Run.new(req["id"], "greenfield")
    Planner().plan(r, req)
    assert r.nodes["unit"].parallel_group == r.nodes["docs"].parallel_group == "verify"


def test_ambiguous_blocks_early(tmp_path):
    req = {"id": "REQ-T3", "type": "ambiguous"}
    r = Run.new(req["id"], "ambiguous")
    store = RunStore(r.id, str(tmp_path))
    Planner().plan(r, req)
    store.save_run(r)
    status = Kernel(r, store, dict(os.environ)).run_until_blocked()
    assert status == "blocked"
    # The requirements node itself is the high-impact ambiguity checkpoint.
    assert r.nodes["req"].state == NodeState.AWAITING_APPROVAL
