"""Requirements agent: interpret intent, flag ambiguity, normalize."""
from __future__ import annotations

from typing import Any
from .base import Agent, llm


class RequirementsAgent(Agent):
    name = "requirements"
    system_prompt = "You normalize product requirements into clear engineering problems and flag ambiguity."

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        analysis = llm("Normalize the requirement and list acceptance criteria.",
                       system=self.system_prompt)
        normalized = {
            "problem": f"[{run.scenario}] URL shortener requirement normalized",
            "acceptance": [
                "POST /shorten returns a unique short code",
                "GET /{code} redirects to the original URL",
                "Invalid URLs are rejected with 400",
            ],
            "ambiguities": (["'more reliable' is under-specified: rate limiting? caching? retries? SLOs?"]
                            if run.scenario == "ambiguous" else []),
        }
        art = store.write_artifact("requirements/normalized.json", str(normalized))
        return {
            "artifact": art,
            "rationale": "Interpreted intent and captured explicit acceptance criteria.",
            "exit_ok": True,
            "tags": (["ambiguity"] if run.scenario == "ambiguous" else []),
            "context_updates": {"acceptance": normalized["acceptance"],
                                "ambiguities": normalized["ambiguities"]},
        }
