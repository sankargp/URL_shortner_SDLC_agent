"""Isolated Git workspaces and GitHub pull-request publication."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

import httpx


class SourceControlError(RuntimeError):
    """A safe, user-facing source-control failure."""


def _authenticated_environment(token: str) -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
            }
        )
    return environment


def _git(
    cwd: Path,
    *args: str,
    token: str = "",
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_authenticated_environment(token),
            check=check,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SourceControlError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "git command failed").strip()
        if token:
            detail = detail.replace(token, "[REDACTED]")
        raise SourceControlError(detail) from exc


def _github_repository(remote_url: str) -> str:
    if remote_url.startswith("git@github.com:"):
        path = remote_url.split(":", 1)[1]
    else:
        parsed = urlparse(remote_url)
        if parsed.hostname != "github.com":
            raise SourceControlError("the configured remote must be hosted on github.com")
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    if len(path.split("/")) != 2:
        raise SourceControlError("the configured GitHub remote URL is invalid")
    return path


@dataclass(frozen=True)
class RepositoryConfig:
    source_root: Path
    remote_name: str
    remote_url: str
    base_branch: str
    github_repository: str
    token: str
    author_name: str = "Agentic SDLC"
    author_email: str = "agentic-sdlc@users.noreply.github.com"

    @classmethod
    def discover(cls) -> RepositoryConfig:
        requested_root = Path(os.getenv("SOURCE_REPO_PATH", ".")).resolve()
        root_result = _git(requested_root, "rev-parse", "--show-toplevel")
        source_root = Path(root_result.stdout.strip()).resolve()
        remotes = [line for line in _git(source_root, "remote").stdout.splitlines() if line]
        requested_remote = os.getenv("GIT_REMOTE_NAME")
        if requested_remote:
            if requested_remote not in remotes:
                raise SourceControlError(f"Git remote '{requested_remote}' does not exist")
            remote_name = requested_remote
        elif "origin" in remotes:
            remote_name = "origin"
        elif len(remotes) == 1:
            remote_name = remotes[0]
        else:
            raise SourceControlError("set GIT_REMOTE_NAME when the repository has multiple remotes")
        remote_url = _git(source_root, "remote", "get-url", remote_name).stdout.strip()
        base_branch = os.getenv("GIT_BASE_BRANCH") or _git(
            source_root, "branch", "--show-current"
        ).stdout.strip()
        if not base_branch:
            raise SourceControlError("set GIT_BASE_BRANCH when the source repository is detached")
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            raise SourceControlError("GITHUB_TOKEN is not set")
        return cls(
            source_root=source_root,
            remote_name=remote_name,
            remote_url=remote_url,
            base_branch=base_branch,
            github_repository=_github_repository(remote_url),
            token=token,
            author_name=os.getenv("GIT_AUTHOR_NAME", "Agentic SDLC"),
            author_email=os.getenv(
                "GIT_AUTHOR_EMAIL", "agentic-sdlc@users.noreply.github.com"
            ),
        )


@dataclass(frozen=True)
class Checkout:
    path: Path
    branch: str
    base_branch: str
    base_sha: str


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "change"


class GitWorkspace:
    def __init__(self, config: RepositoryConfig):
        self.config = config

    def checkout(self, run_root: Path, *, requirement_id: str, run_id: str) -> Checkout:
        run_root.mkdir(parents=True, exist_ok=True)
        destination = run_root / "repository"
        branch = f"agentic/{_slug(requirement_id)}-{_slug(run_id)[:8]}"
        local_sha = _git(
            self.config.source_root, "rev-parse", self.config.base_branch
        ).stdout.strip()
        remote_result = _git(
            self.config.source_root,
            "ls-remote",
            self.config.remote_url,
            f"refs/heads/{self.config.base_branch}",
            token=self.config.token,
        ).stdout.strip()
        remote_sha = remote_result.split()[0] if remote_result else ""
        if local_sha != remote_sha:
            raise SourceControlError(
                f"local base {self.config.base_branch} does not match remote"
            )
        if not destination.exists():
            _git(
                run_root,
                "clone",
                "--no-hardlinks",
                "--branch",
                self.config.base_branch,
                "--single-branch",
                str(self.config.source_root),
                str(destination),
            )
            _git(destination, "remote", "set-url", "origin", self.config.remote_url)
            _git(destination, "switch", "-c", branch)
        elif not (destination / ".git").exists():
            raise SourceControlError("the run repository path is not a Git clone")
        else:
            _git(destination, "remote", "set-url", "origin", self.config.remote_url)
            current_branch = _git(destination, "branch", "--show-current").stdout.strip()
            if current_branch == self.config.base_branch:
                branch_exists = (
                    _git(
                        destination,
                        "show-ref",
                        "--verify",
                        f"refs/heads/{branch}",
                        check=False,
                    ).returncode
                    == 0
                )
                if branch_exists:
                    _git(destination, "switch", branch)
                else:
                    _git(destination, "switch", "-c", branch)
            elif current_branch != branch:
                raise SourceControlError("the run repository is on an unexpected branch")
        return Checkout(destination.resolve(), branch, self.config.base_branch, local_sha)


class PullRequestClient(Protocol):
    def find_or_create_pull_request(self, **request: Any) -> dict[str, Any]: ...


class GitHubClient:
    def __init__(self, config: RepositoryConfig, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {config.token}",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SourceControlError(f"GitHub request failed: {type(exc).__name__}") from exc
        if response.is_error:
            try:
                message = response.json().get("message", "request rejected")
            except (ValueError, AttributeError):
                message = "request rejected"
            if self.config.token:
                message = str(message).replace(self.config.token, "[REDACTED]")
            raise SourceControlError(f"GitHub returned {response.status_code}: {message}")
        return response.json()

    def find_or_create_pull_request(self, **request: Any) -> dict[str, Any]:
        owner = self.config.github_repository.split("/", 1)[0]
        existing = self._request(
            "GET",
            f"/repos/{self.config.github_repository}/pulls",
            params={
                "state": "open",
                "head": f"{owner}:{request['head']}",
                "base": request["base"],
            },
        )
        if existing:
            return existing[0]
        payload = {**request, "draft": False, "maintainer_can_modify": True}
        return self._request(
            "POST", f"/repos/{self.config.github_repository}/pulls", json=payload
        )


class GitPublisher:
    _SECRET_PARTS: ClassVar[set[str]] = {".env", ".git", "secrets", "credentials"}

    def __init__(self, config: RepositoryConfig, github: PullRequestClient):
        self.config = config
        self.github = github

    @classmethod
    def _validate_paths(cls, checkout: Checkout, changed_files: list[str]) -> list[str]:
        validated: list[str] = []
        root = checkout.path.resolve()
        for value in changed_files:
            path = PurePosixPath(value.replace("\\", "/"))
            lowered = [part.lower() for part in path.parts]
            candidate = (root / Path(*path.parts)).resolve()
            unsafe = (
                path.is_absolute()
                or ".." in path.parts
                or candidate != root and root not in candidate.parents
                or any(part in cls._SECRET_PARTS or "token" in part for part in lowered)
            )
            if unsafe:
                raise SourceControlError(f"unsafe changed path: {value}")
            validated.append(path.as_posix())
        return validated

    @staticmethod
    def _body(requirement: dict[str, Any], run_id: str, files: list[str], verification: Any) -> str:
        acceptance = requirement.get("acceptance") or []
        criteria = "\n".join(f"- {item}" for item in acceptance) or "- None provided"
        changed = "\n".join(f"- `{item}`" for item in files) or "- No file changes"
        return (
            f"## Requirement\n\n{requirement.get('intent', '')}\n\n"
            f"## Acceptance criteria\n\n{criteria}\n\n"
            f"## Changed files\n\n{changed}\n\n"
            f"## Verification\n\n```json\n{json.dumps(verification, indent=2)}\n```\n\n"
            f"Governance run: `{run_id}`"
        )

    def publish(
        self,
        checkout: Checkout,
        *,
        requirement: dict[str, Any],
        run_id: str,
        changed_files: list[str],
        verification: Any,
    ) -> dict[str, Any]:
        files = self._validate_paths(checkout, changed_files)
        if files:
            _git(checkout.path, "add", "--", *files)
        staged = _git(checkout.path, "diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            _git(checkout.path, "config", "user.name", self.config.author_name)
            _git(checkout.path, "config", "user.email", self.config.author_email)
            _git(
                checkout.path,
                "commit",
                "-m",
                f"feat({requirement['id']}): {requirement['title']}",
            )
        ahead = int(
            _git(
                checkout.path,
                "rev-list",
                "--count",
                f"{checkout.base_branch}..HEAD",
            ).stdout.strip()
        )
        if ahead == 0:
            return {
                "outcome": "no_changes",
                "branch": checkout.branch,
                "base_branch": checkout.base_branch,
            }
        _git(
            checkout.path,
            "push",
            "--set-upstream",
            "origin",
            checkout.branch,
            token=self.config.token,
        )
        head_sha = _git(checkout.path, "rev-parse", "HEAD").stdout.strip()
        pull_request = self.github.find_or_create_pull_request(
            title=f"[{requirement['id']}] {requirement['title']}",
            body=self._body(requirement, run_id, files, verification),
            head=checkout.branch,
            base=checkout.base_branch,
        )
        return {
            "outcome": "pr_opened",
            "branch": checkout.branch,
            "base_branch": checkout.base_branch,
            "head_sha": head_sha,
            "pr_number": pull_request["number"],
            "pr_url": pull_request["html_url"],
        }
