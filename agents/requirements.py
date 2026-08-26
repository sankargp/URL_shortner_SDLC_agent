"""Requirements agent: interpret intent, flag ambiguity, normalize."""
from __future__ import annotations

import json
import os
from typing import Any

from .base import Agent, llm
from .demo_profiles import resolve_demo_profile

SYSTEM_PROMPT = (
    "You are a senior requirements analyst. Given a raw product requirement, respond "
    "with STRICT JSON ONLY (no prose, no markdown fences) using this shape:\n"
    '{"acceptance": ["..."], "ambiguous": true|false, '
    '"ambiguities": ["what is unclear, and why"], '
    '"interpretations": ["distinct, concrete ways this could be implemented"]}\n'
    "Mark ambiguous=true only when the intent is under-specified enough that two "
    "competent engineers could reasonably build different things from it."
)


def analyze_requirement(requirement: dict) -> dict[str, Any]:
    """Real ambiguity/acceptance analysis via the LLM. Shared by the Planner (to
    decide whether to open a human-approval gate, and what to ask) and the
    RequirementsAgent (to build the normalized artifact), so both agree on one
    verdict instead of the Planner guessing from a scenario label.

    In mock/replay mode this is a deterministic, offline analysis of the
    requirement's own declared fields (by design, for reliable demos/tests — not
    an error fallback). In live mode, the model's JSON response is required: a
    missing key, an unreachable provider, or a malformed reply raises rather than
    silently substituting a guess."""
    if os.getenv("LLM_MODE", "mock").lower() != "live":
        declared_interpretations = requirement.get("possible_interpretations") or []
        return {
            "acceptance": list(requirement.get("acceptance") or []),
            "ambiguous": bool(declared_interpretations),
            "ambiguities": (["Requirement intent is under-specified; see possible_interpretations."]
                            if declared_interpretations else []),
            "interpretations": declared_interpretations,
        }

    prompt = (f"Requirement:\n{json.dumps(requirement, indent=2)}\n\n"
              "Analyze it and reply with the JSON object described in your instructions.")
    raw = llm(prompt, system=SYSTEM_PROMPT)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"requirements analysis: model did not return valid JSON: {raw!r}") from exc
    ambiguous = bool(data.get("ambiguous"))
    return {
        "acceptance": list(data.get("acceptance") or requirement.get("acceptance") or []),
        "ambiguous": ambiguous,
        "ambiguities": list(data.get("ambiguities") or []) if ambiguous else [],
        "interpretations": list(data.get("interpretations") or []) if ambiguous else [],
    }


class RequirementsAgent(Agent):
    name = "requirements"
    system_prompt = SYSTEM_PROMPT

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        requirement = context.get("requirement") or {"id": run.requirement_id, "type": run.scenario}
        # Reuse the Planner's analysis if it already ran one for this requirement.
        analysis = context.get("requirements_analysis") or analyze_requirement(requirement)
        profile_name = context.get("demo_profile")
        profile_error = context.get("demo_profile_error")
        if "demo_profile" not in context and "demo_profile_error" not in context:
            resolution = resolve_demo_profile(requirement)
            profile_name = resolution.profile.name if resolution.profile else None
            profile_error = resolution.error
        normalized = {
            "problem": f"[{run.scenario}] {requirement.get('title') or requirement.get('intent') or run.requirement_id}",
            "acceptance": analysis["acceptance"],
            "ambiguities": analysis["ambiguities"],
            "interpretations": analysis["interpretations"],
        }
        art = store.write_artifact("requirements/normalized.json", json.dumps(normalized, indent=2))
        if profile_error:
            return {
                "artifact": art,
                "rationale": "Requirement does not match exactly one supported offline demo profile.",
                "exit_ok": False,
                "reason": profile_error,
                "tags": [],
                "context_updates": {
                    "acceptance": normalized["acceptance"],
                    "ambiguities": normalized["ambiguities"],
                    "interpretations": normalized["interpretations"],
                    "demo_profile": None,
                    "demo_profile_error": profile_error,
                },
            }
        return {
            "artifact": art,
            "rationale": "Interpreted intent and captured explicit acceptance criteria.",
            "exit_ok": True,
            "tags": (["ambiguity"] if analysis["ambiguous"] else []),
            "context_updates": {"acceptance": normalized["acceptance"],
                                "ambiguities": normalized["ambiguities"],
                                "interpretations": normalized["interpretations"],
                                "demo_profile": profile_name,
                                "demo_profile_error": None},
        }
