"""Transactional refresh of deterministic Governance demo requirements."""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class DuplicateValidationError(RuntimeError):
    """Raised when a destructive catalog target is not the expected duplicate."""


@dataclass(frozen=True)
class RefreshResult:
    removed_ids: tuple[int, ...]
    created_ids: tuple[int, ...]
    created_titles: tuple[str, ...]
    backup_path: Path


_REPLACEMENTS = (
    {
        "requirement_type": "brownfield",
        "title": "Password-protected short links",
        "intent": (
            "Allow a link creator to require a password before a short link redirects "
            "without changing existing unprotected links."
        ),
        "acceptance": [
            "POST /shorten accepts an optional password",
            "Passwords are salted and hashed; plaintext is never persisted or returned",
            "Unprotected links redirect exactly as they do today",
            "Protected links return 401 when X-Link-Password is missing or incorrect",
            "Protected links redirect when X-Link-Password is correct",
            "Existing requests that omit password remain backward compatible",
        ],
        "constraints": [
            "Use PBKDF2-HMAC-SHA256 and constant-time comparison",
            "Never log or persist plaintext passwords",
            "Keep SQLite persistence",
        ],
    },
    {
        "requirement_type": "brownfield",
        "title": "Bulk URL shortening with idempotent retries",
        "intent": (
            "Allow clients to shorten several URLs in one request and safely retry "
            "without creating duplicate links."
        ),
        "acceptance": [
            "POST /shorten/batch accepts between 1 and 100 items",
            "Batch items support URL, custom alias, expiry, and password fields",
            "The endpoint requires an Idempotency-Key header",
            "The same key and payload replay the original response without new links",
            "The same key with a different payload returns 409 Conflict",
            "Mixed item results preserve input order and successful items",
            "POST /shorten remains backward compatible",
        ],
        "constraints": [
            "Store digests rather than raw idempotency keys",
            "Persist completed responses across restart",
            "Limit each batch to 100 items",
            "Keep SQLite persistence",
        ],
    },
)


def _normalized_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _is_expiry_demo_duplicate(title: str) -> bool:
    normalized = _normalized_title(title)
    return normalized.startswith("add custom aliases and link expiry") and "live demo" in normalized


def _snapshot_canonical(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "select * from requirements where id <= 3 order by id"
    ).fetchall()


def _online_backup(database_path: Path, backup_path: Path) -> None:
    source = sqlite3.connect(database_path, timeout=30)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def refresh_demo_requirements(
    workspace_dir: str | Path,
    *,
    now: datetime | None = None,
) -> RefreshResult:
    """Back up and replace only verified REQ-004/REQ-005 expiry duplicates."""
    workspace = Path(workspace_dir)
    database_path = workspace / "governance.db"
    if not database_path.exists():
        raise FileNotFoundError(database_path)

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    backup_dir = workspace / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (
        "governance-before-demo-refresh-"
        f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}.db"
    )
    _online_backup(database_path, backup_path)

    connection = sqlite3.connect(database_path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        canonical_before = _snapshot_canonical(connection)
        canonical = connection.execute(
            "select title from requirements where id=2"
        ).fetchone()
        if canonical is None or canonical[0] != "Add custom aliases and link expiry":
            raise DuplicateValidationError("Canonical REQ-002 did not match expected title")

        duplicates = connection.execute(
            "select id,title from requirements where id in (4,5) order by id"
        ).fetchall()
        if [row[0] for row in duplicates] != [4, 5]:
            raise DuplicateValidationError("REQ-004 and REQ-005 must both exist")
        for requirement_id, title in duplicates:
            if not _is_expiry_demo_duplicate(title):
                raise DuplicateValidationError(
                    f"REQ-{requirement_id:03d} is not an expiry Live Demo duplicate"
                )

        connection.execute("delete from requirements where id in (4,5)")
        created_ids: list[int] = []
        iso_now = timestamp.isoformat()
        for replacement in _REPLACEMENTS:
            cursor = connection.execute(
                """
                insert into requirements (
                    requirement_type, title, intent, acceptance, constraints,
                    possible_interpretations, authoring_status, execution_status,
                    analysis_status, analysis, analysis_error, current_run_id,
                    created_at, updated_at, analyzed_at, implemented_at, revision
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    replacement["requirement_type"],
                    replacement["title"],
                    replacement["intent"],
                    json.dumps(replacement["acceptance"]),
                    json.dumps(replacement["constraints"]),
                    json.dumps([]),
                    "draft",
                    "not_started",
                    "not_requested",
                    None,
                    None,
                    None,
                    iso_now,
                    iso_now,
                    None,
                    None,
                    0,
                ),
            )
            created_ids.append(int(cursor.lastrowid))

        if _snapshot_canonical(connection) != canonical_before:
            raise DuplicateValidationError("Canonical requirements changed during refresh")
        duplicate_titles = connection.execute(
            "select lower(title), count(*) from requirements "
            "group by lower(title) having count(*) > 1"
        ).fetchall()
        if duplicate_titles:
            raise DuplicateValidationError("Refresh would leave duplicate requirement titles")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return RefreshResult(
        removed_ids=(4, 5),
        created_ids=tuple(created_ids),
        created_titles=tuple(item["title"] for item in _REPLACEMENTS),
        backup_path=backup_path,
    )
