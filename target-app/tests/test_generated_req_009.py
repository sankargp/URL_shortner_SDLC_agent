import uuid

import pytest

from conftest import _dispose, _load_app


def _create_link(harness, target_url):
    response = harness.client.post("/shorten", json={"url": target_url})
    assert response.status_code == 200
    return response.json()["code"]


def test_delete_existing_link_returns_empty_204_and_removes_row(harness):
    code = _create_link(harness, "https://example.com/to-delete")

    response = harness.client.delete(f"/{code}")

    assert response.status_code == 204
    assert response.content == b""

    db = harness.module.Session()
    try:
        assert (
            db.query(harness.module.Link)
            .filter(harness.module.Link.code == code)
            .first()
            is None
        )
    finally:
        db.close()


def test_deleted_link_redirect_and_statistics_return_404(harness):
    code = _create_link(harness, "https://example.com/deleted")
    assert harness.client.delete(f"/{code}").status_code == 204

    redirect_response = harness.client.get(
        f"/{code}",
        follow_redirects=False,
    )
    statistics_response = harness.client.get(f"/{code}/stats")

    assert redirect_response.status_code == 404
    assert statistics_response.status_code == 404


def test_unknown_short_code_requests_return_404(harness):
    unknown_code = f"unknown-{uuid.uuid4().hex}"

    assert harness.client.delete(f"/{unknown_code}").status_code == 404
    assert (
        harness.client.get(
            f"/{unknown_code}",
            follow_redirects=False,
        ).status_code
        == 404
    )
    assert harness.client.get(f"/{unknown_code}/stats").status_code == 404


def test_deletion_persists_after_service_restart(harness):
    code = _create_link(harness, "https://example.com/persisted-deletion")
    assert harness.client.delete(f"/{code}").status_code == 204

    database_path = harness.database_path
    _dispose(harness)
    restarted = _load_app(database_path)

    try:
        assert (
            restarted.client.get(
                f"/{code}",
                follow_redirects=False,
            ).status_code
            == 404
        )
        assert restarted.client.get(f"/{code}/stats").status_code == 404
        assert restarted.client.delete(f"/{code}").status_code == 404

        db = restarted.module.Session()
        try:
            assert (
                db.query(restarted.module.Link)
                .filter(restarted.module.Link.code == code)
                .first()
                is None
            )
        finally:
            db.close()
    finally:
        _dispose(restarted)


def test_deletion_does_not_affect_non_deleted_redirect_or_statistics(harness):
    deleted_code = _create_link(harness, "https://example.com/remove-me")
    active_url = "https://example.com/still-active"
    active_code = _create_link(harness, active_url)

    initial_statistics = harness.client.get(f"/{active_code}/stats")
    assert initial_statistics.status_code == 200
    assert initial_statistics.json()["code"] == active_code
    assert initial_statistics.json()["url"] == active_url
    assert initial_statistics.json()["clicks"] == 0

    assert harness.client.delete(f"/{deleted_code}").status_code == 204

    redirect_response = harness.client.get(
        f"/{active_code}",
        follow_redirects=False,
    )
    assert redirect_response.status_code == 307
    assert redirect_response.headers["location"] == active_url

    updated_statistics = harness.client.get(f"/{active_code}/stats")
    assert updated_statistics.status_code == 200
    assert updated_statistics.json()["code"] == active_code
    assert updated_statistics.json()["url"] == active_url
    assert updated_statistics.json()["clicks"] == 1
