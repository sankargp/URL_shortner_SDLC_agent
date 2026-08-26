"""Contracts for the repository-aware publishing DAG."""
from __future__ import annotations

from types import SimpleNamespace

from agents.implementer import ImplementerAgent
from agents.planner import Planner
from agents.release import ReleaseAgent
from orchestrator.context import RunStore
from orchestrator.state import Run


def test_planner_places_publication_before_release_when_requested(tmp_path, monkeypatch):
    run = Run.new("REQ-21", "greenfield")
    store = RunStore(run.id, str(tmp_path / "workspace"))
    requirement = {
        "id": "REQ-21",
        "type": "greenfield",
        "title": "Publish change",
        "intent": "Open a pull request",
        "acceptance": [],
        "constraints": [],
        "possible_interpretations": [],
    }
    monkeypatch.setattr(
        "agents.planner.analyze_requirement",
        lambda value: {
            "acceptance": [],
            "ambiguous": False,
            "ambiguities": [],
            "interpretations": [],
        },
    )

    Planner().plan(run, requirement, store, publish_changes=True)

    assert run.nodes["checkout"].depends_on == ["req"]
    assert run.nodes["arch"].depends_on == ["checkout"]
    assert run.nodes["publish"].depends_on == ["unit", "docs"]
    assert run.nodes["release"].depends_on == ["publish"]
    assert store.read_context()["publish_changes"] is True


class _ArtifactStore:
    def write_artifact(self, name: str, content: str) -> str:
        return name


def test_implementer_writes_only_to_the_checked_out_repository(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    app_path = repository / "target-app" / "main.py"
    app_path.parent.mkdir(parents=True)
    app_path.write_text("def existing():\n    return 1\n")
    outside = tmp_path / "target-app" / "main.py"
    outside.parent.mkdir()
    outside.write_text("def outside():\n    return 1\n")
    generated = "def changed():\n    return 2\n"
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setattr("agents.implementer.llm", lambda *args, **kwargs: generated)

    result = ImplementerAgent().run(
        node=SimpleNamespace(),
        run=SimpleNamespace(scenario="greenfield"),
        context={
            "design": {},
            "demo_profile": "core_shortener",
            "repository": {"path": str(repository)},
        },
        store=_ArtifactStore(),
    )

    assert app_path.read_text() == generated
    assert outside.read_text() == "def outside():\n    return 1\n"
    assert result["exit_ok"] is True
    assert result["context_updates"]["changed_files"] == ["target-app/main.py"]


def test_release_accepts_an_explicit_no_change_publication(monkeypatch):
    monkeypatch.setattr("agents.release.llm", lambda *args, **kwargs: "ok")

    result = ReleaseAgent().run(
        node=SimpleNamespace(),
        run=SimpleNamespace(scenario="greenfield"),
        context={
            "publish_changes": True,
            "publication": {"outcome": "no_changes"},
            "test_report": {"unit": {"passed": 1, "total": 1}},
            "docs_path": "artifacts/docs/API.md",
        },
        store=_ArtifactStore(),
    )

    assert result["exit_ok"] is True
    assert result["context_updates"]["release_checklist"]["publication_ready"] is True
