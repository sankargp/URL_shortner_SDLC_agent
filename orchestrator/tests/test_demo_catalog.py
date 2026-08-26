"""Safety tests for refreshing duplicate Governance demo requirements."""
from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

import orchestrator.cli as cli_module
from orchestrator.demo_catalog import (
    DuplicateValidationError,
    refresh_demo_requirements,
)
from orchestrator.requirements_store import RequirementsRepository


def _catalog_with_duplicates(tmp_path, *, second_title: str | None = None):
    workspace = tmp_path / "workspace"
    repository = RequirementsRepository(workspace)
    repository.create(
        requirement_type="greenfield",
        title="Build URL shortener core",
        intent="Shorten URLs",
    )
    repository.create(
        requirement_type="brownfield",
        title="Add custom aliases and link expiry",
        intent="Add aliases and expiry",
    )
    repository.create(
        requirement_type="ambiguous",
        title="Make the service more reliable",
        intent="Improve reliability",
    )
    repository.create(
        requirement_type="brownfield",
        title="Add custom aliases and link expiry — Live Demo 175831",
        intent="Duplicate expiry demo",
    )
    repository.create(
        requirement_type="brownfield",
        title=second_title or "Add custom aliases and link expiry — Live Demo 180015",
        intent="Duplicate expiry demo",
    )
    return workspace, repository


def _rows(database_path, query, parameters=()):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()


def test_refresh_backs_up_deletes_only_duplicates_and_inserts_fresh_drafts(tmp_path):
    workspace, _ = _catalog_with_duplicates(tmp_path)
    database_path = workspace / "governance.db"
    canonical_before = _rows(
        database_path,
        "select * from requirements where id <= 3 order by id",
    )

    result = refresh_demo_requirements(workspace)

    canonical_after = _rows(
        database_path,
        "select * from requirements where id <= 3 order by id",
    )
    active = _rows(
        database_path,
        "select id,title,authoring_status,execution_status,analysis_status "
        "from requirements order by id",
    )
    backup_duplicates = _rows(
        result.backup_path,
        "select id,title from requirements where id in (4,5) order by id",
    )

    assert result.removed_ids == (4, 5)
    assert result.created_ids == (6, 7)
    assert canonical_after == canonical_before
    assert [row["id"] for row in active] == [1, 2, 3, 6, 7]
    assert [row["title"] for row in active[-2:]] == [
        "Password-protected short links",
        "Bulk URL shortening with idempotent retries",
    ]
    assert all(row["authoring_status"] == "draft" for row in active[-2:])
    assert all(row["execution_status"] == "not_started" for row in active[-2:])
    assert all(row["analysis_status"] == "not_requested" for row in active[-2:])
    assert [row["id"] for row in backup_duplicates] == [4, 5]


def test_title_mismatch_aborts_without_partial_mutation(tmp_path):
    workspace, _ = _catalog_with_duplicates(
        tmp_path,
        second_title="Do not delete this requirement",
    )
    database_path = workspace / "governance.db"
    before = _rows(database_path, "select * from requirements order by id")

    with pytest.raises(DuplicateValidationError, match="REQ-005"):
        refresh_demo_requirements(workspace)

    after = _rows(database_path, "select * from requirements order by id")
    assert after == before


def test_refresh_preserves_canonical_expiry_requirement(tmp_path):
    workspace, _ = _catalog_with_duplicates(tmp_path)

    refresh_demo_requirements(workspace)

    canonical = _rows(
        workspace / "governance.db",
        "select title,execution_status,current_run_id from requirements where id=2",
    )[0]
    assert canonical == {
        "title": "Add custom aliases and link expiry",
        "execution_status": "not_started",
        "current_run_id": None,
    }


def test_cli_requires_explicit_confirmation_before_refresh(tmp_path, monkeypatch):
    workspace, _ = _catalog_with_duplicates(tmp_path)
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    before = _rows(workspace / "governance.db", "select * from requirements order by id")

    refused = CliRunner().invoke(cli_module.app, ["refresh-demo-requirements"])

    assert refused.exit_code == 1
    assert "requires --yes" in refused.output
    assert _rows(
        workspace / "governance.db",
        "select * from requirements order by id",
    ) == before


def test_cli_refreshes_catalog_and_reports_backup(tmp_path, monkeypatch):
    workspace, _ = _catalog_with_duplicates(tmp_path)
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))

    result = CliRunner().invoke(
        cli_module.app,
        ["refresh-demo-requirements", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "Removed: REQ-004, REQ-005" in result.output
    assert "Created: REQ-006, REQ-007" in result.output
    assert "Backup:" in result.output
