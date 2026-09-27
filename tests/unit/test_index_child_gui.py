"""Work order 0x §2 in the real window: Start, Pause and Stop with the child on.

Layer: L5 (pytest-qt, `gui_mainwindow` - WORKORDER-CONVENTIONS §5b)

The acceptance for §2 is something a person clicks, so a scenario clicks it.
The real `MainWindow`, its real Indexing page and index controller, with
"Index in a separate process" switched on: pressing **Start** runs
`app.cli index --events jsonl` as a child process, its progress reaches the
page, **Pause** holds it and says so, **Resume** lets it go, **Stop** ends it,
and the page comes back ready for the next Start with no process left behind.

A second scenario makes the child die and reads what the page says.

Two things are swapped, both outside what is being tested: the child is told
to use the labelled fake embedder (`--fake-embedder-for-bench`, as no model
is downloaded here), and the run lock lives in the test's own folder
(`TMPDIR`), so a run elsewhere on the machine cannot interfere. The window is
not closed at the end - see `gui_mainwindow` for why - but closing with a
child running is `test_child_run.py`'s and `test_index_child_process.py`'s.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt                                     # noqa: E402

from app.index import child_run                                 # noqa: E402
from tests.unit.test_index_freshness import write_aged          # noqa: E402

pytestmark = pytest.mark.gui

FAKE = Path(__file__).resolve().parent / "fake_index_child.py"


@pytest.fixture()
def child_mode(gui_mainwindow, qtbot, tmp_path, monkeypatch):
    """The window with the switch on, a corpus in its folders, and the child
    pointed at the fake embedder. Everything put back afterwards: the window
    is shared by every test in this module."""
    _app, window, _store, _engine = gui_mainwindow
    qtbot.waitUntil(lambda: getattr(window, "indexing_view", None) is not None,
                    timeout=10_000)
    corpus = tmp_path / "docs"
    corpus.mkdir()
    for n in range(400):
        write_aged(corpus / f"note{n:02d}.txt",
                   f"Barnsley Dairy note {n} about the HACCP audit.\n" * 5)
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setenv("TMPDIR", str(locks))

    real_command = child_run.child_command
    monkeypatch.setattr(child_run, "child_command", lambda *a, **k: real_command(
        *a, **{**k, "extra": [*k.get("extra", ()), "--fake-embedder-for-bench"]}))

    before = window._settings
    window._settings = before.model_copy(update={
        "index_separate_process": True,
        # The machine running the suite is shared; a CPU ceiling would make
        # the child wait for it, which is not what is being tested.
        "index_cpu_percent": 0,
    })
    roots_box = window.settings_view.roots_box
    roots_before = roots_box.current_roots()
    roots_box.add_root(str(corpus))
    try:
        yield window, corpus
    finally:
        view = window.indexing_view
        if view._worker is not None:                 # noqa: SLF001
            view.stop()
            qtbot.waitUntil(lambda: view._worker is None, timeout=60_000)
        roots_box.set_roots(roots_before)             # quietly, no save
        window._settings = before


def test_start_pause_resume_and_stop_drive_the_child(child_mode, qtbot) -> None:
    from app.ui.indexing_view import PAUSE_LABEL, PAUSED_HEADLINE, RESUME_LABEL

    window, _corpus = child_mode
    view = window.indexing_view
    ticks: list = []
    view.progressed.connect(lambda state, indexed, *rest: ticks.append((state, indexed)))

    qtbot.mouseClick(view.start_button, Qt.MouseButton.LeftButton)
    # The run belongs to the page's own worker, exactly as in-process.
    qtbot.waitUntil(lambda: view._worker is not None, timeout=30_000)    # noqa: SLF001
    run = view._worker.pipeline                                            # noqa: SLF001
    assert isinstance(run, child_run.ChildIndexRun)
    assert view.is_running() and not view.start_button.isEnabled()

    # The child's progress reaches the page: documents are being counted.
    qtbot.waitUntil(lambda: any(state == "running" and indexed > 0
                                for state, indexed in ticks), timeout=90_000)
    qtbot.mouseClick(view.pause_button, Qt.MouseButton.LeftButton)
    assert view.pause_button.text() == RESUME_LABEL
    assert view.headline.text() == PAUSED_HEADLINE
    # It holds - and the page still says whose pause it is after the child's
    # own next report, which is where the words come from from now on.
    qtbot.wait(1500)
    held = ticks[-1][1]
    qtbot.wait(1000)
    assert ticks[-1][1] == held, "a paused run kept indexing"
    assert view.headline.text() == PAUSED_HEADLINE

    qtbot.mouseClick(view.pause_button, Qt.MouseButton.LeftButton)       # Resume
    assert view.pause_button.text() == PAUSE_LABEL

    qtbot.mouseClick(view.stop_button, Qt.MouseButton.LeftButton)
    assert view.headline.text() == "Stopping after the current file…"
    qtbot.waitUntil(lambda: view._worker is None, timeout=90_000)         # noqa: SLF001

    assert any(state == "finished" for state, _n in ticks), ticks[-5:]
    assert view.start_button.isEnabled() and not view.stop_button.isEnabled()
    assert not child_run.live_children(), "the indexing process outlived its run"
    assert run.returncode is not None


def test_a_child_that_dies_is_explained_on_the_page(child_mode, qtbot, monkeypatch) -> None:
    window, _corpus = child_mode
    view = window.indexing_view
    monkeypatch.setattr(child_run, "child_command",
                        lambda *a, **k: [sys.executable, str(FAKE), "crash"])
    errors: list = []
    view.error.connect(errors.append)
    # The window also puts the error in a message box (`_show_error`), which
    # would wait for a click; its words are recorded instead of shown.
    from PyQt6.QtWidgets import QMessageBox

    boxes: list = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: boxes.append(
        (box.text(), box.informativeText(), box.detailedText())) or 0)

    qtbot.mouseClick(view.start_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(errors), timeout=60_000)
    qtbot.waitUntil(lambda: view._worker is None, timeout=30_000)         # noqa: SLF001

    # Non-negotiable #2: what happened, and how to fix it - on the page.
    assert view.headline.text() == (
        "The indexing process stopped unexpectedly while reading big.pst.")
    assert "Press Start to carry on" in view.detail.text()
    assert errors[0].code == "ERR_INDEX_PROCESS_ENDED"
    qtbot.waitUntil(lambda: bool(boxes), timeout=10_000)
    text, fix, details = boxes[0]
    assert text == "The indexing process stopped unexpectedly while reading big.pst."
    assert "Press Start" in fix and "ran out of memory" in details
    assert view.start_button.isEnabled()
