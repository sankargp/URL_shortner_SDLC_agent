"""Deterministic profile selection and profile-aware agent behavior."""
from __future__ import annotations

import pytest

from agents.architect import ArchitectAgent
from agents.demo_profiles import get_demo_profile, resolve_demo_profile
from agents.planner import Planner
from agents.requirements import RequirementsAgent
from orchestrator.context import RunStore
from orchestrator.state import Node, Run


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        (
            {"type": "brownfield", "title": "Add custom aliases and link expiry"},
            "alias_expiry",
        ),
        (
            {"type": "brownfield", "title": "Password-protected short links"},
            "password_protection",
        ),
        (
            {
                "type": "brownfield",
                "title": "Bulk URL shortening with idempotent retries",
            },
            "bulk_idempotency",
        ),
        (
            {"type": "greenfield", "title": "Build a URL shortener"},
            "core_shortener",
        ),
        (
            {"type": "ambiguous", "title": "Make links more reliable"},
            "ambiguous_reliability",
        ),
    ],
)
def test_supported_requirement_selects_exactly_one_profile(requirement, expected):
    resolution = resolve_demo_profile(requirement, mode="mock")

    assert resolution.error is None
    assert resolution.profile is not None
    assert resolution.profile.name == expected


def test_unknown_mock_requirement_is_unsupported():
    resolution = resolve_demo_profile(
        {"type": "brownfield", "title": "Add social sharing"},
        mode="mock",
    )

    assert resolution.profile is None
    assert resolution.error == "unsupported_demo_profile"


def test_requirement_matching_multiple_profiles_is_rejected():
    resolution = resolve_demo_profile(
        {
            "type": "brownfield",
            "title": "Add password aliases with expiry",
        },
        mode="mock",
    )

    assert resolution.profile is None
    assert resolution.error == "ambiguous_demo_profile"


def test_password_profile_declares_security_impact_and_real_tests():
    profile = get_demo_profile("password_protection")

    assert "security_sensitive" in profile.tags
    assert "password_hash" in str(profile.architecture)
    assert any("test_protected_link" in node_id for node_id in profile.test_node_ids)


def test_bulk_profile_declares_idempotency_capability():
    profile = get_demo_profile("bulk_idempotency")

    assert "idempotent_batch" in profile.capabilities
    assert any("test_batch_retry" in node_id for node_id in profile.test_node_ids)


def test_planner_persists_selected_profile_in_run_context(tmp_path):
    requirement = {
        "id": "REQ-101",
        "type": "brownfield",
        "title": "Password-protected short links",
        "acceptance": ["POST /shorten accepts an optional password"],
    }
    run = Run.new(requirement["id"], requirement["type"])
    store = RunStore(run.id, str(tmp_path))

    Planner().plan(run, requirement, store)

    context = store.read_context()
    assert context["demo_profile"] == "password_protection"
    assert context["demo_profile_error"] is None
    assert "security_sensitive" in run.nodes["arch"].entry_gate.trigger_when


def test_architect_emits_password_specific_impact_analysis(tmp_path):
    run = Run.new("REQ-101", "brownfield")
    node = Node(
        id="arch",
        stage="architecture",
        agent="architect",
        title="Design / impact analysis",
    )
    store = RunStore(run.id, str(tmp_path))

    result = ArchitectAgent().run(
        node=node,
        run=run,
        context={"demo_profile": "password_protection"},
        store=store,
    )

    assert result["exit_ok"] is True
    assert "password_hash" in str(result["context_updates"]["design"])
    assert "security_sensitive" in result["tags"]
    assert "expires_at" not in str(result["context_updates"]["design"])


def test_requirements_agent_safe_stops_unsupported_mock_profile(tmp_path):
    run = Run.new("REQ-102", "brownfield")
    node = Node(
        id="req",
        stage="requirements",
        agent="requirements",
        title="Normalize requirement",
    )
    store = RunStore(run.id, str(tmp_path))
    analysis = {
        "acceptance": ["Share to a social network"],
        "ambiguous": False,
        "ambiguities": [],
        "interpretations": [],
    }

    result = RequirementsAgent().run(
        node=node,
        run=run,
        context={
            "requirement": {
                "id": "REQ-102",
                "type": "brownfield",
                "title": "Add social sharing",
            },
            "requirements_analysis": analysis,
            "demo_profile_error": "unsupported_demo_profile",
        },
        store=store,
    )

    assert result["exit_ok"] is False
    assert result["reason"] == "unsupported_demo_profile"
