"""Agents for isolated repository checkout and pull-request publication."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.source_control import (
    Checkout,
    GitHubClient,
    GitPublisher,
    GitWorkspace,
    RepositoryConfig,
)

from .base import Agent


class CheckoutAgent(Agent):
    name = "checkout"

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        config = RepositoryConfig.discover()
        checkout = GitWorkspace(config).checkout(
            store.root,
            requirement_id=run.requirement_id,
            run_id=run.id,
        )
        repository = {
            "path": str(checkout.path),
            "branch": checkout.branch,
            "base_branch": checkout.base_branch,
            "base_sha": checkout.base_sha,
            "github_repository": config.github_repository,
        }
        artifact = store.write_artifact(
            "source-control/checkout.json", json.dumps(repository, indent=2)
        )
        return {
            "artifact": artifact,
            "rationale": "Created an isolated clone from the configured repository base.",
            "exit_ok": True,
            "context_updates": {"repository": repository},
        }


class PublisherAgent(Agent):
    name = "publisher"

    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        config = RepositoryConfig.discover()
        repository = context["repository"]
        checkout = Checkout(
            path=Path(repository["path"]),
            branch=repository["branch"],
            base_branch=repository["base_branch"],
            base_sha=repository["base_sha"],
        )
        publication = GitPublisher(config, GitHubClient(config)).publish(
            checkout,
            requirement=context["requirement"],
            run_id=run.id,
            changed_files=list(context.get("changed_files") or []),
            verification=context.get("test_report") or {},
        )
        artifact = store.write_artifact(
            "source-control/publication.json", json.dumps(publication, indent=2)
        )
        rationale = (
            f"Opened pull request {publication['pr_url']}."
            if publication["outcome"] == "pr_opened"
            else "The requirement was already satisfied; no pull request was necessary."
        )
        return {
            "artifact": artifact,
            "rationale": rationale,
            "exit_ok": True,
            "context_updates": {"publication": publication},
        }
