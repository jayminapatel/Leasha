r"""WORKORDER-space-report-and-idle-tune-ui-wiring - the two wirings, tested.

§1 the Space Report on the Reports page; §2 the idle-tune scheduler, one
test per ordering rule plus the defaults-only guard. Adapted from the
reference tests on branch `integrate-main` (worktree `crash-recovery-ac8615`)
to the redesigned shell, where the "what happened afterwards" sentence is a
toast rather than a status-bar message.

Needs PyQt6; skips itself without it. On the Windows venv:

    venv\Scripts\python.exe -m pytest tests/unit/test_idle_tune_and_space_report_ui.py -v
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.unit.test_ui_redesign_qt import window  # noqa: E402,F401 - the shared window


# ---------------------------------------------------------------------------
# §1 - the Space Report
# ---------------------------------------------------------------------------

def test_the_space_report_is_listed_beside_digital_inheritance(window):
    from app.ui.reports_view import REPORTS
    app, built, _ = window
    keys = [key for key, _t, _d in REPORTS]
    # 2026-09-20: "Browse your timeline" (order 0n section 4) is the third entry - a report
    # is one more row in `REPORTS`, which is what that registry is for.
    assert keys == ["inheritance", "space", "timeline"]
    titles = [built.reports_view.list.item(i).text()
              for i in range(built.reports_view.list.count())]
    assert "The Space Report" in titles
    item = built.reports_view.list.item(titles.index("The Space Report"))
    assert item.toolTip(), "a plain-words description, like the other report"


def test_the_snapshot_renders_the_same_document_the_cli_prints(window):
    from app.reports.inheritance import report_generated_at
    from app.reports.space import (
        find_duplicate_groups, find_source_uniqueness, render_space_document,
        total_reclaimable_bytes,
    )
    from app.ui.reports_view import _report_snapshot
    app, built, store = window
    sources, generated_at, space = _report_snapshot(store)
    expected = render_space_document(
        find_duplicate_groups(store), find_source_uniqueness(store),
        total_reclaimable=total_reclaimable_bytes(store),
        generated_at=report_generated_at(store))
    assert space == expected
    assert space.lstrip().startswith("#"), "plain text with # headings, like Inheritance"


def test_opening_the_space_report_shows_it_and_export_is_offered(window):
    app, built, store = window
    view = built.reports_view
    view._loaded(("", None, "# The Space Report\n\nNothing indexed yet."))
    for row in range(view.list.count()):
        if view.list.item(row).data(1) == "space":
            view.list.setCurrentRow(row)
    assert "Space Report" in view.body.toPlainText()
    assert view.export.isEnabled()


def test_export_writes_a_pdf_off_the_ui_thread(window, tmp_path, monkeypatch):
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QFileDialog
    app, built, store = window
    view = built.reports_view
    view._loaded(("", None, "# The Space Report\n\nA line."))
    for row in range(view.list.count()):
        if view.list.item(row).data(1) == "space":
            view.list.setCurrentRow(row)
    target = tmp_path / "space-report.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "PDF files (*.pdf)")))
    view._start_export()
    QThreadPool.globalInstance().waitForDone(10_000)
    for _ in range(3):
        app.processEvents()
    assert target.is_file() and target.stat().st_size > 0


# ---------------------------------------------------------------------------
# §2 - the idle-tune scheduler, one rule at a time
# ---------------------------------------------------------------------------

def _plugged(value):
    return lambda: None if value is None else type("B", (), {"power_plugged": value})()


def test_rule_1_never_while_an_index_run_is_in_progress(window, monkeypatch):
    import app.index.autotune as autotune_module
    app, built, _ = window
    asked = []
    monkeypatch.setattr(built.indexing_view, "is_running", lambda: True)
    monkeypatch.setattr(autotune_module, "should_bench",
                        lambda store, profile: asked.append(True) or "unused")
    built._maybe_run_idle_bench()
    assert not asked and not built._idle_bench_running


def test_rule_2_never_on_confirmed_battery_but_unknown_does_not_block(window, monkeypatch):
    from PyQt6.QtCore import QThreadPool
    import app.index.autotune as autotune_module
    app, built, _ = window
    asked = []
    monkeypatch.setattr(built.indexing_view, "is_running", lambda: False)
    monkeypatch.setattr(autotune_module, "should_bench",
                        lambda store, profile: asked.append(True) or "")
    monkeypatch.setattr("psutil.sensors_battery", _plugged(False))
    built._maybe_run_idle_bench()
    assert not asked, "a confirmed 'unplugged' holds the bench back"
    monkeypatch.setattr("psutil.sensors_battery", _plugged(None))
    built._maybe_run_idle_bench()
    QThreadPool.globalInstance().waitForDone(10_000)
    for _ in range(3):
        app.processEvents()
    assert asked, "an unreadable battery must not block it"


def test_rule_3_an_empty_reason_does_nothing_and_rule_4_runs_off_thread(window, monkeypatch):
    import threading
    from PyQt6.QtCore import QThreadPool
    import app.index.autotune as autotune_module
    import app.index.index_bench as index_bench_module
    app, built, _ = window
    ran = []
    monkeypatch.setattr(built.indexing_view, "is_running", lambda: False)
    monkeypatch.setattr("psutil.sensors_battery", _plugged(True))
    monkeypatch.setattr(autotune_module, "should_bench", lambda store, profile: "")
    monkeypatch.setattr(index_bench_module, "run_index_bench",
                        lambda settings, devices=None: ran.append(threading.get_ident()))
    built._maybe_run_idle_bench()
    QThreadPool.globalInstance().waitForDone(10_000)
    for _ in range(3):
        app.processEvents()
    assert not ran, "rule 3: an empty reason means do nothing this tick"

    from app.index.index_bench import IndexBench
    result = IndexBench(documents=1, chunks=1, extract_per_second=1.0,
                        write_per_second=1.0, embed_per_second={"cpu": 1.0},
                        seconds=0.1, error="")
    monkeypatch.setattr(autotune_module, "should_bench", lambda store, profile: "never timed")
    monkeypatch.setattr(index_bench_module, "run_index_bench",
                        lambda settings, devices=None: ran.append(threading.get_ident()) or result)
    monkeypatch.setattr("app.core.measured.remember", lambda store, measured: None)
    main_thread = threading.get_ident()
    original = built._settings.index_tuning_mode
    try:
        built._maybe_run_idle_bench()
        QThreadPool.globalInstance().waitForDone(10_000)
        for _ in range(3):
            app.processEvents()
        assert ran and ran[0] != main_thread, "rule 4: the bench runs on a worker"
    finally:
        built._settings_changed({"INDEX_TUNING_MODE": original})
        built._settings = built._settings.model_copy(update={"index_tuning_mode": original})
        built.indexing_view.tuning.load(built._settings)


def test_rules_5_and_6_remember_then_upgrade_defaults_to_auto_quietly(window, monkeypatch):
    from PyQt6.QtCore import QThreadPool
    import app.index.autotune as autotune_module
    import app.index.index_bench as index_bench_module
    from app.index.index_bench import IndexBench
    app, built, _ = window
    original = built._settings.index_tuning_mode
    assert original == "defaults"
    result = IndexBench(documents=3, chunks=9, extract_per_second=12.0,
                        write_per_second=40.0, embed_per_second={"cpu": 8.0},
                        seconds=1.2, error="")
    remembered = []
    monkeypatch.setattr(built.indexing_view, "is_running", lambda: False)
    monkeypatch.setattr("psutil.sensors_battery", _plugged(True))
    monkeypatch.setattr(autotune_module, "should_bench", lambda store, profile: "not timed yet")
    monkeypatch.setattr(index_bench_module, "run_index_bench", lambda settings, devices=None: result)
    monkeypatch.setattr("app.core.measured.remember", lambda store, m: remembered.append(m))
    built.toast.clear()
    try:
        built._maybe_run_idle_bench()
        QThreadPool.globalInstance().waitForDone(10_000)
        for _ in range(3):
            app.processEvents()
        assert remembered, "rule 5: a successful bench is remembered"
        assert built._settings.index_tuning_mode == "auto"
        assert built.indexing_view.tuning.current_mode() == "auto", "the control reflects it"
        assert built.toast.current_text().startswith("Timed this computer while it was idle")
    finally:
        built._settings_changed({"INDEX_TUNING_MODE": original})
        built._settings = built._settings.model_copy(update={"index_tuning_mode": original})
        built.indexing_view.tuning.load(built._settings)
        built.toast.clear()


@pytest.mark.parametrize("chosen", ["manual", "auto"])
def test_rule_6_never_overrides_a_real_choice(window, monkeypatch, chosen):
    from app.index.index_bench import IndexBench
    app, built, _ = window
    original = built._settings.index_tuning_mode
    writes = []
    monkeypatch.setattr(built, "_settings_changed", lambda values: writes.append(values))
    built._settings = built._settings.model_copy(update={"index_tuning_mode": chosen})
    try:
        built._idle_bench_finished(IndexBench(
            documents=1, chunks=1, extract_per_second=1.0, write_per_second=1.0,
            embed_per_second={"cpu": 1.0}, seconds=0.1, error=""))
        assert not writes and built._settings.index_tuning_mode == chosen
    finally:
        built._settings = built._settings.model_copy(update={"index_tuning_mode": original})
