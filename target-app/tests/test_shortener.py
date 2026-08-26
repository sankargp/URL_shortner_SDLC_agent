"""Acceptance tests for the generated URL-shortener target application."""
from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


@pytest.fixture
def harness(tmp_path, monkeypatch):
    database_path = tmp_path / "urls.db"
    monkeypatch.setenv("URL_SHORTENER_DATABASE_PATH", str(database_path))
    loaded = _load_app(database_path)
    yield loaded
    _dispose(loaded)


@pytest.mark.profile_core
def test_clean_database_shorten_and_redirect(harness):
    assert harness.module.DATABASE_PATH == harness.database_path
    response = harness.client.post(
        "/shorten",
        json={"url": "https://example.com"},
    )

    assert response.status_code == 200
    code = response.json()["code"]
    redirected = harness.client.get(f"/{code}", follow_redirects=False)
    assert redirected.status_code == 307
    assert redirected.headers["location"] == "https://example.com/"


@pytest.mark.profile_core
def test_stats_increment(harness):
    created = harness.client.post(
        "/shorten",
        json={"url": "https://stats.example"},
    )
    code = created.json()["code"]

    harness.client.get(f"/{code}", follow_redirects=False)
    stats = harness.client.get(f"/{code}/stats")

    assert stats.status_code == 200
    assert stats.json()["clicks"] == 1


@pytest.mark.profile_core
def test_invalid_url_rejected(harness):
    response = harness.client.post("/shorten", json={"url": "not-a-url"})

    assert response.status_code == 422


@pytest.mark.profile_alias_expiry
def test_custom_alias_and_expiry(harness):
    created = harness.client.post(
        "/shorten",
        json={
            "url": "https://alias.example",
            "custom_alias": "friendly_name",
            "expiry_days": 1,
        },
    )

    assert created.status_code == 200
    assert created.json()["code"] == "friendly_name"
    duplicate = harness.client.post(
        "/shorten",
        json={"url": "https://duplicate.example", "custom_alias": "friendly_name"},
    )
    assert duplicate.status_code == 409

    session = harness.module.Session()
    try:
        link = session.query(harness.module.Link).filter_by(code="friendly_name").one()
        link.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    expired = harness.client.get("/friendly_name", follow_redirects=False)
    assert expired.status_code == 410


@pytest.mark.profile_password
def test_protected_link_requires_correct_password(harness):
    created = harness.client.post(
        "/shorten",
        json={"url": "https://private.example", "password": "s3cret"},
    )
    assert created.status_code == 200
    code = created.json()["code"]

    missing = harness.client.get(f"/{code}", follow_redirects=False)
    incorrect = harness.client.get(
        f"/{code}",
        headers={"X-Link-Password": "wrong"},
        follow_redirects=False,
    )
    stats_before = harness.client.get(f"/{code}/stats")
    authorized = harness.client.get(
        f"/{code}",
        headers={"X-Link-Password": "s3cret"},
        follow_redirects=False,
    )
    stats_after = harness.client.get(f"/{code}/stats")

    assert missing.status_code == 401
    assert incorrect.status_code == 401
    assert stats_before.json()["clicks"] == 0
    assert authorized.status_code == 307
    assert authorized.headers["location"] == "https://private.example/"
    assert stats_after.json()["clicks"] == 1


@pytest.mark.profile_password
def test_password_is_not_persisted_in_plaintext(harness):
    secret = "never-store-this"
    created = harness.client.post(
        "/shorten",
        json={"url": "https://secret.example", "password": secret},
    )
    code = created.json()["code"]

    session = harness.module.Session()
    try:
        link = session.query(harness.module.Link).filter_by(code=code).one()
        assert link.password_salt
        assert link.password_hash
        assert secret not in link.password_salt
        assert secret not in link.password_hash
    finally:
        session.close()

    stats = harness.client.get(f"/{code}/stats")
    assert "password" not in str(stats.json()).casefold()


@pytest.mark.profile_password
def test_expired_protected_link_returns_gone_before_authentication(harness):
    created = harness.client.post(
        "/shorten",
        json={
            "url": "https://expired-private.example",
            "password": "s3cret",
            "expiry_days": 1,
        },
    )
    code = created.json()["code"]
    session = harness.module.Session()
    try:
        link = session.query(harness.module.Link).filter_by(code=code).one()
        link.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    response = harness.client.get(f"/{code}", follow_redirects=False)

    assert response.status_code == 410


def _link_count(harness: AppHarness) -> int:
    session = harness.module.Session()
    try:
        return session.query(harness.module.Link).count()
    finally:
        session.close()


@pytest.mark.profile_bulk
def test_batch_retry_is_idempotent(harness):
    payload = {
        "items": [
            {"url": "https://one.example"},
            {"url": "https://two.example", "password": "batch-secret"},
        ]
    }
    headers = {"Idempotency-Key": "retry-123"}

    first = harness.client.post("/shorten/batch", headers=headers, json=payload)
    second = harness.client.post("/shorten/batch", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert _link_count(harness) == 2

    database_path = harness.database_path
    _dispose(harness)
    restarted = _load_app(database_path)
    try:
        replayed = restarted.client.post("/shorten/batch", headers=headers, json=payload)
        assert replayed.status_code == 200
        assert replayed.json() == first.json()
        assert _link_count(restarted) == 2
    finally:
        _dispose(restarted)


@pytest.mark.profile_bulk
def test_batch_mixed_results_preserve_order(harness):
    existing = harness.client.post(
        "/shorten",
        json={"url": "https://existing.example", "custom_alias": "taken"},
    )
    assert existing.status_code == 200

    response = harness.client.post(
        "/shorten/batch",
        headers={"Idempotency-Key": "mixed-123"},
        json={
            "items": [
                {"url": "https://success.example"},
                {"url": "https://conflict.example", "custom_alias": "taken"},
                {"url": "not-a-url"},
            ]
        },
    )

    assert response.status_code == 207
    results = response.json()["results"]
    assert [item["index"] for item in results] == [0, 1, 2]
    assert [item["status"] for item in results] == [200, 409, 422]
    assert results[0]["code"]
    assert results[1]["detail"] == "alias already taken"
    assert results[2]["detail"] == "invalid item"


@pytest.mark.profile_bulk
def test_batch_rejects_changed_payload_for_key(harness):
    headers = {"Idempotency-Key": "same-key"}
    first = harness.client.post(
        "/shorten/batch",
        headers=headers,
        json={"items": [{"url": "https://first.example"}]},
    )
    changed = harness.client.post(
        "/shorten/batch",
        headers=headers,
        json={"items": [{"url": "https://changed.example"}]},
    )

    assert first.status_code == 200
    assert changed.status_code == 409
    assert changed.json()["detail"] == "idempotency key reused with different payload"
    assert _link_count(harness) == 1


@pytest.mark.profile_bulk
def test_batch_requires_key_and_bounds_item_count(harness):
    missing_key = harness.client.post(
        "/shorten/batch",
        json={"items": [{"url": "https://one.example"}]},
    )
    empty = harness.client.post(
        "/shorten/batch",
        headers={"Idempotency-Key": "empty"},
        json={"items": []},
    )
    too_many = harness.client.post(
        "/shorten/batch",
        headers={"Idempotency-Key": "too-many"},
        json={"items": [{"url": f"https://{index}.example"} for index in range(101)]},
    )

    assert missing_key.status_code == 422
    assert empty.status_code == 422
    assert too_many.status_code == 422
