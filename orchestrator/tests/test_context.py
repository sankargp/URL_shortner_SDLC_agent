"""Concurrency tests for run persistence."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from orchestrator.context import RunStore


def test_record_lineage_serializes_read_modify_write(tmp_path, monkeypatch):
    store = RunStore("run-concurrent", str(tmp_path))
    original_dump = store._dump

    def delayed_dump(name, value):
        time.sleep(0.05)
        original_dump(name, value)

    monkeypatch.setattr(store, "_dump", delayed_dump)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda node: store.record_lineage(
                    artifact=f"{node}.txt",
                    from_requirement="REQ-001",
                    node=node,
                    rationale="concurrent output",
                ),
                ("docs", "unit"),
            )
        )

    assert {entry["produced_by_node"] for entry in store._load("lineage.json", [])} == {
        "docs",
        "unit",
    }
