"""Specialist agents + a simple registry.

Each agent has a bounded role and a uniform contract:
    run(node, run, context, store) -> dict with keys:
        artifact (str, optional)      : relative path of a produced artifact
        rationale (str)               : why this output was produced (lineage)
        exit_ok (bool)                : did the node satisfy its exit gate
        reason (str, optional)        : failure reason if exit_ok is False
        context_updates (dict)        : cross-stage context to persist
        tags (list, optional)         : hints used for approval routing
"""
from .architect import ArchitectAgent
from .base import Agent
from .docs import DocsAgent
from .implementer import ImplementerAgent
from .planner import Planner
from .release import ReleaseAgent
from .requirements import RequirementsAgent
from .source_control import CheckoutAgent, PublisherAgent
from .tester import TesterAgent

_REGISTRY: dict[str, Agent] = {
    "requirements": RequirementsAgent(),
    "architect": ArchitectAgent(),
    "implementer": ImplementerAgent(),
    "tester": TesterAgent(),
    "docs": DocsAgent(),
    "release": ReleaseAgent(),
    "checkout": CheckoutAgent(),
    "publisher": PublisherAgent(),
}


class _Registry:
    def get(self, name: str) -> Agent:
        if name not in _REGISTRY:
            raise KeyError(f"No agent registered for '{name}'")
        return _REGISTRY[name]


registry = _Registry()

__all__ = ["Agent", "Planner", "registry"]
