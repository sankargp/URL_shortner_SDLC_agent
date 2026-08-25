"""Tester agent: generates and 'runs' unit + integration tests.

Exit gate models acceptance: if generated tests would fail, exit_ok=False, which
drives the kernel's bounded-retry path. The demo passes deterministically.
"""
from __future__ import annotations

from typing import Any
from .base import Agent, llm


class TesterAgent(Agent):
    name = "tester"
    system_prompt = "You write meaningful unit + integration tests and validate acceptance criteria."

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        llm("Generate tests for the acceptance criteria.", system=self.system_prompt)
        acceptance = context.get("acceptance", [])
        test_report = {
            "unit": {"total": 8, "passed": 8},
            "integration": {"total": 3, "passed": 3},
            "acceptance_covered": acceptance,
        }
        art = store.write_artifact("testing/report.json", str(test_report))
        exit_ok = test_report["unit"]["passed"] == test_report["unit"]["total"]
        return {
            "artifact": art,
            "rationale": "Tests cover core acceptance criteria; all green.",
            "exit_ok": exit_ok,
            "reason": None if exit_ok else "unit_tests_failing",
            "context_updates": {"test_report": test_report},
        }
