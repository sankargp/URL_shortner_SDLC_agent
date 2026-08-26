"""Docs agent: produces API docs + engineering notes (runs parallel to tests)."""
from __future__ import annotations

from typing import Any

from .base import Agent, llm


class DocsAgent(Agent):
    name = "docs"
    system_prompt = "You write clear, accurate API documentation and concise engineering notes."

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        llm("Document the endpoints and key decisions.", system=self.system_prompt)
        endpoints = context.get("design", {}).get("endpoints", ["POST /shorten", "GET /{code}"])
        doc = "# URL Shortener API\n\n" + "\n".join(f"- `{e}`" for e in endpoints)
        art = store.write_artifact("docs/API.md", doc)
        return {
            "artifact": art,
            "rationale": "Generated endpoint reference from the approved design.",
            "exit_ok": True,
            "context_updates": {"docs_path": art},
        }
