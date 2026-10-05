r"""The Indexing page's per-reader lines and heartbeat, pressed for real.

Layer: L5

Work order 0x §4d, on top of §3's presenter. Driven through the real
`IndexingView` and its own `_on_progress`/`_on_finished`/`_on_failed`, as
`WORKORDER-CONVENTIONS.md` §5b asks, rather than by calling the panel alone.
What these pin:

* two busy readers show as two lines, with their place inside the archive,
  and the "now" line above the counts carries §3's headline;
* the heartbeat says "working" and, once nothing has moved for
  `QUIET_AFTER_S`, turns into the calm warning **on its own**, from the panel's
  one-second redraw, with no new tick - the case it exists for;
* a finished run, a failed run and a stop leave no lines and no timer;
* a hidden page runs no timer, and showing it again starts it.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from app.index import pipeline as pipeline_module  # noqa: E402
from app.index.pipeline import IndexStats  # noqa: E402
from app.ui.presenter.live_progress import QUIET_AFTER_S  # noqa: E402

pytestmark = pytest.mark.gui


def _view(qtbot):
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    qtbot.waitExposed(view)
    return view


def _reading(*, last_activity: float) -> IndexStats:
    """A snapshot with two busy readers and one waiting, as a run sends it."""
    snap = IndexStats(phase=pipeline_module.PHASE_READING, walk_complete=True,
                      seen=10, indexed=4).snapshot()
    now = time.time()
    snap.workers = {
        "1": {"file": "Archive2019.pst", "path": "D:/Mail/Archive2019.pst",
              "started_at": now - 200, "stage": "reading", "item": 4511,
              "inner": [{"kind": "pst", "name": "Archive2019.pst", "unit": "message",
                         "n": 4512, "total": 18300, "where": "Inbox/Projects",
                         "stage": "messages", "detail": ""}]},
        "2": {"file": "report.docx", "path": "D:/Docs/report.docx",
              "started_at": now - 2, "stage": "reading", "item": 0, "inner": []},
        "3": {"file": "", "path": "", "started_at": 0.0, "stage": "", "item": 0,
              "inner": []},
    }
    snap.last_activity = last_activity
    snap.embed_batch, snap.embed_batches = 3, 12
    return snap


def _tick(view, stats) -> None:
    view._last_paint = 0.0                  # past the paint throttle, as a new tick
    view._on_progress(stats)


def _layout_holding(widget):
    """The layout `widget` was added to, however deeply it is nested."""
    pending = [widget.parentWidget().layout()]
    while pending:
        layout = pending.pop()
        if layout.indexOf(widget) >= 0:
            return layout
        pending.extend(child for child in (layout.itemAt(i).layout() for i in range(layout.count()))
                       if child is not None)
    raise AssertionError("the widget is in no layout")


def test_two_readers_show_as_two_lines_under_the_bar(qtbot) -> None:
    view = _view(qtbot)
    panel = view.workers_panel
    assert not panel.isVisible() and not panel.beating, "nothing before a run"

    _tick(view, _reading(last_activity=time.time()))

    assert panel.isVisible() and panel.beating
    lines = panel.lines.text().split("\n")
    assert lines[0].startswith(
        "Reader 1: Archive2019.pst › Inbox/Projects › message 4,512 of 18,300 · 3 min")
    assert lines[1].startswith("Reader 2: report.docx · ")
    assert lines[2] == "Reader 3: waiting for the next file"
    assert panel.heartbeat.text().startswith("Working · last activity")
    assert panel.writer.text() == "Making text searchable by meaning, batch 3 of 12"
    # §4b's slot now carries §3's headline.
    assert view.now_line.isVisible()
    assert view.now_line.text() == (
        "Reading Archive2019.pst › Inbox/Projects — message 4,512 of 18,300")
    # Under the detail line, as the layout promises.
    # 2026-10-05: the Status page became two columns that day, so the two sit
    # in the run column - a layout inside the page's own. This asked the page's
    # own layout, which holds neither, and compared -1 with 0.
    layout = _layout_holding(view.detail)
    assert layout.indexOf(panel) == layout.indexOf(view.detail) + 1


def test_the_heartbeat_turns_quiet_by_itself_without_a_new_tick(qtbot) -> None:
    view = _view(qtbot)
    panel = view.workers_panel
    # Two seconds short of the threshold: "working" now, the warning within
    # the next couple of one-second redraws - with no tick in between.
    _tick(view, _reading(last_activity=time.time() - (QUIET_AFTER_S - 2)))
    assert panel.heartbeat.text().startswith("Working")
    assert not panel.heartbeat.property("quiet")

    qtbot.waitUntil(lambda: panel.heartbeat.text().startswith("No progress for"),
                    timeout=5000)
    assert panel.heartbeat.property("quiet") is True
    assert "OCR" in panel.heartbeat.text()


def test_a_finished_run_leaves_no_lines_and_no_timer(qtbot) -> None:
    view = _view(qtbot)
    _tick(view, _reading(last_activity=time.time()))
    view._on_finished(IndexStats(phase=pipeline_module.PHASE_WORD_INDEX).snapshot())
    panel = view.workers_panel
    assert not panel.isVisible() and not panel.beating
    assert panel.lines.text() == "" and panel.heartbeat.text() == ""


def test_a_failed_run_leaves_no_lines_and_no_timer(qtbot) -> None:
    view = _view(qtbot)
    _tick(view, _reading(last_activity=time.time()))
    view._on_failed(SimpleNamespace(message="The index could not be opened.",
                                    suggestion="Check the disk."))
    assert not view.workers_panel.isVisible() and not view.workers_panel.beating


def test_a_stop_under_way_clears_the_lines(qtbot) -> None:
    view = _view(qtbot)
    _tick(view, _reading(last_activity=time.time()))
    view._stopping = True
    _tick(view, _reading(last_activity=time.time()))
    assert not view.workers_panel.isVisible() and not view.workers_panel.beating
    assert view.headline.text() == "Stopping after the current file…"


def test_a_hidden_page_runs_no_timer(qtbot) -> None:
    view = _view(qtbot)
    panel = view.workers_panel
    _tick(view, _reading(last_activity=time.time()))
    assert panel.beating
    view.hide()
    assert not panel.beating
    view.show()
    qtbot.waitExposed(view)
    assert panel.beating


def test_an_older_snapshot_without_readers_keeps_the_old_words(qtbot) -> None:
    """A stats object with no `workers` at all (a run from before §3, or a
    record from another process) keeps the 0w sentence and shows no panel."""
    from app.ui.presenter.activity import READING_WORDS

    view = _view(qtbot)
    old = SimpleNamespace(phase="reading", paused=False, walk_complete=True,
                          seen=3, indexed=1, skipped=0, chunks=0, skipped_by_code={},
                          skipped_roots=(), notices=[], activity=None,
                          ocr_mode="both", files_per_minute=0.0,
                          recent_files_per_minute=None)
    _tick(view, old)
    assert view.now_line.text() == READING_WORDS
    assert not view.workers_panel.isVisible()
