"""Tester agent that reports results from an actual pytest subprocess."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .base import Agent, llm
from .demo_profiles import CORE_TEST_NODE_IDS, get_demo_profile

_CODE_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)\n```", re.DOTALL)
_TEST_MODULE_PROMPT = (
    "Write pytest acceptance tests for a new capability added to an existing "
    "FastAPI URL-shortener service. Respond with exactly one Python code fence "
    "containing a complete, standalone test module (no prose outside the fence).\n\n"
    "Conventions you MUST follow (this file is dropped directly into the existing "
    "target-app/tests/ directory and run alongside it):\n"
    '- Start with `from conftest import link_count` if you need a link count, '
    "otherwise no import of it is needed.\n"
    "- Every test function takes a `harness` fixture parameter (already defined "
    "in conftest.py; do not redefine or import it).\n"
    "- `harness.client` is a `fastapi.testclient.TestClient` for the app.\n"
    "- `harness.module` is the loaded application module (e.g. `harness.module.Link`, "
    "`harness.module.Session` for direct DB assertions).\n"
    "- Name every test function `test_...` and add `import pytest` plus any stdlib "
    "imports you need (e.g. `datetime`) at the top of the fence.\n"
    "- Only assert behavior described in the acceptance criteria below; do not invent "
    "unrelated requirements.\n\n"
    "Acceptance criteria:\n{acceptance}\n\n"
    "Design / impact analysis:\n{design}\n\n"
    "Implemented source (target-app/main.py):\n{source}\n"
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "requirement"


def _extract_test_module(response: str) -> str | None:
    match = _CODE_FENCE.search(response)
    code = match.group(1).strip() if match else response.strip()
    if not code or "def test_" not in code:
        return None
    try:
        compile(code, "<generated-tests>", "exec")
    except SyntaxError:
        return None
    return code + ("\n" if not code.endswith("\n") else "")


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
        repository = context.get("repository") or {}
        # Tests must run against the isolated per-run checkout the implementer
        # just wrote into, not the orchestrator process's own working directory.
        repo_root = Path(repository["path"]) if repository.get("path") else Path.cwd()

        profile_name = context.get("demo_profile")
        mode = os.getenv("LLM_MODE", "mock").casefold()
        generated_test_path: str | None = None
        if profile_name:
            profile = get_demo_profile(profile_name)
            test_node_ids = profile.test_node_ids
            expect_exact_count = True
        elif mode == "live":
            written = self._generate_test_module(run, context, repo_root)
            if written is None:
                return {
                    "rationale": "The model did not return a usable test module.",
                    "exit_ok": False,
                    "reason": "generated_tests_unusable",
                    "context_updates": {},
                }
            generated_test_path = written
            test_node_ids = CORE_TEST_NODE_IDS + (generated_test_path,)
            expect_exact_count = False
        else:
            return {
                "rationale": "No testing profile was selected.",
                "exit_ok": False,
                "reason": context.get("demo_profile_error") or "missing_demo_profile",
                "context_updates": {},
            }

        environment = dict(os.environ)
        with tempfile.TemporaryDirectory(prefix="agentic-sdlc-db-") as database_dir:
            environment["URL_SHORTENER_DATABASE_PATH"] = str(
                Path(database_dir) / "urls.db"
            )
            report = run_pytest_nodes(
                test_node_ids,
                env=environment,
                cwd=repo_root,
            )

        report_data = asdict(report)
        report_data["command"] = list(report.command)
        report_data["profile"] = profile_name
        report_data["generated_test_path"] = generated_test_path
        report_data["acceptance"] = list(context.get("acceptance", []))
        report_data["test_node_ids"] = list(test_node_ids)
        artifact = store.write_artifact(
            "testing/report.json",
            json.dumps(report_data, indent=2),
        )
        exit_ok = report.return_code == 0 and report.failed == 0 and report.errors == 0
        if expect_exact_count:
            exit_ok = exit_ok and report.total == len(test_node_ids)
        else:
            exit_ok = exit_ok and report.total > len(CORE_TEST_NODE_IDS)

        context_updates: dict[str, Any] = {"test_report": report_data}
        if generated_test_path and exit_ok:
            changed_files = list(context.get("changed_files") or [])
            if generated_test_path not in changed_files:
                changed_files.append(generated_test_path)
            context_updates["changed_files"] = changed_files

        label = profile_name or "a generated test module"
        return {
            "artifact": artifact,
            "rationale": f"Executed {report.total} acceptance tests for {label}.",
            "exit_ok": exit_ok,
            "reason": None if exit_ok else "pytest_acceptance_failed",
            "context_updates": context_updates,
        }

    def _generate_test_module(
        self, run, context: dict[str, Any], repo_root: Path
    ) -> str | None:
        app_path = repo_root / "target-app" / "main.py"
        source = app_path.read_text(encoding="utf-8") if app_path.exists() else ""
        prompt = _TEST_MODULE_PROMPT.format(
            acceptance=json.dumps(context.get("acceptance", []), indent=2),
            design=json.dumps(context.get("design", {}), indent=2),
            source=source,
        )
        response = llm(prompt, system=self.system_prompt, max_tokens=4096)
        module_source = _extract_test_module(response)
        if module_source is None:
            return None
        filename = f"test_generated_{_slug(run.requirement_id)}.py"
        destination = repo_root / "target-app" / "tests" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(module_source, encoding="utf-8")
        return f"target-app/tests/{filename}"
