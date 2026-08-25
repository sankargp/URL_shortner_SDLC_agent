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
from .base import Agent
from .requirements import RequirementsAgent
from .architect import ArchitectAgent
from .implementer import ImplementerAgent
from .tester import TesterAgent
from .docs import DocsAgent
from .release import ReleaseAgent
from .planner import Planner

_REGISTRY: dict[str, Agent] = {
    "requirements": RequirementsAgent(),
    "architect": ArchitectAgent(),
    "implementer": ImplementerAgent(),
    "tester": TesterAgent(),
    "docs": DocsAgent(),
    "release": ReleaseAgent(),
}


class _Registry:
    def get(self, name: str) -> Agent:
        if name not in _REGISTRY:
            raise KeyError(f"No agent registered for '{name}'")
        return _REGISTRY[name]


registry = _Registry()

__all__ = ["Agent", "Planner", "registry"]
