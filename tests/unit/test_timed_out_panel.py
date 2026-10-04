r"""The Indexing page's "Timed-out files" panel and its button (order 0z, F3).

Layer: L5

Driven through the real `IndexingView` and, for what the press starts, the
real `MainWindow` and its index controller - as `test_force_skip_button.py`
and `test_start_indexing_resolves_off_thread.py` do. What these pin:

* the panel's words, without a display (`presenter/timed_out.py`);
* the panel is hidden until the index holds a timed-out file, shows one row
  per file type, and each row's button reads exactly
  "Retry with a longer time limit";
* a press asks for that type with the number in the box, and does no I/O;
* the rows come from the summary a worker read (`read_index_summary`);
* in the window a press builds a retry run - in-process, or for the separate
  indexing process when that switch is on - and saves no setting;
* a press while a run is going starts nothing and says why.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt                                     # noqa: E402

from app.core.errors import make_error                          # noqa: E402
from app.index.timed_out_retry import RetryTimedOut             # noqa: E402
from app.storage.sqlite_store import FileStatus, SqliteStore    # noqa: E402
from app.ui.presenter import timed_out as words                 # noqa: E402

pytestmark = pytest.mark.gui

GROUPS = [
    {"ext": "pdf", "count": 12, "example": r"D:\Docs\Manuals\big-manual.pdf"},
    {"ext": "pst", "count": 1, "example": "D:/Mail/Archive2019.pst"},
    {"ext": "", "count": 2, "example": "D:/Code/Makefile"},
]


# ---------------------------------------------------------------------------
# The words (no display needed)
# ---------------------------------------------------------------------------

def test_the_button_says_exactly_what_the_order_asks() -> None:
    assert words.RETRY_LABEL == "Retry with a longer time limit"


def test_rows_are_worded_most_files_first() -> None:
    rows = words.timed_out_rows(GROUPS)
    assert [row.heading for row in rows] == [
        "12 .pdf files", "2 files with no extension", "1 .pst file"]
    assert rows[0].example_text == "e.g. big-manual.pdf", "the name, not the path"
    assert words.panel_title(rows) == "15 files timed out"
    assert words.panel_title(rows[2:]) == "1 file timed out"
    assert words.retry_accessible_name(rows[0]) == (
        "Retry with a longer time limit: 12 .pdf files")


def test_rows_tolerate_what_is_not_a_group() -> None:
    assert words.timed_out_rows(None) == []
    assert words.timed_out_rows([None, 7, {"ext": "pdf", "count": 0},
                                 {"ext": "txt", "count": "3"}]) == [
        words.TimedOutRow(ext="txt", count=3)]


def test_the_box_offers_longer_never_the_same() -> None:
    assert words.FACTOR_MIN == 2 and words.FACTOR_DEFAULT == 4
    assert words.FACTOR_MIN <= words.FACTOR_DEFAULT <= words.FACTOR_MAX


# ---------------------------------------------------------------------------
# The panel on the page
# ---------------------------------------------------------------------------

def _view(qtbot):
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)
    view.resize(900, 700)
    view.show()
    qtbot.waitExposed(view)
    return view


def test_the_panel_is_hidden_until_something_has_timed_out(qtbot) -> None:
    from app.ui.widgets.indexing_layout import paint_totals

    view = _view(qtbot)
    panel = view.timed_out
    assert not panel.isVisible()

    paint_totals(view, {"error": "", "stats": {}, "timed_out": []})
    assert not panel.isVisible()

    paint_totals(view, {"error": "", "stats": {}, "timed_out": GROUPS})
    assert panel.isVisible()
    assert panel.title() == "15 files timed out"
    for ext in ("pdf", "pst", ""):
        assert panel.button_for(ext).text() == "Retry with a longer time limit"
    assert panel.button_for("pdf").accessibleName().endswith("12 .pdf files")
    assert panel.button_for("docx") is None

    # A summary that could not be read (no key) leaves the rows alone...
    paint_totals(view, {"error": "OperationalError: database is locked"})
    assert panel.isVisible() and panel.button_for("pdf") is not None
    # ...and one that says nothing is timed out any more hides the panel.
    paint_totals(view, {"error": "", "stats": {}, "timed_out": []})
    assert not panel.isVisible() and panel.button_for("pdf") is None


def test_a_press_asks_for_that_type_with_the_number_in_the_box(qtbot) -> None:
    view = _view(qtbot)
    panel = view.timed_out
    panel.show_groups(GROUPS)
    asked: list = []
    panel.retryRequested.connect(lambda ext, factor: asked.append((ext, factor)))

    assert panel.factor.value() == 4
    assert panel.factor.minimum() == 2 and panel.factor.maximum() == 100
    assert panel.factor.suffix() == " times the usual limit"
    qtbot.mouseClick(panel.button_for("pdf"), Qt.MouseButton.LeftButton)
    panel.factor.setValue(10)
    assert panel.factor.accessibleName() == "Give each file: 10 times the usual limit"
    qtbot.mouseClick(panel.button_for(""), Qt.MouseButton.LeftButton)
    assert asked == [("pdf", 4.0), ("", 10.0)]


def test_new_counts_update_the_rows_in_place(qtbot) -> None:
    view = _view(qtbot)
    panel = view.timed_out
    panel.show_groups(GROUPS)
    button = panel.button_for("pdf")

    fewer = [dict(GROUPS[0], count=11), GROUPS[2], GROUPS[1]]
    panel.show_groups(fewer)
    assert panel.button_for("pdf") is button, "same types: nothing was rebuilt"
    assert panel.title() == "14 files timed out"
    assert button.accessibleName().endswith("11 .pdf files")

    panel.show_groups([GROUPS[1]])
    assert panel.button_for("pdf") is None and panel.button_for("pst") is not None
    assert panel.title() == "1 file timed out"


def test_the_panel_reads_no_store_on_the_interface_thread() -> None:
    """The panel and its words are given rows; neither asks a store for them."""
    root = Path(__file__).resolve().parents[2] / "app" / "ui"
    for name in ("widgets/timed_out_panel.py", "presenter/timed_out.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "store." not in text and "SqliteStore(" not in text, name


# ---------------------------------------------------------------------------
# The summary the rows come from
# ---------------------------------------------------------------------------

def _timed_out(store: SqliteStore, path: str) -> None:
    ext = Path(path).suffix.lstrip(".").lower()
    file_id = store.upsert_file(path, size_bytes=10, mtime_ns=1_000, ext=ext,
                                status=FileStatus.PENDING)
    store.mark_skipped(file_id, make_error(
        "ERR_FILE_TIMEOUT", "test", path=path, took="20 min",
        reason="reading it took longer than 20 min"))


def test_the_index_summary_carries_the_timed_out_groups(tmp_path) -> None:
    from app.ui.tasks import read_index_summary

    with SqliteStore(tmp_path / "index.db") as store:
        assert read_index_summary(store)["timed_out"] == []
        _timed_out(store, "D:/Docs/a.pdf")
        _timed_out(store, "D:/Docs/b.pdf")
        _timed_out(store, "D:/Mail/old.pst")
        assert [(g["ext"], g["count"]) for g in read_index_summary(store)["timed_out"]] == [
            ("pdf", 2), ("pst", 1)]


# ---------------------------------------------------------------------------
# What a press starts, in the real window
# ---------------------------------------------------------------------------

@pytest.fixture()
def window(tmp_path, monkeypatch):
    """The real window over a temporary index holding two timed-out PDFs, with
    the tuning numbers resolved by a stand-in (no hardware is asked)."""
    import app.index.resolve as resolve_module
    from app.index.resolve import Resolved
    from tests.unit.test_start_indexing_resolves_off_thread import _pump, _window

    app, built, store, vectors = _window(tmp_path)
    monkeypatch.setattr(resolve_module, "resolve_for_run",
                        lambda settings, store: Resolved(
                            workers=2, onnx_threads=1, embed_batch=8))
    _timed_out(store, str(tmp_path / "a.pdf"))
    _timed_out(store, str(tmp_path / "b.pdf"))
    started: list = []
    built.indexing_view.start = lambda pipeline, **kw: started.append(pipeline)
    # The window reads its totals as it opens, and `refresh_totals` does nothing
    # while a read is still going - so that one is let finish first. Without
    # this the page kept the opening read, taken before the two rows existed
    # (found 2026-09-30, once another thread's work made that read slower).
    _pump(app)
    built.indexing_view.refresh_totals(store, built._settings)
    _pump(app)
    try:
        yield app, built, started
    finally:
        _pump(app)
        store.close()
        vectors.close()


def test_a_press_in_the_window_builds_a_retry_and_saves_no_setting(window) -> None:
    from tests.unit.test_start_indexing_resolves_off_thread import _pump

    app, built, started = window
    panel = built.indexing_view.timed_out
    assert panel.title() == "2 files timed out", "painted from the worker's read"
    before = built._settings.model_dump()
    env_before = Path(built._settings.env_file).read_text(encoding="utf-8")

    panel.factor.setValue(6)
    panel.button_for("pdf").click()
    assert not built.indexing_view.start_button.isEnabled(), "busy while it prepares"
    _pump(app)

    assert len(started) == 1
    run = started[0]
    assert type(run).__name__ == "RetryPipeline"
    assert run.retry == RetryTimedOut(group="pdf", factor=6.0)
    # Nothing is walked or pruned, and the run's own limits are the saved ones.
    assert run.config.walk.roots == [] and run.config.prune_missing is False
    assert run.config.file_time_limit_s == built._settings.index_file_time_limit_s
    assert run.config.stall_limit_s == built._settings.index_stall_limit_s
    assert built._settings.model_dump() == before, "no setting was changed"
    assert Path(built._settings.env_file).read_text(encoding="utf-8") == env_before
    assert not built._resolving_index


def test_with_the_separate_process_switch_on_the_child_is_asked_for_the_retry(
        window) -> None:
    from app.index.child_run import ChildIndexRun
    from tests.unit.test_start_indexing_resolves_off_thread import _pump

    app, built, started = window
    built._settings = built._settings.model_copy(
        update={"index_separate_process": True})
    built.indexing_view.timed_out.button_for("pdf").click()
    _pump(app)

    assert len(started) == 1 and isinstance(started[0], ChildIndexRun)
    argv = started[0].argv
    assert "--retry-timed-out=pdf" in argv and "--time-limit-factor=4" in argv
    assert argv[-1] == "--", "no folders: a retry walks nothing"
    assert argv.index("--retry-timed-out=pdf") < argv.index("--")
    # And the command line reads that back as the same retry.
    from app.cli import build_parser
    from app.cli.index import _retry_asked

    args = build_parser().parse_args(argv[argv.index("index"):])
    assert _retry_asked(args) == (RetryTimedOut(group="pdf", factor=4.0), None)


def test_a_press_while_a_run_is_going_starts_nothing_and_says_why(window) -> None:
    from tests.unit.test_start_indexing_resolves_off_thread import _pump

    app, built, started = window
    view = built.indexing_view
    # A run another process holds the run lock for - the page's poll sets this.
    view._external = {"owner": "command-line", "pid": 1}
    # The toast is shared with the window's other notices (the schedule says
    # its piece as the window opens), so what was said is recorded here.
    said: list[str] = []
    built.notify = lambda text, *_a, **_k: said.append(text)
    try:
        view.timed_out.button_for("pdf").click()
        _pump(app)
        assert started == []
        assert not built._resolving_index
        assert view.start_button.isEnabled(), "nothing was left disabled"
        assert said == [words.RETRY_BUSY]
    finally:
        view._external = None
        del built.notify


def test_a_finished_retry_teaches_the_tuner_nothing_and_offers_no_images_pass(
        window, monkeypatch) -> None:
    """A retry reads the slowest files in the index: it is not a measurement of
    the computer and it is not the text pass. Neither follow-up may act on it."""
    import app.index.autotune as autotune
    from app.index.pipeline import IndexStats
    from app.index.timed_out_retry import RESOLVED_KEY

    _app, built, _started = window
    built._settings = built._settings.model_copy(update={"index_ocr_pass": "after-run"})
    learned: list = []
    monkeypatch.setattr(autotune, "learn",
                        lambda *a, **k: learned.append(a) or None)
    said: list[str] = []
    built.notify = lambda text, *_a, **_k: said.append(text)
    try:
        retried = IndexStats(stages={"extract": 9.0, "embed": 1.0},
                             resolved={"workers": 2, RESOLVED_KEY: ".pdf, 4 times"})
        built.index_ctl._learn_from_run(retried)
        built.index_ctl._offer_images_pass(retried)
        assert learned == [] and said == []

        ordinary = IndexStats(stages={"extract": 9.0, "embed": 1.0},
                              resolved={"workers": 2})
        built.index_ctl._learn_from_run(ordinary)
        built.index_ctl._offer_images_pass(ordinary)
        assert len(learned) == 1, "an ordinary run is still learned from"
        assert len(said) == 1 and said[0].startswith("Text is indexed.")
    finally:
        del built.notify


def test_nothing_is_collected_in_the_middle_of_a_test() -> None:
    """The guard in `tests/conftest.py` - see `no_window_is_collected_while_it_
    paints`. This file is where its absence ended the test process."""
    import gc

    assert not gc.isenabled()


def test_under_after_run_the_start_after_a_text_pass_is_the_images_pass(window) -> None:
    """2026-10-04, the owner: "this is the second time it is running why is it
    not scanning for faces". Under "after-run" every Start was the text pass,
    so the notice's "press Start again" held every picture again."""
    from app.index.pipeline import IndexStats

    _app, built, _started = window
    built._settings = built._settings.model_copy(update={"index_ocr_pass": "after-run"})
    built.notify = lambda *_a, **_k: None
    ctl = built.index_ctl
    try:
        ctl._images_due = False
        assert ctl._ocr_mode_for_run() == "text"
        ctl._offer_images_pass(IndexStats(ocr_mode="text"))
        assert ctl._ocr_mode_for_run() == "images", "the next Start reads the images"
        ctl._offer_images_pass(IndexStats(ocr_mode="images"))
        assert ctl._ocr_mode_for_run() == "text", "and then text again"
        built._settings = built._settings.model_copy(update={"index_ocr_pass": "manual"})
        ctl._images_due = True
        assert ctl._ocr_mode_for_run() == "text", "manual stays manual"
    finally:
        ctl._images_due = False
        del built.notify


def test_a_run_in_its_own_process_is_told_which_pass_it_is(window) -> None:
    """2026-10-04, the owner: "confirm the individual index and main index is
    same code". They are; but a child process was never told the pass, and
    worked out "text" from the settings every time under "after-run" - so the
    images pass the window had decided on ran as the text pass."""
    from types import SimpleNamespace

    from app.index.timed_out_retry import RetryTimedOut

    _app, built, _started = window
    built._settings = built._settings.model_copy(update={"index_ocr_pass": "after-run"})
    ctl, tuned = built.index_ctl, SimpleNamespace(workers=2)
    try:
        ctl._images_due = True
        whole = ctl._child_run(tuned, ["C:/docs"], None, False).argv
        one_folder = ctl._child_run(tuned, ["C:/docs/a"], ["C:/docs/a"], True).argv
        assert "--only-ocr" in whole and "--only-ocr" in one_folder, "Start and Index now alike"
        ctl._images_due = False
        assert "--skip-ocr" in ctl._child_run(tuned, ["C:/docs"], None, False).argv
        retried = ctl._child_run(tuned, ["C:/docs"], None, False, retry=RetryTimedOut("pdf"))
        assert "--only-ocr" not in retried.argv and "--skip-ocr" not in retried.argv
        # 2026-10-04: and the pass a retry takes is the same in both places.
        # The window used `_ocr_mode_for_run` (here the images pass, which
        # reads only the pictures inside a retried mailbox) while the child
        # worked out "text"; both now ask `run_setup.pass_for(..., NOW)`.
        from app.cli import build_parser
        from app.cli.index import _ocr_mode

        ctl._images_due = True
        argv = retried.argv
        args = build_parser().parse_args(argv[argv.index("index"):])
        in_process = ctl._run_pass(RetryTimedOut("pdf"))
        assert in_process == "both"
        assert _ocr_mode(args, built._settings, images_due=True, retry=True) == in_process
    finally:
        ctl._images_due = False
