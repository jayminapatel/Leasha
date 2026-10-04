r"""The window's index runs are set up by the same code as every other run.

Layer: L5

2026-10-04, the owner: "where ever possible the same code should run for
functions so they are all consistent and standard". The window's half of
`test_run_setup.py`, through the real `MainWindow` and its index controller,
with the tuning numbers resolved by a stand-in (no hardware is asked) and the
run captured rather than started:

* Start builds exactly the configuration `run_setup.build_pipeline_config`
  builds - so it leaves out Leasha's own folders, as the command line does;
* only a whole run over the saved folders moves the schedule;
* a run stopped part-way does not decide the next pass;
* what another process recorded about the images pass reaches the window,
  unless the window's own newer write has not landed yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from app.index.resolve import Resolved                          # noqa: E402

pytestmark = pytest.mark.gui

TUNED = Resolved(workers=2, onnx_threads=1, embed_batch=8)


@pytest.fixture()
def window(tmp_path, monkeypatch):
    import app.index.resolve as resolve_module
    from tests.unit.test_start_indexing_resolves_off_thread import _pump, _window

    app, built, store, vectors = _window(tmp_path)
    monkeypatch.setattr(resolve_module, "resolve_for_run",
                        lambda settings, store: TUNED)
    started: list = []
    built.indexing_view.start = lambda pipeline, **kw: started.append(pipeline)
    _pump(app)
    try:
        yield app, built, started
    finally:
        _pump(app)
        store.close()
        vectors.close()


def test_start_builds_the_configuration_every_run_builds(window, tmp_path) -> None:
    """The window's run had a `PipelineConfig` of its own and had drifted: it
    never left out Leasha's own folders, so a folder holding the index, the
    logs or the models was read into itself."""
    from app.index.run_setup import build_pipeline_config
    from tests.unit.test_start_indexing_resolves_off_thread import _pump

    app, built, started = window
    folder = tmp_path / "docs"
    folder.mkdir()
    built._start_indexing(roots=[str(folder)])
    _pump(app)

    assert len(started) == 1
    ctl = built.index_ctl
    expected = build_pipeline_config(
        built._settings, [folder], tuned=TUNED, first=tuple(ctl._first_folders()),
        prune=False, ocr_mode=ctl._ocr_mode_for_run())
    assert started[0].config == expected
    assert str(built._settings.data_path) in started[0].config.walk.exclude_paths


def test_only_a_whole_run_moves_the_schedule(window, tmp_path) -> None:
    """"Index now" on one folder, or a retry, pushed the next scheduled run
    over every folder back by a whole interval."""
    from app.index.pipeline import IndexStats
    from app.index.timed_out_retry import RESOLVED_KEY, RetryTimedOut

    _app, built, _started = window
    ctl = built.index_ctl
    told: list = []
    built.scheduler.notify_finished = lambda *a, **k: told.append(1)
    folder = str(tmp_path)

    ctl._index_resolved(TUNED, [folder], [folder], True)
    ctl._schedule_after_run(IndexStats())
    assert told == [], "one folder's Index now"

    ctl._index_resolved(TUNED, [], None, False, retry=RetryTimedOut("pdf"))
    ctl._schedule_after_run(IndexStats(resolved={RESOLVED_KEY: "pdf"}))
    assert told == [], "a retry"

    ctl._index_resolved(TUNED, [folder], None, False)
    ctl._schedule_after_run(IndexStats())
    assert told == [1], "a whole run"


def test_one_folder_s_index_now_neither_reads_nor_records_the_images_pass(
        window, tmp_path) -> None:
    """2026-10-04, code review: "Index now" on one folder read the index-wide
    "images due" flag (so under "after-run" it ran the whole index's images
    pass, scoped to one folder) and wrote it when it finished."""
    from app.index.pipeline import IndexStats
    from app.index.run_setup import OCR_MODES

    _app, built, started = window
    built._settings = built._settings.model_copy(
        update={"index_ocr_pass": "after-run", "index_ocr_mode": "both"})
    built.notify = lambda *_a, **_k: None
    ctl = built.index_ctl
    folder = tmp_path / "one"
    folder.mkdir()
    try:
        ctl._images_due = True
        ctl._index_resolved(TUNED, [str(folder)], [str(folder)], False)
        assert started[-1].config.ocr_mode == "both", "as INDEX_OCR_MODE says"
        assert ctl._pass_flags() == [], "and the child is told the same"
        ctl._offer_images_pass(IndexStats(ocr_mode="both"))
        assert ctl._images_due is True, "nothing recorded"
        ctl._index_resolved(TUNED, [str(folder)], None, False)
        assert started[-1].config.ocr_mode == "images", "Start is the images pass"
        assert OCR_MODES == ("both", "text", "images")
    finally:
        ctl._images_due = False
        ctl._pass_whole = True
        del built.notify


def test_the_what_gets_read_page_hears_that_the_images_are_due(window) -> None:
    """2026-10-04, code review: the pictures sentence re-implemented the pass
    rule and never knew the images were due."""
    from app.ui.presenter.coverage import PICTURES

    _app, built, _started = window
    box = built.indexing_view.tuning.coverage
    ctl = built.index_ctl
    try:
        box.note_levers({"index_ocr_pass": "after-run"})
        assert box.sentences[PICTURES].text().startswith("Text in photos")
        ctl._images_due = False
        built._show_external_run({"locked": False, "record": None, "link": None,
                                  "front_requested": False, "images_due": True})
        assert box.sentences[PICTURES].text().startswith("This run reads only photos")
    finally:
        ctl._images_due = False
        box.note_levers({"images_due": False})


def test_a_stopped_text_pass_does_not_make_the_images_due(window) -> None:
    from app.index.pipeline import IndexStats

    _app, built, _started = window
    built._settings = built._settings.model_copy(update={"index_ocr_pass": "after-run"})
    said: list = []
    built.notify = lambda text, *_a, **_k: said.append(text)
    ctl = built.index_ctl
    try:
        ctl._images_due = False
        built.indexing_view._stopping = True
        ctl._offer_images_pass(IndexStats(ocr_mode="text"))
        assert ctl._images_due is False and said == [], "carried on as the text pass"
        built.indexing_view._stopping = False
        ctl._offer_images_pass(IndexStats(ocr_mode="text"))
        assert ctl._images_due is True and len(said) == 1
    finally:
        built.indexing_view._stopping = False
        ctl._images_due = False
        del built.notify


def test_what_another_process_recorded_reaches_the_window(window) -> None:
    """`app.cli index` records the images pass as the window does; the window
    reads it on the external-run tick it already has."""
    _app, built, _started = window
    ctl = built.index_ctl
    payload = {"locked": False, "record": None, "link": None,
               "front_requested": False}
    try:
        ctl._images_due = False
        built._show_external_run(dict(payload, images_due=True))
        assert ctl._images_due is True
        built._show_external_run(payload)
        assert ctl._images_due is True, "unknown changes nothing"
        ctl._images_due_saving = True
        built._show_external_run(dict(payload, images_due=False))
        assert ctl._images_due is True, "the window's own newer write wins"
    finally:
        ctl._images_due_saving = False
        ctl._images_due = False


def test_the_window_saves_the_pst_reader_where_every_run_reads_it(window) -> None:
    from app.index.run_setup import PST_BACKEND_STATE_KEY
    from tests.unit.test_start_indexing_resolves_off_thread import _pump

    app, built, _started = window
    from app.extract.base import extractor_for

    extractor = extractor_for(Path("x.pst"))
    before = getattr(extractor, "backend", "auto")
    try:
        built._save_pst_backend("libpff")
        # A queued write (`state_writes`); `IndexWorker.run` settles these
        # before a run reads the store, which this stands in for.
        from app.ui.state_writes import pool
        pool().waitForDone(5_000)
        _pump(app)
        assert built._store.get_state(PST_BACKEND_STATE_KEY, "") == "libpff"
    finally:
        if extractor is not None:
            extractor.backend = before
