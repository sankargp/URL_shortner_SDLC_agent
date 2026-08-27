"""Shared test harness for the URL-shortener acceptance tests.

Lives in conftest.py (not test_shortener.py) so any generated test module in
this directory can use the `harness` fixture without importing across test files.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP_PATH = Path(__file__).resolve().parents[1] / "main.py"


@dataclass
class AppHarness:
    module: object
    client: TestClient
    database_path: Path
    module_name: str


def _load_app(database_path: Path) -> AppHarness:
    os.environ["URL_SHORTENER_DATABASE_PATH"] = str(database_path)
    module_name = f"target_app_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return AppHarness(
        module=module,
        client=TestClient(module.app),
        database_path=database_path,
        module_name=module_name,
    )


def _dispose(harness: AppHarness) -> None:
    harness.module.engine.dispose()
    sys.modules.pop(harness.module_name, None)


def link_count(harness: AppHarness) -> int:
    session = harness.module.Session()
    try:
        return session.query(harness.module.Link).count()
    finally:
        session.close()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    database_path = tmp_path / "urls.db"
    monkeypatch.setenv("URL_SHORTENER_DATABASE_PATH", str(database_path))
    loaded = _load_app(database_path)
    yield loaded
    _dispose(loaded)
