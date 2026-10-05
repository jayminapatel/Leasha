r"""The Indexing page's Force skip, pressed for real (work order 0z lane B).

Layer: L5

Driven through the real `IndexingView` and its own `_on_progress`, as
`test_indexing_workers_panel.py` does. What these pin:

* one Force skip button per **busy** reader, named by the reader's number, and
  none for a reader waiting for its next file;
* pressing one asks the run to skip that reader's file (`force_skip("2")`) and
  nothing else - no I/O on the UI thread - and the button stays disabled while
  the same file is shown, so a second press cannot land on the next file;
* the reader moving on to another file brings the button back;
* a finished run leaves no buttons.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from app.index import pipeline as pipeline_module  # noqa: E402
from app.index.pipeline import IndexStats  # noqa: E402

pytestmark = pytest.mark.gui


class _Run:
    """The pipeline as the page sees it: only `force_skip` matters here."""

    def __init__(self) -> None:
        self.skipped: list[str] = []

    def force_skip(self, reader: str) -> bool:
        self.skipped.append(reader)
        return True


def _view(qtbot):
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    qtbot.waitExposed(view)
    return view


#: When each file was opened. Fixed, as a real run's are: a reader's line keeps
#: the `started_at` it began the file with for as long as it reads it.
T0 = time.time()


def _reading(second_file: str = "report.pdf") -> IndexStats:
    snap = IndexStats(phase=pipeline_module.PHASE_READING, walk_complete=True,
                      seen=10, indexed=4).snapshot()
    now = T0
    snap.workers = {
        "1": {"file": "Archive2019.pst", "path": "D:/Mail/Archive2019.pst",
              "started_at": now - 200, "stage": "reading", "item": 3, "inner": []},
        "2": {"file": second_file, "path": f"D:/Docs/{second_file}",
              "started_at": now - 30 if second_file == "report.pdf" else now - 1,
              "stage": "reading", "item": 0, "inner": []},
        "3": {"file": "", "path": "", "started_at": 0.0, "stage": "", "item": 0,
              "inner": []},
    }
    snap.last_activity = now
    return snap


def _tick(view, stats) -> None:
    view._last_paint = 0.0
    view._on_progress(stats)


def test_one_button_per_busy_reader_and_a_press_skips_that_readers_file(qtbot) -> None:
    from app.ui.widgets.indexing_workers import FORCE_SKIP_LABEL

    view = _view(qtbot)
    run = _Run()
    view._worker = SimpleNamespace(pipeline=run)
    panel = view.workers_panel

    _tick(view, _reading())

    shown = {key: button for key, button in panel.skip_buttons.items()
             if button.isVisible()}
    assert sorted(shown) == ["1", "2"], "reader 3 is waiting: nothing to skip"
    assert shown["2"].text() == FORCE_SKIP_LABEL.format(n="2") == "Force skip reader 2"
    assert "report.pdf" in shown["2"].toolTip()

    shown["2"].click()
    assert run.skipped == ["2"]
    assert not shown["2"].isEnabled() and shown["1"].isEnabled()

    # The same file still showing: still disabled, and a click does nothing.
    _tick(view, _reading())
    assert not panel.skip_buttons["2"].isEnabled()
    panel.skip_buttons["2"].click()
    assert run.skipped == ["2"]

    # The reader has moved on: its button is back, for the new file.
    _tick(view, _reading(second_file="minutes.docx"))
    assert panel.skip_buttons["2"].isEnabled()
    assert "minutes.docx" in panel.skip_buttons["2"].toolTip()


def test_a_finished_run_leaves_no_buttons(qtbot) -> None:
    view = _view(qtbot)
    view._worker = SimpleNamespace(pipeline=_Run())
    _tick(view, _reading())
    assert view.workers_panel.skip_row.isVisible()

    view.workers_panel.clear()
    assert not view.workers_panel.skip_row.isVisible()
    assert not any(b.isVisible() for b in view.workers_panel.skip_buttons.values())


def test_no_run_means_force_skip_does_nothing(qtbot) -> None:
    from app.ui.widgets.indexing_controls import force_skip_reader

    view = _view(qtbot)
    view._worker = None
    assert force_skip_reader(view, "1") is False


def test_force_skip_buttons_carry_an_icon_and_sit_in_reader_order(qtbot) -> None:
    """Owner, 2026-10-05: "the force skip needs icons too". The row also read
    "1, 3, 4, 2", the order readers first got a file."""
    from app.ui.widgets.indexing_workers import IndexingWorkers

    panel = IndexingWorkers()
    qtbot.addWidget(panel)
    for keys in (["1", "3"], ["1", "3", "4", "2"]):
        panel._show_skips({k: {"file": f"f{k}.md", "started_at": 1.0} for k in keys})
    layout = panel._skip_layout
    shown = [layout.itemAt(i).widget() for i in range(layout.count())
             if layout.itemAt(i).widget() is not None]
    assert [b.text() for b in shown] == [
        "Force skip reader 1", "Force skip reader 2",
        "Force skip reader 3", "Force skip reader 4"]
    for button in shown:
        assert button.property("buttonIcon") == "skip-forward"
        assert not button.icon().isNull()
