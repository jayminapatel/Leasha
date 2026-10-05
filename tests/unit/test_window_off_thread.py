r"""Two small jobs the window did on its own thread. 2026-10-04, code review.

Layer: L5

The logs and sessions folders' Open made the folder and launched Explorer on
the interface thread, and a drop on the window stat'ed every dropped path
there - each a wait on a sleeping drive. Both go to a worker now; these drive
them with the launchers replaced, so nothing here opens anything.
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _pump() -> None:
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    for _ in range(5):
        QThreadPool.globalInstance().waitForDone(3_000)
        app.processEvents()


def test_dropped_paths_become_folders_on_a_worker(tmp_path):
    from app.ui.tasks import dropped_roots

    folder = tmp_path / "docs"
    folder.mkdir()
    note = folder / "note.txt"
    note.write_text("x", encoding="utf-8")
    assert dropped_roots([str(folder), str(note), str(tmp_path / "gone" / "a.txt")]) == sorted(
        {str(folder), str(tmp_path / "gone")})


def test_a_drop_on_the_window_is_sorted_out_off_its_thread(tmp_path, monkeypatch):
    from PySide6.QtCore import QMimeData, QUrl

    from app.ui import shell, tasks

    seen: list = []
    real = tasks.dropped_roots
    monkeypatch.setattr(tasks, "dropped_roots", lambda paths: seen.append(
        threading.current_thread() is threading.main_thread()) or real(paths))
    started: list = []
    window = SimpleNamespace(_index_dropped=started.append)
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    event = SimpleNamespace(mimeData=lambda: data, acceptProposedAction=lambda: None)

    shell.MainWindow.dropEvent(window, event)
    _pump()

    assert seen == [False], "the stat ran on a worker"
    assert [list(map(os.path.normcase, roots)) for roots in started] == [
        [os.path.normcase(str(tmp_path))]]


def test_the_logs_folder_is_made_and_opened_through_the_route(tmp_path, monkeypatch):
    from app.ui import tasks
    from app.ui.widgets import environment_box

    made: list = []
    real = tasks.make_folder
    monkeypatch.setattr(environment_box, "make_folder", lambda folder: made.append(
        threading.current_thread() is threading.main_thread()) or real(folder))
    opened: list = []
    monkeypatch.setattr(environment_box, "open_async",
                        lambda path, **kwargs: opened.append(path))
    box = environment_box.EnvironmentBox(SimpleNamespace(log_path=str(tmp_path / "logs")))

    box._open_folder(tmp_path / "logs" / "sessions")
    _pump()

    assert made == [False], "made on a worker"
    assert (tmp_path / "logs" / "sessions").is_dir()
    assert opened == [str(tmp_path / "logs" / "sessions")]
    box.close()


def test_a_folder_that_cannot_be_made_says_why(tmp_path):
    from app.ui.tasks import make_folder

    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert make_folder(blocker / "inside").startswith("Could not open ")
    assert make_folder(tmp_path / "fine") == ""
