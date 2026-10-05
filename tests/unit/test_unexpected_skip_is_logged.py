"""A file skipped by a fault in Leasha's own code leaves a line in the log.

2026-10-05. The Indexing page showed "3 x An unexpected error occurred ... This
is a bug - please report it with the detail below and today's file from the
logs folder", and neither existed: the exception had been turned into a skipped
file without one line being logged. These fail on the code before the fix.
"""

from __future__ import annotations

from loguru import logger

from app.core.errors import make_error, to_app_error
from app.index.pipeline import say_unexpected_skip


def _captured():
    lines: list = []
    handle = logger.add(lambda message: lines.append(message.record), level="DEBUG")
    return lines, handle


def test_an_unexpected_skip_names_the_file_and_carries_the_trace():
    lines, handle = _captured()
    try:
        try:
            raise KeyError("virtual_path")
        except KeyError as exc:
            error = to_app_error(exc, "index.pipeline", path="D:/mail/2026.pst")
        assert say_unexpected_skip(None, "D:/mail/2026.pst", error) is True
    finally:
        logger.remove(handle)
    assert len(lines) == 1
    record = lines[0]
    assert record["level"].name == "ERROR"
    assert record["extra"]["error_code"] == "ERR_UNEXPECTED"
    assert "D:/mail/2026.pst" in record["message"]
    assert "KeyError" in record["message"] and "virtual_path" in record["message"]


def test_a_known_reason_is_not_logged_twice_and_nothing_here_can_raise():
    lines, handle = _captured()
    try:
        known = make_error("ERR_FILE_LOCKED", "index.pipeline", path="D:/mail/2026.pst")
        assert say_unexpected_skip(None, "D:/mail/2026.pst", known) is False
        assert say_unexpected_skip(None, "D:/mail/2026.pst", None) is False
        assert say_unexpected_skip(object(), "D:/mail/2026.pst", to_app_error(
            RuntimeError("x"), "index.pipeline")) is False       # a logger that cannot log
    finally:
        logger.remove(handle)
    assert lines == []


def test_recording_a_skip_calls_it():
    """The line that was missing: `_record_skip` says an unexpected skip."""
    import inspect

    from app.index import pipeline

    owner = next(cls for _name, cls in inspect.getmembers(pipeline, inspect.isclass)
                 if "_record_skip" in vars(cls))
    assert "say_unexpected_skip(" in inspect.getsource(owner._record_skip)


def test_the_panel_says_the_fault_was_in_indexing_not_in_the_window():
    from app.ui.presenter.indexing import group_skips

    group = group_skips({"ERR_UNEXPECTED": 3})[0]
    assert group.count == 3
    assert "in indexing" in group.message and "in ui" not in group.message
