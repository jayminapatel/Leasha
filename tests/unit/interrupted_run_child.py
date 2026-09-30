"""A real index run that is killed part-way. Driven by `test_interrupted_runs.py`.

Layer: test support.

Run as `python -m tests.unit.interrupted_run_child <db> <root> <lock_dir>`. It
takes the run lock exactly as `app.cli index` does, runs a real `Pipeline` over
the folder, and on the first progress tick with something indexed ends the
process with `os._exit(9)` - no `finally`, no `__exit__`, no lock release: the
same thing a power cut or Task Manager's "End task" does to the window.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(db: str, root: str, lock_dir: str) -> None:
    # The run lock this takes is the test run's own, never the machine's:
    # `lock_dir` only places a lock file on Linux and macOS, and on Windows
    # this process was taking the real index mutex. See that module.
    from tests import private_locks

    private_locks.install()

    from app.core.run_lock import COMMAND_LINE, IndexRunLock
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore
    from tests.unit.test_index_freshness import NullVectors, fake_embedder

    def die(stats) -> None:
        if stats.indexed >= 1:
            os._exit(9)

    with SqliteStore(Path(db)) as store:
        IndexRunLock(store, owner=COMMAND_LINE, lock_dir=Path(lock_dir)).acquire()
        config = PipelineConfig(walk=WalkConfig(roots=[Path(root)]), workers=1,
                                checkpoint_every=1)
        Pipeline(store, NullVectors(), fake_embedder(), config).run(on_progress=die)
    os._exit(3)                          # never reached if the kill worked


if __name__ == "__main__":
    main(*sys.argv[1:4])
