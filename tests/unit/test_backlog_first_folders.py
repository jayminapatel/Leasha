r"""The start-of-run vector backlog is asked for in the run's own order.

Layer: L3

2026-10-10, indexing review S2. `SqliteStore.unembedded_by_file` learned to put
the `--first` folders first and then the newest files (`test_backlog_order.py`);
this pins that the pipeline passes it the run's `--first` folders, so a person
who said "my current project first" gets meaning for it first too.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from app.index import pipeline as module


class _Store:
    def __init__(self) -> None:
        self.asked: list[dict] = []

    def unembedded_by_file(self, **kwargs):
        self.asked.append(kwargs)
        return []


def test_the_backlog_is_asked_for_with_the_first_folders():
    store = _Store()
    built = module.Pipeline.__new__(module.Pipeline)
    built.store = store
    built.config = SimpleNamespace(
        embed_batch=64,
        walk=SimpleNamespace(priority_roots=[Path("D:/Current"), Path("D:/Next")]))
    built._stop = threading.Event()
    built._interrupted = False
    built._text_first = lambda: True
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger

    built._drain_unembedded(SimpleNamespace(enrichment_counts={}))

    assert store.asked == [{"batch_size": 64,
                            "first_folders": [str(Path("D:/Current")), str(Path("D:/Next"))]}]
