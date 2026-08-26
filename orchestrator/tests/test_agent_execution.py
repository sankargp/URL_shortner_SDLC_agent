"""Tests for truthful implementation and pytest-backed testing agents."""
from __future__ import annotations

import json
import os

import agents.implementer as implementer_module
from agents.implementer import ImplementerAgent, materialize_validated_source
from agents.tester import run_pytest_nodes
from orchestrator.context import RunStore
from orchestrator.state import Node, Run


def _node(agent: str, stage: str) -> Node:
    return Node(
        id=agent,
        stage=stage,
        agent=agent,
        title=f"Run {agent}",
    )


def test_invalid_source_does_not_replace_existing_application(tmp_path):
    destination = tmp_path / "main.py"
    destination.write_text("original = True\n")

    result = materialize_validated_source("def broken(:\n", destination)

    assert result.ok is False
    assert result.error == "source_syntax_error"
    assert destination.read_text() == "original = True\n"


def test_mock_implementer_materializes_template_and_writes_provenance(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LLM_MODE", "mock")
    template = tmp_path / "template.py"
    destination = tmp_path / "target-app" / "main.py"
    template.write_text("def generated():\n    return 'working'\n")
    destination.parent.mkdir(parents=True)
    destination.write_text("old = True\n")
    run = Run.new("REQ-101", "brownfield")
    store = RunStore(run.id, str(tmp_path / "workspace"))

    result = ImplementerAgent(
        app_path=destination,
        template_path=template,
    ).run(
        node=_node("implementer", "implementation"),
        run=run,
        context={"demo_profile": "password_protection"},
        store=store,
    )

    assert result["exit_ok"] is True
    assert destination.read_text() == template.read_text()
    artifact = json.loads((store.root / result["artifact"]).read_text())
    assert artifact["profile"] == "password_protection"
    assert artifact["mode"] == "mock"
    assert artifact["sha256"]
    assert "password_protection" in artifact["capabilities"]


def test_invalid_live_output_preserves_existing_application(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setattr(implementer_module, "llm", lambda *args, **kwargs: "def broken(:")
    destination = tmp_path / "target-app" / "main.py"
    template = tmp_path / "template.py"
    destination.parent.mkdir(parents=True)
    destination.write_text("original = True\n")
    template.write_text("template = True\n")
    run = Run.new("REQ-101", "brownfield")
    store = RunStore(run.id, str(tmp_path / "workspace"))

    result = ImplementerAgent(
        app_path=destination,
        template_path=template,
    ).run(
        node=_node("implementer", "implementation"),
        run=run,
        context={"demo_profile": "password_protection", "design": {}},
        store=store,
    )

    assert result["exit_ok"] is False
    assert result["reason"] == "source_syntax_error"
    assert destination.read_text() == "original = True\n"


def test_pytest_runner_reports_real_pass_and_failure_counts(tmp_path):
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        "def test_passes():\n"
        "    assert 2 + 2 == 4\n\n"
        "def test_fails():\n"
        "    assert False\n"
    )

    passing = run_pytest_nodes(
        (f"{test_file}::test_passes",),
        env=dict(os.environ),
        cwd=tmp_path,
    )
    failing = run_pytest_nodes(
        (f"{test_file}::test_fails",),
        env=dict(os.environ),
        cwd=tmp_path,
    )

    assert passing.return_code == 0
    assert passing.passed == 1
    assert passing.failed == 0
    assert failing.return_code != 0
    assert failing.passed == 0
    assert failing.failed == 1
