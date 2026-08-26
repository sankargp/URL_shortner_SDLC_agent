"""Architect agent: design (greenfield) or impact analysis (brownfield)."""
from __future__ import annotations

import json
from typing import Any

from .base import Agent, llm
from .demo_profiles import get_demo_profile


class ArchitectAgent(Agent):
    name = "architect"
    system_prompt = "You produce clean designs and, for brownfield work, precise impact analyses."

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        llm("Produce the design / impact analysis.", system=self.system_prompt)
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
