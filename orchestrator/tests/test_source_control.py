"""Behavior tests for isolated Git checkout and GitHub publication."""
from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from orchestrator.source_control import (
    GitHubClient,
    GitPublisher,
    GitWorkspace,
    RepositoryConfig,
    SourceControlError,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    remote.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Test User")
    _git(source, "config", "user.email", "test@example.com")
    (source / "target-app").mkdir()
    (source / "target-app" / "main.py").write_text("VERSION = 1\n")
    _git(source, "add", "target-app/main.py")
    _git(source, "commit", "-m", "initial")
    _git(remote, "init", "--bare")
    _git(source, "remote", "add", "custom", str(remote))
    _git(source, "push", "-u", "custom", "main")
    return source, remote


def _config(source: Path, remote: Path) -> RepositoryConfig:
    return RepositoryConfig(
        source_root=source.resolve(),
        remote_name="custom",
        remote_url=str(remote),
        base_branch="main",
        github_repository="acme/widget",
        token="",
        author_name="Agentic SDLC",
        author_email="agent@example.com",
    )


def test_discover_uses_the_only_remote_when_origin_is_absent(tmp_path, monkeypatch):
    source, _ = _repository(tmp_path)
    _git(source, "remote", "set-url", "custom", "https://github.com/acme/widget.git")
    monkeypatch.setenv("SOURCE_REPO_PATH", str(source))
    monkeypatch.delenv("GIT_REMOTE_NAME", raising=False)
    monkeypatch.delenv("GIT_BASE_BRANCH", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    config = RepositoryConfig.discover()

    assert config.source_root == source.resolve()
    assert config.remote_name == "custom"
    assert config.base_branch == "main"
    assert config.github_repository == "acme/widget"


def test_checkout_uses_committed_head_and_leaves_dirty_source_untouched(tmp_path):
    source, remote = _repository(tmp_path)
    tracked = source / "target-app" / "main.py"
    tracked.write_text("VERSION = 999\n")
    run_root = tmp_path / "runs" / "run-12345678"

    checkout = GitWorkspace(_config(source, remote)).checkout(
        run_root,
        requirement_id="REQ-42",
        run_id="run-12345678",
    )

    assert (checkout.path / "target-app" / "main.py").read_text() == "VERSION = 1\n"
    assert tracked.read_text() == "VERSION = 999\n"
    assert _git(source, "status", "--short") == "M target-app/main.py"
    assert checkout.branch == "agentic/req-42-run-1234"


def test_checkout_rejects_a_local_base_that_is_not_on_the_remote(tmp_path):
    source, remote = _repository(tmp_path)
    (source / "local-only.txt").write_text("not pushed\n")
    _git(source, "add", "local-only.txt")
    _git(source, "commit", "-m", "local only")

    with pytest.raises(SourceControlError, match="does not match remote"):
        GitWorkspace(_config(source, remote)).checkout(
            tmp_path / "run",
            requirement_id="REQ-1",
            run_id="run-1",
        )


def test_checkout_recovers_when_clone_exists_before_branch_creation(tmp_path):
    source, remote = _repository(tmp_path)
    config = _config(source, remote)
    run_root = tmp_path / "run"
    run_root.mkdir()
    _git(run_root, "clone", "--branch", "main", str(source), "repository")

    checkout = GitWorkspace(config).checkout(
        run_root,
        requirement_id="REQ-2",
        run_id="run-recovery",
    )

    assert _git(checkout.path, "branch", "--show-current") == "agentic/req-2-run-reco"
    assert _git(checkout.path, "remote", "get-url", "origin") == str(remote)


class _GitHub:
    def __init__(self, *, fail_once: bool = False):
        self.fail_once = fail_once
        self.requests: list[dict] = []

    def find_or_create_pull_request(self, **request):
        self.requests.append(request)
        if self.fail_once:
            self.fail_once = False
            raise SourceControlError("temporary GitHub failure")
        return {"number": 17, "html_url": "https://github.com/acme/widget/pull/17"}


def test_publish_recovers_after_commit_and_push_precede_pr_failure(tmp_path):
    source, remote = _repository(tmp_path)
    config = _config(source, remote)
    checkout = GitWorkspace(config).checkout(
        tmp_path / "run",
        requirement_id="REQ-7",
        run_id="run-abcdefgh",
    )
    app_path = checkout.path / "target-app" / "main.py"
    app_path.write_text("VERSION = 2\n")
    github = _GitHub(fail_once=True)
    publisher = GitPublisher(config, github)

    with pytest.raises(SourceControlError, match="temporary GitHub failure"):
        publisher.publish(
            checkout,
            requirement={
                "id": "REQ-7",
                "title": "Change behavior",
                "intent": "Make the behavior observable",
                "acceptance": ["The version changes"],
            },
            run_id="run-abcdefgh",
            changed_files=["target-app/main.py"],
            verification={"unit": {"passed": 1, "total": 1}},
        )

    result = publisher.publish(
        checkout,
        requirement={
            "id": "REQ-7",
            "title": "Change behavior",
            "intent": "Make the behavior observable",
            "acceptance": ["The version changes"],
        },
        run_id="run-abcdefgh",
        changed_files=["target-app/main.py"],
        verification={"unit": {"passed": 1, "total": 1}},
    )

    assert result["outcome"] == "pr_opened"
    assert result["pr_number"] == 17
    assert result["pr_url"] == "https://github.com/acme/widget/pull/17"
    assert result["branch"] == "agentic/req-7-run-abcd"
    assert len(github.requests) == 2
    assert github.requests[-1]["title"] == "[REQ-7] Change behavior"
    assert "The version changes" in github.requests[-1]["body"]
    assert _git(checkout.path, "rev-list", "--count", "main..HEAD") == "1"


def test_publish_returns_no_changes_without_calling_github(tmp_path):
    source, remote = _repository(tmp_path)
    config = _config(source, remote)
    checkout = GitWorkspace(config).checkout(
        tmp_path / "run",
        requirement_id="REQ-8",
        run_id="run-ijklmnop",
    )
    github = _GitHub()

    result = GitPublisher(config, github).publish(
        checkout,
        requirement={"id": "REQ-8", "title": "Already done", "intent": "", "acceptance": []},
        run_id="run-ijklmnop",
        changed_files=[],
        verification={},
    )

    assert result == {
        "outcome": "no_changes",
        "branch": "agentic/req-8-run-ijkl",
        "base_branch": "main",
    }
    assert github.requests == []


@pytest.mark.parametrize("changed_file", ["../outside.py", ".env", "secrets/token.txt"])
def test_publish_rejects_paths_outside_the_clone_or_containing_secrets(
    tmp_path, changed_file
):
    source, remote = _repository(tmp_path)
    config = _config(source, remote)
    checkout = GitWorkspace(config).checkout(
        tmp_path / "run",
        requirement_id="REQ-9",
        run_id="run-qrstuvwx",
    )

    with pytest.raises(SourceControlError, match="unsafe changed path"):
        GitPublisher(config, _GitHub()).publish(
            checkout,
            requirement={"id": "REQ-9", "title": "Unsafe", "intent": "", "acceptance": []},
            run_id="run-qrstuvwx",
            changed_files=[changed_file],
            verification={},
        )


def test_github_errors_redact_a_token_echoed_by_the_server(tmp_path):
    source, remote = _repository(tmp_path)
    config = _config(source, remote)
    config = RepositoryConfig(**{**config.__dict__, "token": "secret-token"})

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "invalid secret-token"})

    client = httpx.Client(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(reject),
    )

    with pytest.raises(SourceControlError) as failure:
        GitHubClient(config, client).find_or_create_pull_request(
            title="[REQ-1] Change",
            body="Body",
            head="agentic/req-1-run-1",
            base="main",
        )

    assert "secret-token" not in str(failure.value)
    assert "[REDACTED]" in str(failure.value)
