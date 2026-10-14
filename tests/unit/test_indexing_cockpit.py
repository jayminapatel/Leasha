r"""The Indexing cockpit: fixed geometry, a row per thread, and the words behind it.

Layer: L5

The owner, 2026-10-14: *"the index page is not the most user friendly as a
dashboard for indexing, the sizes of boxes change on content etc it is
disturbing for the eye ... That should be the cockpit for indexing"*, *"any
thing the threads are executing should be visible on the indexing status page
nothing silent"*, and *"has to have option to skip the current file for the
thread ... if more threads that list has to be dynamic"*. What these pin:

* **no reflow** - nothing on the Status shelf moves or changes size between an
  idle page, an empty snapshot, huge numbers, very long file names, eight
  readers, a paused run, a finished one and a failed one;
* the lanes table grows and shrinks with the readers (4 -> 6 -> 2), reuses its
  rows, and each skip button skips its own reader's file;
* the lane rows come from `stats.lanes` in a fixed order, a missing lane or
  key is an idle row of "-", and "Pictures beside meaning" appears in place;
* the state pill follows the run, and a failure stays until the next start;
* the System log tab reads the log file on a worker, only while it is shown;
* the presenter's words and numbers, without a window.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.ui.presenter import cockpit as cp

# ---------------------------------------------------------------------------
# The presenter, without Qt
# ---------------------------------------------------------------------------


def _stats(**values):
    base = dict(phase="reading", paused=False, paused_by_person=False, indexed=0,
                seen=0, skipped=0, unchanged=0, chunks=0, walk_complete=True,
                recent_files_per_minute=None, files_per_minute=0.0)
    base.update(values)
    return SimpleNamespace(**base)


def test_before_a_run_every_tile_and_resource_is_a_dash() -> None:
    assert [value for _c, value, _t in cp.tiles(None)] == [cp.NO_VALUE] * 6
    assert [caption for caption, _v, _t in cp.tiles(None)] == list(cp.TILE_LABELS)
    assert [value for _c, value, _t in cp.resources(None)] == [cp.NO_VALUE] * 5
    assert cp.step_states(None) == ["todo"] * len(cp.STEPS)


def test_the_tiles_say_what_the_progress_line_says() -> None:
    stats = _stats(indexed=1234567, seen=2000000, skipped=12, chunks=40211,
                   recent_files_per_minute=321.4)
    values = {caption: (value, tip) for caption, value, tip in
              cp.tiles(stats, total_estimate=0)}
    assert values["Indexed"][0] == "1,234,567"
    assert values["Files seen"][0] == "2,000,000"
    assert values["Passages"][0] == "40,211"
    assert values["Speed"][0] == "321 files/min"
    # No total: no number, and the progress line's own sentence in the tooltip.
    from app.ui.presenter.indexing import ETA_UNKNOWN, progress_text

    assert values["Time left"] == (cp.NO_VALUE, ETA_UNKNOWN)
    assert ETA_UNKNOWN in progress_text(stats)[1]
    with_total = dict((c, v) for c, v, _t in cp.tiles(stats, total_estimate=3_000_000))
    assert with_total["Time left"] not in (cp.NO_VALUE, ETA_UNKNOWN)


def test_a_stretch_with_no_count_shows_no_speed() -> None:
    values = dict((c, v) for c, v, _t in cp.tiles(_stats(phase="tidying", indexed=5)))
    assert values["Indexed"] == "5"
    assert values["Speed"] == values["Time left"] == cp.NO_VALUE


def test_the_steps_follow_the_phase_and_the_pictures_lane() -> None:
    names = [words for words, _codes in cp.STEPS]
    states = dict(zip(names, cp.step_states(_stats(phase="meaning"))))
    assert states["Prepare"] == states["Read"] == "done"
    assert states["Meaning"] == "current"
    assert states["Tidy"] == states["Faces and pictures"] == "todo"
    # Videos and recordings are reading.
    assert dict(zip(names, cp.step_states(_stats(phase="media"))))["Read"] == "current"
    lit = cp.step_states(_stats(phase="reading", lanes={"pictures": {"state": "working"}}))
    assert dict(zip(names, lit))["Faces and pictures"] == "current"
    assert cp.step_states(_stats(), finished=True) == ["done"] * len(cp.STEPS)


def test_the_pill_keeps_a_failure_until_the_next_start() -> None:
    assert cp.next_state("running", "failed") == "failed"
    assert cp.next_state("failed", "idle") == "failed"
    assert cp.next_state("failed", "running") == "running"
    assert cp.next_state("running", "nonsense") == "idle"
    assert cp.run_state(paused=True, held=False, stopping=True) == "stopping"
    assert cp.run_state(paused=False, held=True, stopping=False) == "paused"
    assert cp.run_state(paused=False, held=False, stopping=False) == "running"
    assert set(cp.STATES) == set(cp.STATE_ICONS) == set(cp.STATE_TOKENS)


def test_resources_are_only_what_the_run_reports() -> None:
    plain = dict((c, (v, t)) for c, v, t in cp.resources(_stats()))
    assert plain["Processor"][0] == plain["Memory"][0] == plain["Disk free"][0] \
        == cp.NO_VALUE
    assert plain["Graphics card"][0] == cp.NO_VALUE
    assert plain["Governor"][0] == "Not waiting"
    measured = _stats(resources={"cpu_percent": 47.4, "memory_mb": 5321,
                                 "memory_total_mb": 16384, "disk_free_gb": 212.2,
                                 "gpu_lane": "meaning"})
    values = dict((c, v) for c, v, _t in cp.resources(measured))
    assert values == {"Processor": "47%", "Memory": "5.2 GB of 16.0 GB",
                      "Graphics card": "Meaning", "Disk free": "212 GB",
                      "Governor": "Not waiting"}
    free = _stats(resources={"gpu_lane": ""})
    assert dict((c, v) for c, v, _t in cp.resources(free))["Graphics card"] == cp.GPU_FREE
    by_device = _stats(lanes={"pictures": {"state": "working", "device": "DirectML"},
                              "meaning": {"state": "idle", "device": "cpu"}})
    assert dict((c, v) for c, v, _t in cp.resources(by_device))["Graphics card"] == "Pictures"
    paused = _stats(paused=True, pause_reason="Memory is above 80%.")
    governor = [row for row in cp.resources(paused) if row[0] == "Governor"][0]
    assert governor[1:] == ("Waiting", "Memory is above 80%.")
    assert [row for row in cp.resources(_stats(paused_by_person=True, paused=True))
            if row[0] == "Governor"][0][1] == "Held by you"


def test_lane_rows_come_in_a_fixed_order_and_a_missing_lane_is_a_dash() -> None:
    assert [row.key for row in cp.lane_rows(None)] == [
        "walker", "pictures", "meaning", "writer", "pass"]
    assert all(row.doing == row.on == row.done == cp.NO_VALUE
               for row in cp.lane_rows(None))
    now = time.time()
    rows = cp.lane_rows(_stats(lanes={
        "meaning": {"state": "working", "task": "embedding", "item": "batch 3",
                    "started_at": now - 75, "done": 3, "total": 12, "device": "DirectML"},
        "pictures": {"state": "waiting"},              # only a state
        "beside": {"label": "Pictures beside meaning", "state": "idle"},
        "nonsense": {"state": "working"},              # not a lane the table knows
    }))
    by_key = {row.key: row for row in rows}
    assert [row.key for row in rows] == [
        "walker", "pictures", "meaning", "writer", "beside", "pass"]
    meaning = by_key["meaning"]
    assert meaning.doing == "Making text searchable by meaning"
    assert meaning.done == "3 of 12" and meaning.device == "graphics card"
    assert meaning.device_tip == "DirectML"
    assert meaning.took(now) == "1 min 15 s"
    assert by_key["pictures"].doing == "Waiting" and by_key["pictures"].on == cp.NO_VALUE
    assert by_key["walker"].doing == cp.NO_VALUE


def test_the_device_chip_says_plain_words() -> None:
    """2026-10-14, the owner's review of the renders: "DirectML" is a provider's
    name, not a word; the chip says where the work runs and the tooltip keeps
    the name."""
    for raw in ("DirectML", "dml", "CUDAExecutionProvider", "cuda:0", "GPU 0",
                "DmlExecutionProvider"):
        assert cp.device_words(raw) == cp.DEVICE_GPU == "graphics card", raw
    for raw in ("cpu", "CPUExecutionProvider", "CPU"):
        assert cp.device_words(raw) == cp.DEVICE_CPU == "processor", raw
    assert cp.device_words("") == cp.device_words(None) == ""
    row = cp.lane_rows(_stats(lanes={"pictures": {"state": "working", "device": "cpu"}}))[1]
    assert (row.device, row.device_tip) == ("processor", "cpu")


def test_reader_rows_say_where_each_reader_is() -> None:
    now = time.time()
    stats = _stats(workers={
        "10": {"file": "b.docx", "path": "D:/b.docx", "started_at": now - 2, "inner": []},
        "2": {"file": "Archive.pst", "path": "D:/Mail/Archive.pst", "started_at": now - 200,
              "inner": [{"name": "Archive.pst", "unit": "message", "n": 12, "total": 40,
                         "where": "Inbox", "stage": "messages",
                         "counts": {"Indexed": 11, "Failed": 1}}]},
        "1": {"file": "", "started_at": 0.0},
    })
    rows = cp.reader_rows(stats, now=now)
    assert [row.name for row in rows] == ["Reader 1", "Reader 2", "Reader 10"]
    waiting, archive, plain = rows
    assert waiting.state == "waiting" and waiting.took(now) == cp.NO_VALUE
    assert archive.doing == "Reading messages"
    assert archive.on == "Archive.pst › Inbox"
    assert archive.done.startswith("message 12 of 40 · ")
    assert archive.on_tip == "D:/Mail/Archive.pst" and archive.file == "Archive.pst"
    assert archive.line.startswith("Reader 2: Archive.pst › Inbox › message 12 of 40")
    assert plain.doing == "Reading" and plain.done == cp.NO_VALUE


def test_held_for_later_counts_the_index_and_words_the_run() -> None:
    from app.core.file_state import EXPLANATIONS

    assert cp.HELD_SENTENCE == EXPLANATIONS["Deferred"]
    count, lines = cp.held_summary({"ERR_OCR_HELD": 8, "ERR_PDF_ENCRYPTED": 2}, None)
    assert count == 8
    assert lines[0].startswith("8 · ") and "images pass" in lines[0]
    count, _lines = cp.held_summary({"ERR_OCR_HELD": 8}, {"Deferred": 4500, "Indexed": 9})
    assert count == 4500, "the index's own count wins once the funnel has read it"
    assert cp.held_summary(None, None) == (0, [])
    assert cp.tab_title("Skipped", 0) == "Skipped"
    assert cp.tab_title("Skipped", 1234) == "Skipped (1,234)"


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool  # noqa: E402

from app.index import pipeline as pipeline_module  # noqa: E402
from app.index.pipeline import IndexStats  # noqa: E402

WIDTH, HEIGHT = 1150, 700


def _view(qtbot, *, width: int = WIDTH, height: int = HEIGHT):
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)
    view.resize(width, height)
    view.show()
    qtbot.waitExposed(view)
    return view


def _tick(view, stats) -> None:
    view._last_paint = 0.0
    view._on_progress(stats)


def _reading(readers: int = 4, *, name: str = "report.docx", **values) -> IndexStats:
    fields = dict(phase=pipeline_module.PHASE_READING, walk_complete=True,
                  seen=10, indexed=4)
    fields.update(values)
    snap = IndexStats(**fields).snapshot()
    now = time.time()
    snap.workers = {
        str(n): {"file": f"{n}-{name}", "path": f"D:/Docs/{n}-{name}",
                 "started_at": now - n, "stage": "reading", "item": 0, "inner": []}
        for n in range(1, readers + 1)}
    snap.last_activity = now
    return snap


def _geometry(view) -> list:
    """Where every fixed part of the shelf is, and how big."""
    from PySide6.QtCore import QPoint, QRect

    parts = [view.pill, view.headline, view.now_line, view.controls, view.funnel,
             view.stepper,
             *view.stepper.chips, view.tiles, *view.tiles.boxes, view.bar, view.detail,
             view.workers_panel, view.workers_panel.area, view.workers_panel.heartbeat,
             view.resources, *view.resources.boxes, view.cockpit_tabs]
    return [QRect(part.mapTo(view, QPoint(0, 0)), part.size()) for part in parts]


@pytest.mark.parametrize("size", [(1150, 700), (1920, 1080)])
def test_nothing_on_the_shelf_moves_whatever_it_shows(qtbot, size) -> None:
    """The owner's complaint itself: boxes that change size with what they hold."""
    from types import SimpleNamespace as NS

    view = _view(qtbot, width=size[0], height=size[1])
    qtbot.wait(20)
    before = _geometry(view)
    long_name = "A very long file name " * 12 + ".pdf"
    snapshots = [
        IndexStats().snapshot(),                                       # empty
        _reading(4, indexed=987_654_321, seen=1_234_567_890, skipped=55_555_555,
                 chunks=9_999_999_999),                               # huge numbers
        _reading(4, name=long_name),                                  # long names
        _reading(8),                                                  # eight readers
        _reading(4, paused=True, pause_reason="Memory is above the ceiling " * 6),
    ]
    snapshots[2].lanes = {"pictures": {"state": "working", "item": long_name,
                                       "device": "DirectML GPU 0 " * 4},
                          "beside": {"state": "working", "task": long_name}}
    snapshots[2].notices = ["A notice that goes on and on " * 20]
    snapshots[2].skipped_by_code = {"ERR_OCR_HELD": 4000, "ERR_FILE_LOCKED": 3}
    # The counts on top (owner: "keep the counts on top"): none, then a
    # files line and a pictures line far wider than the page.
    view.funnel.show_counts({"Indexed": 987_654_321, "Queued": 123_456_789,
                             "Deferred": 55_555, "Skipped": 4_000_000, "Failed": 9,
                             "Offline": 77, "NameOnly": 12_345, "TimedOut": 3})
    view.funnel.show_pictures({"pictures": 15011, "read": 40, "faces_looked": 40,
                               "faces": 120, "people": 8, "unsorted": 5,
                               "to_describe": 300, "text_to_read": 999})
    qtbot.wait(10)
    assert _geometry(view) == before, "funnel counts"
    assert view.funnel.text().startswith("Indexed 987,654,321")
    for snap in snapshots:
        _tick(view, snap)
        qtbot.wait(10)
        assert _geometry(view) == before, snap
    view._on_finished(_reading(2))
    qtbot.wait(10)
    assert _geometry(view) == before, "finished"
    view._on_failed(NS(message="The index could not be opened. " * 10,
                       suggestion="Check the disk. " * 20))
    qtbot.wait(10)
    assert _geometry(view) == before, "failed"
    assert view.minimumSizeHint().height() <= HEIGHT


def test_the_counts_keep_two_lines_and_cut_what_does_not_fit(qtbot) -> None:
    """The funnel at the top (owner: "keep the counts on top"): two lines high
    whatever it holds, never hidden, each line cut with "…", whole in the tooltip."""
    from app.ui.widgets.status_funnel import StatusFunnel

    funnel = StatusFunnel()
    qtbot.addWidget(funnel)
    funnel.resize(220, funnel.height())
    funnel.show()
    qtbot.waitExposed(funnel)
    height = funnel.height()
    assert not funnel.isHidden() and funnel.text() == ""
    funnel.show_counts({"Indexed": 448_210, "Queued": 1_200, "Deferred": 45,
                        "Skipped": 310, "Failed": 12})
    funnel.show_pictures({"pictures": 15011, "read": 40, "to_describe": 300})
    assert funnel.height() == height and not funnel.wordWrap()
    whole = funnel.text()
    assert whole.startswith("Indexed 448,210 · ") and "\nPictures: 40 of 15,011" in whole
    assert funnel.toolTip().startswith(whole), "cut, so whole in the tooltip"
    assert "Deferred: " in funnel.toolTip()


def test_the_reader_rows_follow_the_pool_and_are_reused(qtbot) -> None:
    view = _view(qtbot)
    panel = view.workers_panel
    _tick(view, _reading(4))
    assert panel.readers == ["1", "2", "3", "4"]
    first_row, first_button = panel.reader_rows["1"], panel.skip_buttons["1"]
    _tick(view, _reading(6))
    assert panel.readers == ["1", "2", "3", "4", "5", "6"]
    assert panel.reader_rows["1"] is first_row and panel.skip_buttons["1"] is first_button
    _tick(view, _reading(2))
    assert panel.readers == ["1", "2"]
    assert not panel.reader_rows["5"].isVisible()
    assert not panel.skip_buttons["5"].isVisible()
    # The table's own height never changed; more readers scroll inside it.
    height = panel.area.height()
    _tick(view, _reading(8))
    qtbot.wait(20)
    assert panel.area.height() == height
    assert panel.area.verticalScrollBar().maximum() > 0


def test_each_skip_button_skips_its_own_readers_file(qtbot) -> None:
    class _Run:
        def __init__(self) -> None:
            self.skipped: list[str] = []

        def force_skip(self, reader: str) -> bool:
            self.skipped.append(reader)
            return True

    view = _view(qtbot)
    run = _Run()
    view._worker = SimpleNamespace(pipeline=run)
    _tick(view, _reading(6))
    for key in ("5", "2", "6"):
        button = view.workers_panel.skip_buttons[key]
        assert button.objectName() == f"indexForceSkip{key}"
        assert button.accessibleName() == f"Force skip reader {key}"
        assert f"{key}-report.docx" in button.toolTip()
        button.click()
    assert run.skipped == ["5", "2", "6"]


def test_the_skip_buttons_join_the_tab_order_in_reader_order(qtbot) -> None:
    from PySide6.QtCore import Qt

    def after(widget):
        current = widget.nextInFocusChain()
        while current is not widget and not (
                current.focusPolicy() & Qt.FocusPolicy.TabFocus):
            current = current.nextInFocusChain()
        return current

    view = _view(qtbot)
    _tick(view, _reading(3))
    skips = [view.workers_panel.skip_buttons[k] for k in ("1", "2", "3")]
    chain = [view.reset_button, *skips, view.cockpit_tabs.tabBar()]
    for first, second in zip(chain, chain[1:]):
        assert after(first) is second


def test_the_lane_rows_paint_from_the_run_and_beside_appears_in_place(qtbot) -> None:
    view = _view(qtbot)
    panel = view.workers_panel
    snap = _reading(2)
    snap.lanes = {"meaning": {"state": "working", "task": "embedding", "done": 3,
                              "total": 12, "device": "DirectML", "started_at": time.time()}}
    _tick(view, snap)
    meaning = panel.lane_rows["meaning"]
    assert meaning.doing.text() == "Making text searchable by meaning"
    assert meaning.done.text() == "3 of 12" and meaning.device.text() == "graphics card"
    assert meaning.device.toolTip() == "DirectML"
    assert panel.lane_rows["walker"].doing.text() == "-"
    assert "beside" not in panel.lane_rows or not panel.lane_rows["beside"].isVisible()
    snap.lanes["beside"] = {"state": "working", "task": "ocr"}
    _tick(view, snap)
    box = panel._lanes_box
    order = [box.itemAt(i).widget() for i in range(box.count())]
    assert order.index(panel.lane_rows["beside"]) == order.index(panel.lane_rows["writer"]) + 1
    assert order.index(panel.lane_rows["pass"]) == order.index(panel.lane_rows["beside"]) + 1
    view._on_finished(_reading(2))
    assert meaning.doing.text() == "-", "a finished run leaves no lane working"


def test_the_state_pill_follows_the_run(qtbot) -> None:
    class _Pipeline:
        def pause(self) -> None: ...
        def resume(self) -> None: ...

    view = _view(qtbot)
    pill = view.pill
    assert pill.state == "idle" and pill.word.text() == "Idle"
    view.controls.show_running()
    view._worker = SimpleNamespace(pipeline=_Pipeline(), stop=lambda: None)
    _tick(view, _reading(1))
    assert pill.word.text() == "Running"
    view.pause_run()
    assert pill.word.text() == "Paused"
    view.resume_run()
    _tick(view, _reading(1, paused=True, pause_reason="busy"))
    assert pill.state == "paused", "the machine's pause is a pause too"
    view.stop()
    assert pill.word.text() == "Stopping"
    view._on_failed(SimpleNamespace(message="Broke.", suggestion="Retry."))
    view._on_done()
    assert pill.word.text() == "Failed", "the failure stays after the buttons go idle"
    view.controls.show_running()
    assert pill.state == "running"


def test_the_tabs_carry_their_counts_and_the_held_files(qtbot) -> None:
    from app.ui.widgets.cockpit_tabs import HELD, NOTICES, SKIPPED, TAB_TIPS, TABS

    view = _view(qtbot)
    tabs = view.cockpit_tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == [name for name, _g in TABS]
    assert all(not tabs.tabIcon(i).isNull() for i in range(tabs.count()))
    assert [tabs.tabToolTip(i) for i in range(tabs.count())] == list(TAB_TIPS)
    snap = _reading(1)
    snap.skipped_by_code = {"ERR_OCR_HELD": 8, "ERR_FILE_TIMEOUT": 2}
    snap.add_notice("40GB free on the index drive")
    _tick(view, snap)
    assert tabs.tabText(SKIPPED) == "Skipped (10)"
    assert tabs.tabText(HELD) == "Held for later (8)"
    assert tabs.tabText(NOTICES) == "Notices (1)"
    assert "images pass" in tabs.held_lines.text()
    assert tabs.no_skips.isHidden() and tabs.no_notices.isHidden()


@pytest.mark.slow
def test_the_system_log_reads_the_file_on_a_worker_only_while_shown(qtbot, tmp_path) -> None:
    from datetime import date

    from app.core.log_tail import LogTail, log_file_for
    from app.ui.widgets.cockpit_tabs import SYSTEM_LOG

    view = _view(qtbot)
    tabs = view.cockpit_tabs
    feed = tabs.feed
    feed.tail = LogTail(tmp_path, today=date.today)
    log_file_for(tmp_path, date.today()).write_text(
        "2026-10-14 09:30:01.123 | WARNING  | index.pipeline | - | x:y:1 | from the child\n",
        encoding="utf-8")
    pane = view.system_log
    assert not pane._timer.isActive(), "no reading while another tab is in front"
    tabs.setCurrentIndex(SYSTEM_LOG)
    assert pane._timer.isActive()
    qtbot.waitUntil(lambda: "from the child" in pane.view.toPlainText(), timeout=5000)
    assert "09:30:01 WARNING" in pane.view.toPlainText()
    tabs.setCurrentIndex(0)
    assert not pane._timer.isActive()
    QThreadPool.globalInstance().waitForDone(5000)   # no read left behind


def test_the_feed_never_has_two_reads_out(qtbot) -> None:
    from app.ui.widgets.cockpit_tabs import LogTailFeed

    class _Slow:
        def __init__(self) -> None:
            self.calls = 0

        def poll(self) -> list:
            self.calls += 1
            time.sleep(0.2)
            return []

        def lines(self, limit=None) -> list:
            return ["a"]

    tail = _Slow()
    feed = LogTailFeed(tail)
    assert feed.lines(10) == ["a"]
    assert feed.request() is False, "one read at a time"
    qtbot.waitUntil(lambda: not feed._busy, timeout=5000)
    assert tail.calls == 1
    assert feed.request() is True
    QThreadPool.globalInstance().waitForDone(5000)   # no read left behind
