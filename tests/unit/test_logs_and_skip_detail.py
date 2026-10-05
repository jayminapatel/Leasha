"""Four faults found on 2026-10-05 from one line on the Indexing page.

"3 x An unexpected error occurred ... please report it with the detail below
and today's file from the logs folder." There was no detail below, today's
file had stopped being written at 00:48, and the run beside it warned
seventeen times about something that was not wrong. Each test here fails on
the code before its fix.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from loguru import logger

from app.core.errors import make_error, to_app_error


# -- the log file two processes share -------------------------------------------

def test_the_log_is_never_renamed_out_from_under_another_process(tmp_path):
    """The window and its indexing process write one `app_<day>.log`. Rotating
    it by size renames it, Windows refuses while the other has it open, and
    every later line from that process is lost. A second handle stands in for
    the second process; every line must reach the disk."""
    from app.core import logging as leasha_logging

    assert inspect.signature(leasha_logging.setup_logging).parameters["rotation"].default == "00:00"
    try:
        leasha_logging.setup_logging(tmp_path, force=True)
        logger.info("first")
        logger.complete()
        target = next((tmp_path / "app").glob("app_*.log"))
        with open(target, "a", encoding="utf-8"):                 # the other process
            for number in range(400):
                logger.info("line {} {}", number, "x" * 60)
            logger.complete()
    finally:
        logger.remove()
    files = list((tmp_path / "app").glob("app_*"))
    assert len(files) == 1, files
    written = files[0].read_text(encoding="utf-8")
    assert written.count("| line ") == 400 or sum("line " in row for row in written.splitlines()) >= 400


# -- what is recorded for a file skipped by a fault ------------------------------

def _fault() -> object:
    try:
        raise KeyError("virtual_path")
    except KeyError as exc:
        return to_app_error(exc, "index.pipeline", path="D:/mail/2026.pst")


def test_a_fault_is_recorded_with_the_exception_not_only_the_sentence():
    from app.storage.sqlite_store import skip_sentence

    error = _fault()
    recorded = skip_sentence(error)
    assert recorded.startswith(error.message)
    assert "KeyError" in recorded and "virtual_path" in recorded
    # A known reason is its sentence, as it always was.
    known = make_error("ERR_FILE_LOCKED", "index.pipeline", path="D:/mail/2026.pst")
    assert skip_sentence(known) == known.message


def test_the_store_gives_the_panel_its_detail(tmp_path):
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "knowledge.db").connect()
    try:
        file_id = store.upsert_file("D:/mail/2026.pst", parent_dir="D:/mail", ext="pst",
                                    size_bytes=1, mtime_ns=1, status="PENDING", source_kind="file")
        store.mark_skipped(file_id, _fault())
        rows = store.skip_details("ERR_UNEXPECTED")
        assert rows == [{"path": "D:/mail/2026.pst", "detail": rows[0]["detail"]}]
        assert "KeyError" in rows[0]["detail"]
        assert store.skip_details("ERR_FILE_LOCKED") == []
    finally:
        store.close()


def test_the_panel_shows_the_detail_it_promises_and_keeps_it_through_a_rebuild():
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QApplication

    from app.ui.widgets.skips_panel import SkipsPanel

    app = QApplication.instance() or QApplication([])

    class Retry(QObject):
        asked = pyqtSignal(str)

    retry = Retry()
    panel = SkipsPanel(retry.asked)
    try:
        panel.show_skips({"ERR_UNEXPECTED": 3})
        row = panel._widgets["ERR_UNEXPECTED"]
        assert row._details.isHidden(), "nothing to show yet"
        panel.show_details("ERR_UNEXPECTED", [
            {"path": "D:\\mail\\2026.pst", "detail": "An unexpected error occurred. KeyError: 'x'"}])
        assert "2026.pst: An unexpected error occurred. KeyError: 'x'" in row._details.text()
        assert not row._details.isHidden()
        # A new reason rebuilds every row; the detail is put back.
        panel.show_skips({"ERR_UNEXPECTED": 3, "ERR_FILE_LOCKED": 1})
        assert "KeyError" in panel._widgets["ERR_UNEXPECTED"]._details.text()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_the_window_asks_for_the_detail_when_a_run_ends_with_such_a_skip():
    from app.ui.controllers import index_controller

    source = inspect.getsource(index_controller)
    assert "finished.connect(self._show_unexpected_detail)" in source
    asks = source[source.index("def _show_unexpected_detail"):]
    assert "skip_details" in asks[:asks.index("def _idle_bench_finished")]


# -- the warning that was not true ------------------------------------------------

def test_two_attachments_of_one_name_are_not_a_reader_forgetting_its_key():
    from app.index import pipeline

    assert pipeline.same_named_attachment(
        "pst://2026/0000000071/attachments/image.png") is True
    assert pipeline.same_named_attachment("D:/mail/2026.pst") is False
    assert pipeline.same_named_attachment(None) is False
    # The real fault - many documents sharing the file's own path - still warns.
    owner = next(cls for _name, cls in inspect.getmembers(pipeline, inspect.isclass)
                 if "_record_skip" in vars(cls))
    assert "if not same_named_attachment(key):" in inspect.getsource(owner)
