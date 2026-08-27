"""Implementer agent with validated, atomic target-app materialization."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import Agent, llm
from .demo_profiles import get_demo_profile

_APP_PATH = Path("target-app/main.py")
_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "target_app_main.py"
_CODE_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)\n```", re.DOTALL)


@dataclass(frozen=True)
class MaterializeResult:
    ok: bool
    sha256: str | None = None
    error: str | None = None


def materialize_validated_source(source: str, destination: Path) -> MaterializeResult:
    """Compile and atomically replace a Python source file."""
    try:
        compile(source, str(destination), "exec")
    except SyntaxError:
        return MaterializeResult(ok=False, error="source_syntax_error")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(source)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, destination)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        return MaterializeResult(ok=False, error="source_write_failed")

    return MaterializeResult(
        ok=True,
        sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )


class ImplementerAgent(Agent):
    name = "implementer"
    system_prompt = "You write production-quality, modular, testable code with clean design."

    def __init__(
        self,
        *,
        app_path: Path = _APP_PATH,
        template_path: Path = _TEMPLATE_PATH,
    ):
        self.app_path = Path(app_path)
        self.template_path = Path(template_path)

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        mode = os.getenv("LLM_MODE", "mock").casefold()
        profile_name = context.get("demo_profile")
        profile = get_demo_profile(profile_name) if profile_name else None
        if mode != "live" and profile is None:
            return {
                "rationale": "No implementation profile was selected.",
                "exit_ok": False,
                "reason": context.get("demo_profile_error") or "missing_demo_profile",
                "context_updates": {},
            }

        repository = context.get("repository") or {}
        app_path = self.app_path
        if repository.get("path") and not app_path.is_absolute():
            app_path = Path(repository["path"]) / app_path
        existing_source = app_path.read_text(encoding="utf-8") if app_path.exists() else None
        if mode == "live":
            response = llm(
                self._build_prompt(run, context.get("design", {}), app_path),
                system=self.system_prompt,
                max_tokens=8192,
            )
            source = self._extract_code(response)
            if source is None:
                materialized = MaterializeResult(ok=False, error="unusable_live_output")
            else:
                materialized = materialize_validated_source(source, app_path)
        else:
            if not self.template_path.exists():
                materialized = MaterializeResult(ok=False, error="template_missing")
            else:
                source = self.template_path.read_text(encoding="utf-8")
                materialized = materialize_validated_source(source, app_path)

        changed_files = (
            [_APP_PATH.as_posix()]
            if materialized.ok and existing_source != app_path.read_text(encoding="utf-8")
            else []
        )

        provenance = {
            "profile": profile.name if profile else None,
            "mode": mode,
            "capabilities": list(profile.capabilities) if profile else [],
            "files": changed_files,
            "sha256": materialized.sha256,
            "error": materialized.error,
        }
        artifact = store.write_artifact(
            "implementation/provenance.json",
            json.dumps(provenance, indent=2),
        )
        if not materialized.ok:
            return {
                "artifact": artifact,
                "rationale": "Implementation source was rejected before replacement.",
                "exit_ok": False,
                "reason": materialized.error,
                "tags": [],
                "context_updates": {"implementation": provenance},
            }

        rationale = (
            f"Materialized validated source for {profile.name}."
            if profile
            else "Materialized validated source from the model's live design."
        )
        return {
            "artifact": artifact,
            "rationale": rationale,
            "exit_ok": True,
            "tags": ["merge"],
            "context_updates": {
                "implementation": provenance,
                "app_path": str(app_path),
                "changed_files": changed_files,
            },
        }

    def _build_prompt(
        self,
        run,
        design: dict[str, Any],
        app_path: Path | None = None,
    ) -> str:
        destination = app_path or self.app_path
        existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
        return (
            "Implement the approved design as one complete runnable FastAPI Python file. "
            "Preserve compatible behavior and return only one Python code fence.\n\n"
            f"Scenario: {run.scenario}\nDesign: {json.dumps(design, indent=2)}\n\n"
            f"Current implementation:\n{existing}"
        )

    @staticmethod
    def _extract_code(response: str) -> str | None:
        match = _CODE_FENCE.search(response)
        code = match.group(1).strip() if match else response.strip()
        if not code or ("def " not in code and "class " not in code):
            return None
        return code + ("\n" if not code.endswith("\n") else "")
