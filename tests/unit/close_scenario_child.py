"""The child process of `test_close_ends_the_app.py`. Not a test module.

Runs the real `MainWindow` with an index run in flight and two other visible
windows, closes the window inside a real event loop, and reports what happened on
stdout. It lives in its own process so that a hang - which is the very thing under
test - can be killed by the parent's wall-clock cap instead of freezing pytest
(pytest-timeout cannot interrupt a thread blocked inside Qt).

Arms `faulthandler.dump_traceback_later(DUMP_AFTER, exit=True)` first, so a hang
writes every thread's stack to stderr and ends the process by itself.

Usage: python close_scenario_child.py <scratch dir>
"""

from __future__ import annotations

import faulthandler
import math
import os
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("EMBED_DEVICE", "cpu")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DUMP_AFTER = 150
FILES = 120


def main(scratch: Path) -> int:
    faulthandler.dump_traceback_later(DUMP_AFTER, exit=True)

    # The window below probes the run lock every four seconds, by its real
    # name; with the owner's own run going it would draw somebody else's run.
    from tests import private_locks

    private_locks.install()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget

    from app.core import run_lock
    from app.core.config import load_settings
    from app.index.embedder import Embedder, l2_normalise
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.resources import ResourceGovernor, ResourceLimits, Snapshot
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow
    from app.ui.widgets.log_window import LogWindow
    from tests.unit.test_index_freshness import write_aged
    from tests.unit.test_window_opens import ENV, _Engine

    # A private index lock: a real Leasha run or another process holding the
    # machine-wide one would fail this run before it starts.
    real_lock = run_lock.IndexRunLock
    run_lock.IndexRunLock = lambda store=None, *, owner="", **_kw: real_lock(
        store, owner=owner, name=f"leasha-test-{uuid.uuid4().hex}",
        lock_dir=scratch / "locks")

    home = scratch / "app"
    home.mkdir()
    env = home / ".env"
    env.write_text(ENV.format(d=home.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    corpus = scratch / "docs"
    corpus.mkdir()
    for n in range(FILES):
        write_aged(corpus / f"doc{n:03d}.txt", f"Document number {n} about the dairy audit.")

    calls: list[int] = []

    def encode(texts):
        calls.append(len(texts))
        time.sleep(0.25)
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)]) for t in texts]

    class Vectors:
        def delete_by_file_ids(self, _ids) -> None:
            pass

        def add(self, *, chunk_ids, **_kw) -> int:
            return len(chunk_ids)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    window = MainWindow(settings, store, vectors, _Engine(store))
    window.resize(640, 480)
    window.show()

    def pump(seconds: float, until=lambda: False) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            if until():
                return True
            time.sleep(0.02)
        return until()

    if not pump(60, lambda: getattr(window, "indexing_view", None) is not None):
        print("RESULT no_indexing_view")
        return 2

    popout = QWidget()
    popout.show()
    log_window = LogWindow()
    log_window.show()

    pipeline = Pipeline(
        store, Vectors(), Embedder(dim=8, encoder=encode),
        PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1, embed_batch=64,
                       limits=ResourceLimits(pause_on_battery=False, cpu_percent=0)),
        # A probe that measures nothing: the real one walks the process table and
        # child processes on every check, which on a busy machine (thread dump,
        # 2026-09-20) kept the run from ever reaching the embedder.
        governor=ResourceGovernor(
            ResourceLimits(pause_on_battery=False, cpu_percent=0), probe=lambda: Snapshot()))
    window.indexing_view.start(pipeline)
    if not pump(90, lambda: bool(calls)):
        print("RESULT run_never_embedded")
        return 3

    ended_by_timer: list[bool] = []
    backstop = QTimer()
    backstop.setSingleShot(True)
    backstop.timeout.connect(lambda: (ended_by_timer.append(True), app.quit()))
    backstop.start(25000)
    QTimer.singleShot(0, window.close)
    began = time.monotonic()
    app.exec()
    waited = time.monotonic() - began
    backstop.stop()

    print(f"RESULT ended_by_timer={bool(ended_by_timer)} waited={waited:.1f} "
          f"calls={sum(calls)} of={FILES} "
          f"popout_visible={popout.isVisible()} log_visible={log_window.isVisible()} "
          f"pool_active={window.indexing_view.pool.activeThreadCount()}", flush=True)
    faulthandler.cancel_dump_traceback_later()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
