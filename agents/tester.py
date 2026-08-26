"""Tester agent that reports results from an actual pytest subprocess."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .base import Agent
from .demo_profiles import get_demo_profile


@dataclass(frozen=True)
class PytestReport:
    return_code: int
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    command: tuple[str, ...]
    stdout: str
    stderr: str


def run_pytest_nodes(
    node_ids: tuple[str, ...],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int = 120,
) -> PytestReport:
    """Run exact pytest node IDs and derive counts from JUnit XML."""
    if not node_ids:
        return PytestReport(
            return_code=5,
            total=0,
            passed=0,
            failed=0,
            errors=0,
            skipped=0,
            command=(),
            stdout="",
            stderr="no acceptance tests configured",
        )

    with tempfile.TemporaryDirectory(prefix="agentic-sdlc-tests-") as temporary_dir:
        junit_path = Path(temporary_dir) / "junit.xml"
        command = (
            sys.executable,
            "-m",
            "pytest",
            *node_ids,
            "-q",
            f"--junitxml={junit_path}",
        )
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return PytestReport(
                return_code=124,
                total=0,
                passed=0,
                failed=0,
                errors=0,
                skipped=0,
                command=command,
                stdout=(exc.stdout or "")[-4000:],
                stderr="pytest timed out",
            )

        total = failed = errors = skipped = 0
        if junit_path.exists():
            root = ET.parse(junit_path).getroot()
            suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
            for suite in suites:
                total += int(suite.attrib.get("tests", 0))
                failed += int(suite.attrib.get("failures", 0))
                errors += int(suite.attrib.get("errors", 0))
                skipped += int(suite.attrib.get("skipped", 0))
        passed = max(total - failed - errors - skipped, 0)
        return PytestReport(
            return_code=completed.returncode,
            total=total,
            passed=passed,
            failed=failed,
            errors=errors,
            skipped=skipped,
            command=command,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
        )


class TesterAgent(Agent):
    name = "tester"
    system_prompt = "You validate acceptance criteria with executable tests."

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        profile_name = context.get("demo_profile")
        if not profile_name:
            return {
                "rationale": "No testing profile was selected.",
                "exit_ok": False,
                "reason": context.get("demo_profile_error") or "missing_demo_profile",
                "context_updates": {},
            }

        profile = get_demo_profile(profile_name)
        environment = dict(os.environ)
        with tempfile.TemporaryDirectory(prefix="agentic-sdlc-db-") as database_dir:
            environment["URL_SHORTENER_DATABASE_PATH"] = str(
                Path(database_dir) / "urls.db"
            )
            report = run_pytest_nodes(
                profile.test_node_ids,
                env=environment,
                cwd=Path.cwd(),
            )

        report_data = asdict(report)
        report_data["command"] = list(report.command)
        report_data["profile"] = profile.name
        report_data["acceptance"] = list(context.get("acceptance", []))
        report_data["test_node_ids"] = list(profile.test_node_ids)
        artifact = store.write_artifact(
            "testing/report.json",
            json.dumps(report_data, indent=2),
        )
        exit_ok = (
            report.return_code == 0
            and report.failed == 0
            and report.errors == 0
            and report.total == len(profile.test_node_ids)
        )
        return {
            "artifact": artifact,
            "rationale": f"Executed {report.total} acceptance tests for {profile.name}.",
            "exit_ok": exit_ok,
            "reason": None if exit_ok else "pytest_acceptance_failed",
            "context_updates": {"test_report": report_data},
        }
