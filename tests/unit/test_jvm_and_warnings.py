r"""Three ways a five-hour index run reported itself badly, or died.

All three came out of one real run over `D:\SearchData`:

    1,880 docs  0 unchanged  0 already current  37,063 chunks | 4/min
    # A fatal error has been detected by the Java Runtime Environment:
    #  EXCEPTION_ACCESS_VIOLATION (0xc0000005) ... pid=44512
    #  Problematic frame: C  [python312.dll+0x76c49]
"""

from __future__ import annotations

import warnings

import pytest


# --- the JVM that killed the run -------------------------------------------

def test_the_jvm_backed_reader_is_off_unless_asked_for(monkeypatch):
    """**A JVM fault is not an exception - it is a dead process.**

    `mpxj` runs Java inside this process through JPype, so nothing here can
    catch a crash in it. One `.mpp` ended a run at 1,880 documents after five
    hours.

    `pyproject.toml` already excluded JVM tests from the default suite for
    exactly this reason. The same argument applies with more force to a
    multi-day index, where the cost is not a test report.
    """
    from app.extract.diagrams import JVM_SWITCH, _jvm_allowed

    monkeypatch.delenv(JVM_SWITCH, raising=False)
    assert _jvm_allowed() is False

    monkeypatch.setenv(JVM_SWITCH, "1")
    assert _jvm_allowed() is True


def test_no_jvm_is_started_while_the_switch_is_off(monkeypatch):
    """The switch has to be checked *before* the import, not after.

    Importing mpxj is what starts the JVM, so a guard that runs afterwards
    guards nothing.
    """
    import inspect

    from app.extract import diagrams

    body = inspect.getsource(diagrams._mpp_tasks)
    guard = body.index("_jvm_allowed()")
    reader = body.index("_mpp_reader()")
    assert guard < reader, "the JVM is loaded before the switch is consulted"


def test_a_project_file_is_still_indexed_by_name(monkeypatch):
    """Turning the reader off must not make `.mpp` files vanish - the whole
    point of the metadata document is that the plan stays findable."""
    from app.extract.diagrams import JVM_SWITCH

    monkeypatch.delenv(JVM_SWITCH, raising=False)
    from app.extract.base import extractor_for
    from pathlib import Path

    assert extractor_for(Path("plan.mpp")) is not None


# --- the message that contradicted itself ----------------------------------

def test_a_capped_sheet_is_not_reported_as_a_damaged_file():
    """**Both halves of this appeared in the same log line.**

        Cannot read '...JP Compass Dicer EMEA V0.2.xlsx' - it is encrypted or
        damaged. | Sheet 'Detailed' has more than 5,000 rows. The first 5,000
        were indexed; the rest were not...

    The file read perfectly. Only the headline was wrong, and it was the half
    somebody skims.
    """
    from app.core.errors import make_error

    error = make_error("ERR_FILE_TRUNCATED", "extract.xlsx", path="D:/big.xlsx")
    rendered = error.render()

    assert "encrypted or damaged" not in rendered
    assert "Only part" in rendered


@pytest.mark.parametrize("module,component", [
    ("app.extract.office", "extract.xlsx"),
    ("app.extract.xls", "extract.xls"),
])
def test_both_spreadsheet_readers_use_the_truncation_code(module, component):
    """`xls.py` copied the mistake from `office.py` when it was written, so
    both had to be fixed. A test over both is what stops the next copy."""
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module))
    assert "ERR_FILE_TRUNCATED" in source, f"{module} still calls a capped sheet corrupt"


# --- the warnings that broke the progress line ------------------------------

def test_openpyxl_warnings_are_suppressed_across_the_whole_read():
    """**`yield from`, not `return`.**

    `extract` is a generator. Returning the inner iterator would leave the
    `catch_warnings` block before a single row was read, so every warning would
    fire anyway - the fix would look right and change nothing.
    """
    import inspect

    from app.extract.office import XlsxExtractor

    body = inspect.getsource(XlsxExtractor.extract)
    assert "catch_warnings" in body
    assert "yield from" in body, (
        "returning the inner generator exits the warnings filter before it is used"
    )


def test_the_filter_is_scoped_not_global():
    """A blanket `warnings.filterwarnings` at import time would also hide a
    warning worth reading from somewhere else entirely."""
    import inspect

    from app.extract import office

    module_body = inspect.getsource(office)
    assert "simplefilter" in module_body
    # Inside a function, not at module scope.
    assert not any(
        line.startswith("warnings.simplefilter") or line.startswith("warnings.filterwarnings")
        for line in module_body.splitlines()
    )


# --- the switch has a control now (review 2026-10-08) -------------------------------------

def test_the_jvm_switch_is_a_setting_with_a_surface():
    """`LEASHA_ENABLE_JVM` was the whole interface: a tunable with no control,
    non-negotiable 11 broken. The Settings control is the ordinary route; the
    variable stays as the one-run override, as `LEASHA_PDF_OCR_PAGES` does."""
    from app.core.settings_registry import SURFACES, by_key

    setting = by_key("JVM_READERS_ENABLED")
    assert setting is not None and setting.kind == "bool"
    assert setting.default is False, "a JVM fault ends the run; off unless asked for"
    assert setting.surface in SURFACES


def test_the_setting_turns_the_reader_on_and_the_variable_still_wins(monkeypatch):
    from app.extract import diagrams

    monkeypatch.delenv(diagrams.JVM_SWITCH, raising=False)
    monkeypatch.setattr(diagrams, "_SETTINGS_JVM", True)
    assert diagrams._jvm_allowed() is True

    monkeypatch.setattr(diagrams, "_SETTINGS_JVM", False)
    assert diagrams._jvm_allowed() is False

    monkeypatch.setenv(diagrams.JVM_SWITCH, "1")       # one run, whatever Settings says
    assert diagrams._jvm_allowed() is True
    monkeypatch.setenv(diagrams.JVM_SWITCH, "")        # set but empty: off, deliberately
    monkeypatch.setattr(diagrams, "_SETTINGS_JVM", True)
    assert diagrams._jvm_allowed() is False


def test_the_setting_reaches_config():
    from app.core.config import Settings

    assert "jvm_readers_enabled" in Settings.model_fields
