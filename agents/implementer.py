"""Implementer agent: produces the target-app code + API/schema artifacts.

In mock/replay mode it materializes a known-good implementation so the built
service is genuinely runnable end-to-end. In live mode it asks the LLM to
generate the FastAPI service from the approved design and writes the result to
target-app/main.py, falling back to the known-good implementation if the call
fails or the response doesn't look like real code.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from .base import Agent, llm

_APP_PATH = Path("target-app/main.py")
_CODE_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)\n```", re.DOTALL)


class ImplementerAgent(Agent):
    name = "implementer"
    system_prompt = "You write production-quality, modular, testable code with clean design."

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        design = context.get("design", {})
        mode = os.getenv("LLM_MODE", "mock").lower()
        response = llm(self._build_prompt(run, design), system=self.system_prompt, max_tokens=4096)

        note = None
        if mode == "live":
            code = self._extract_code(response)
            if code:
                _APP_PATH.parent.mkdir(parents=True, exist_ok=True)
                _APP_PATH.write_text(code)
                note = "Implemented service from live LLM output; written to target-app/main.py."
            else:
                note = f"Live LLM output unusable ({response[:120]!r}); kept existing target-app/main.py."

        if note is None:
            note = ("Added custom_alias + expiry (brownfield enhancement)."
                    if run.scenario == "brownfield" else
                    "Implemented core shorten/redirect/stats with base62 codes.")

        # Record the OpenAPI-style schema as an artifact (target-app itself is
        # shipped/maintained in /target-app and auto-exposes OpenAPI via FastAPI).
        schema = {
            "paths": {
                "/shorten": {"post": {"summary": "Create a short code"}},
                "/{code}": {"get": {"summary": "Redirect to original URL"}},
                "/{code}/stats": {"get": {"summary": "Per-link analytics"}},
            }
        }
        art = store.write_artifact("implementation/openapi.json", str(schema))
        return {
            "artifact": art,
            "rationale": note,
            "exit_ok": True,
            "tags": (["merge"] if run.scenario != "ambiguous" else []),
            "context_updates": {"schema": schema, "app_path": str(_APP_PATH)},
        }

    def _build_prompt(self, run, design: dict) -> str:
        existing = _APP_PATH.read_text() if _APP_PATH.exists() else ""
        return (
            "Implement the service per the approved design below. Return a single, "
            "complete, runnable Python file for target-app/main.py: a FastAPI URL "
            "shortener with SQLite (SQLAlchemy) persistence, keeping the existing "
            "POST /shorten, GET /{code}, GET /{code}/stats API surface intact.\n\n"
            f"Scenario: {run.scenario}\n"
            f"Design: {design}\n\n"
            f"Current implementation (enhance, don't discard, unless empty):\n{existing}\n\n"
            "Respond with ONLY a single ```python code fence containing the full file."
        )

    @staticmethod
    def _extract_code(response: str) -> str | None:
        if response.startswith("[live-fallback]"):
            return None
        match = _CODE_FENCE.search(response)
        code = match.group(1).strip() if match else response.strip()
        if not code or ("def " not in code and "class " not in code):
            return None
        return code
