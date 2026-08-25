"""Agent base class + LLM abstraction with mock/replay/live modes.

The LLM call is isolated here so the entire pipeline runs offline (LLM_MODE=mock)
for reliable demos, replays cached outputs deterministically (replay), or calls a
real provider (live). Agents never call providers directly.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any


def llm(prompt: str, *, system: str = "", cache_key: str | None = None, max_tokens: int = 1024) -> str:
    """Return model text. Falls back safely when no provider/key is available."""
    mode = os.getenv("LLM_MODE", "mock").lower()
    if mode == "live":
        try:
            return _live_call(prompt, system, max_tokens)
        except Exception as exc:
            # Never let a provider hiccup break the pipeline during a demo, but
            # keep the failure reason so callers can detect/report a fallback.
            return f"[live-fallback] {type(exc).__name__}: {exc}"
    # mock / replay: deterministic, offline.
    return f"[{mode}] {system[:40]} :: {prompt[:120]}"


def _live_call(prompt: str, system: str, max_tokens: int) -> str:
    provider = os.getenv("LLM_PROVIDER", "anthropic")
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is not set")
    if provider == "anthropic":
        import anthropic  # imported lazily; only needed in live mode
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=os.getenv("LLM_MODEL", "claude-3-5-sonnet-latest"),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")
    import openai
    client = openai.OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o"),
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content or ""


class Agent(ABC):
    name: str = "agent"
    system_prompt: str = ""

    @abstractmethod
    def run(self, *, node, run, context: dict[str, Any], store) -> dict[str, Any]:
        """Execute the node's work and return the standard agent result dict."""
        raise NotImplementedError
