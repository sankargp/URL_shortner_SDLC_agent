"""Deterministic requirement profiles used by offline SDLC demonstrations."""
from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DemoProfile:
    name: str
    architecture: dict[str, Any]
    tags: tuple[str, ...]
    capabilities: tuple[str, ...]
    test_node_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProfileResolution:
    profile: DemoProfile | None
    error: str | None = None


_CORE_TESTS = (
    "target-app/tests/test_shortener.py::test_clean_database_shorten_and_redirect",
    "target-app/tests/test_shortener.py::test_stats_increment",
    "target-app/tests/test_shortener.py::test_invalid_url_rejected",
)
# Public alias: the regression suite every requirement's acceptance tests run
# alongside, including dynamically generated ones with no matching profile.
CORE_TEST_NODE_IDS = _CORE_TESTS

_PROFILES = {
    "core_shortener": DemoProfile(
        name="core_shortener",
        architecture={
            "components": [
                "API layer (FastAPI)",
                "code generator (base62)",
                "persistence (SQLite)",
                "analytics (click events)",
            ],
            "endpoints": ["POST /shorten", "GET /{code}", "GET /{code}/stats"],
            "regression_risks": ["generated codes must remain unique"],
        },
        tags=(),
        capabilities=("core_shortener",),
        test_node_ids=_CORE_TESTS,
    ),
    "alias_expiry": DemoProfile(
        name="alias_expiry",
        architecture={
            "impacted_modules": ["target-app/main.py"],
            "schema_changes": ["links.alias", "links.expires_at"],
            "api_changes": ["POST /shorten: optional custom_alias and expiry_days"],
            "regression_risks": ["existing short codes must remain valid"],
        },
        tags=("schema_change",),
        capabilities=("custom_alias", "link_expiry"),
        test_node_ids=_CORE_TESTS
        + ("target-app/tests/test_shortener.py::test_custom_alias_and_expiry",),
    ),
    "password_protection": DemoProfile(
        name="password_protection",
        architecture={
            "impacted_modules": ["target-app/main.py"],
            "schema_changes": ["links.password_salt", "links.password_hash"],
            "api_changes": [
                "POST /shorten: optional password",
                "GET /{code}: optional X-Link-Password header",
            ],
            "security_controls": [
                "PBKDF2-HMAC-SHA256 password derivation",
                "constant-time password comparison",
                "no plaintext password persistence",
            ],
            "regression_risks": ["unprotected links must redirect unchanged"],
        },
        tags=("schema_change", "security_sensitive"),
        capabilities=("password_protection",),
        test_node_ids=_CORE_TESTS
        + (
            "target-app/tests/test_shortener.py::test_protected_link_requires_correct_password",
            "target-app/tests/test_shortener.py::test_password_is_not_persisted_in_plaintext",
        ),
    ),
    "bulk_idempotency": DemoProfile(
        name="bulk_idempotency",
        architecture={
            "impacted_modules": ["target-app/main.py"],
            "schema_changes": ["idempotency_requests table"],
            "api_changes": ["POST /shorten/batch with Idempotency-Key"],
            "reliability_controls": [
                "stable replay after restart",
                "request-payload conflict detection",
                "ordered item-level results",
            ],
            "regression_risks": ["POST /shorten must remain backward compatible"],
        },
        tags=("schema_change",),
        capabilities=("idempotent_batch",),
        test_node_ids=_CORE_TESTS
        + (
            "target-app/tests/test_shortener.py::test_batch_retry_is_idempotent",
            "target-app/tests/test_shortener.py::test_batch_mixed_results_preserve_order",
            "target-app/tests/test_shortener.py::test_batch_rejects_changed_payload_for_key",
        ),
    ),
    "link_preview": DemoProfile(
        name="link_preview",
        architecture={
            "impacted_modules": ["target-app/main.py"],
            "api_changes": ["GET /{code}/preview: read-only link metadata, no redirect or click increment"],
            "regression_risks": ["preview must not redirect or mutate link state"],
        },
        tags=(),
        capabilities=("link_preview",),
        test_node_ids=_CORE_TESTS
        + (
            "target-app/tests/test_shortener.py::test_preview_returns_link_metadata",
            "target-app/tests/test_shortener.py::test_preview_does_not_redirect_or_increment_clicks",
            "target-app/tests/test_shortener.py::test_preview_unknown_code_returns_404",
        ),
    ),
    "ambiguous_reliability": DemoProfile(
        name="ambiguous_reliability",
        architecture={
            "status": "blocked_pending_interpretation",
            "decision_required": "choose a reliability interpretation",
        },
        tags=("ambiguity",),
        capabilities=(),
        test_node_ids=(),
    ),
}


def _search_text(requirement: dict[str, Any]) -> str:
    text = " ".join(
        str(requirement.get(key, "")) for key in ("title", "intent")
    )
    return unicodedata.normalize("NFKC", text).casefold()


def _matchers(requirement: dict[str, Any]) -> tuple[Callable[[str], bool], ...]:
    requirement_type = str(requirement.get("type", "")).casefold()
    return (
        lambda text: requirement_type == "greenfield",
        lambda text: "alias" in text and "expir" in text,
        lambda text: "password" in text or "protected" in text,
        lambda text: ("bulk" in text or "batch" in text) and "idempoten" in text,
        lambda text: "preview" in text,
        lambda text: requirement_type == "ambiguous",
    )


def resolve_demo_profile(
    requirement: dict[str, Any],
    mode: str | None = None,
) -> ProfileResolution:
    """Resolve exactly one deterministic profile, or return a truthful error."""
    profile_names = tuple(_PROFILES)
    text = _search_text(requirement)
    matches = [
        _PROFILES[name]
        for name, matcher in zip(profile_names, _matchers(requirement), strict=True)
        if matcher(text)
    ]
    if len(matches) == 1:
        return ProfileResolution(profile=matches[0])
    if len(matches) > 1:
        return ProfileResolution(profile=None, error="ambiguous_demo_profile")

    selected_mode = (mode or os.getenv("LLM_MODE", "mock")).casefold()
    if selected_mode == "live":
        return ProfileResolution(profile=None)
    return ProfileResolution(profile=None, error="unsupported_demo_profile")


def get_demo_profile(name: str) -> DemoProfile:
    try:
        return _PROFILES[name]
    except KeyError as exc:
        raise KeyError(f"Unknown demo profile: {name}") from exc
