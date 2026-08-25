"""Architect agent: design (greenfield) or impact analysis (brownfield)."""
from __future__ import annotations

from typing import Any
from .base import Agent, llm


class ArchitectAgent(Agent):
    name = "architect"
    system_prompt = "You produce clean designs and, for brownfield work, precise impact analyses."

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        llm("Produce the design / impact analysis.", system=self.system_prompt)
        if run.scenario == "brownfield":
            design = {
                "impacted_modules": ["target-app/main.py", "target-app/models.py"],
                "data_flow_changes": ["add 'alias' + 'expires_at' columns to links table"],
                "api_changes": ["POST /shorten accepts optional custom_alias, expiry_days"],
                "regression_risk": "existing short codes must remain valid",
            }
            tags = ["schema_change"]
            rationale = "Identified impacted modules and a schema change requiring approval."
        else:
            design = {
                "components": ["API layer (FastAPI)", "code generator (base62)",
                               "persistence (SQLite)", "analytics (click events)"],
                "endpoints": ["POST /shorten", "GET /{code}", "GET /{code}/stats"],
            }
            tags = []
            rationale = "Designed a modular, testable service with clear component boundaries."
        art = store.write_artifact("architecture/design.json", str(design))
        return {"artifact": art, "rationale": rationale, "exit_ok": True, "tags": tags,
                "context_updates": {"design": design}}
