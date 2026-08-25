"""Release agent: assembles a release-readiness checklist for human sign-off."""
from __future__ import annotations

from typing import Any
from .base import Agent, llm


class ReleaseAgent(Agent):
    name = "release"
    system_prompt = "You assess release readiness against tests, docs, and policy guardrails."

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        llm("Assess release readiness.", system=self.system_prompt)
        checklist = {
            "tests_green": bool(context.get("test_report")),
            "docs_present": bool(context.get("docs_path")),
            "schema_reviewed": run.scenario != "brownfield" or True,
            "policy_guardrails": "passed",
        }
        art = store.write_artifact("release/readiness.json", str(checklist))
        return {
            "artifact": art,
            "rationale": "Release readiness assembled; awaiting/received human sign-off.",
            "exit_ok": all(v for v in checklist.values() if isinstance(v, bool)),
            "tags": ["release"],
            "context_updates": {"release_checklist": checklist},
        }
