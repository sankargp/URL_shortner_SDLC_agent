"""Architect agent: design (greenfield) or impact analysis (brownfield)."""
from __future__ import annotations

import json
import os
from typing import Any

from .base import Agent, llm
from .demo_profiles import get_demo_profile

SYSTEM_PROMPT = "You produce clean designs and, for brownfield work, precise impact analyses."


def _build_prompt(run, context: dict[str, Any]) -> str:
    requirement = context.get("requirement") or {"id": run.requirement_id, "type": run.scenario}
    return (
        f"Requirement ({run.scenario}):\n{json.dumps(requirement, indent=2)}\n\n"
        f"Acceptance criteria:\n{json.dumps(context.get('acceptance', []), indent=2)}\n\n"
        "Produce a design / impact analysis for this change to the target FastAPI URL-shortener "
        "service. Respond with STRICT JSON ONLY (no prose, no markdown fences) using this shape:\n"
        '{"impacted_modules": ["..."], "api_changes": ["..."], "schema_changes": ["..."], '
        '"regression_risks": ["..."], "tags": ["schema_change or security_sensitive, as applicable"]}\n'
        'Omit a key rather than inventing a value with nothing to report. Only include "schema_change" '
        'in tags when the change requires a persisted schema change, and "security_sensitive" only '
        "when it touches authentication, authorization, or secrets."
    )


class ArchitectAgent(Agent):
    name = "architect"
    system_prompt = SYSTEM_PROMPT

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        mode = os.getenv("LLM_MODE", "mock").casefold()
        if mode == "live":
            response = llm(_build_prompt(run, context), system=self.system_prompt, max_tokens=2048)
            try:
                design = json.loads(response)
            except json.JSONDecodeError:
                return {
                    "rationale": "The model did not return a valid design.",
                    "exit_ok": False,
                    "reason": "live_design_unparseable",
                    "tags": [],
                    "context_updates": {},
                }
            tags = list(design.pop("tags", None) or [])
            rationale = "Produced a live impact analysis from the model's design."
        else:
            profile_name = context.get("demo_profile")
            if not profile_name:
                return {
                    "rationale": "No deterministic profile was available for architecture.",
                    "exit_ok": False,
                    "reason": context.get("demo_profile_error") or "dynamic_design_unavailable",
                    "tags": [],
                    "context_updates": {},
                }

            profile = get_demo_profile(profile_name)
            design = dict(profile.architecture)
            tags = list(profile.tags)
            rationale = f"Produced impact analysis for the {profile.name} profile."
        art = store.write_artifact(
            "architecture/design.json",
            json.dumps(design, indent=2),
        )
        return {"artifact": art, "rationale": rationale, "exit_ok": True, "tags": tags,
                "context_updates": {"design": design}}
