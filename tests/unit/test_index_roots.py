r"""The command line and the window index the same folders.

Layer: L0 / L3

**One setting had two sources of truth.** The window saves "Folders to index"
under `ui:roots`; `app.cli index` only ever read its own arguments. So a
command-line run - including the one somebody uses to verify a migration, which
is the whole reason the command exists - indexed whatever folder was typed
rather than what the application is configured to index, and nothing afterwards
could tell the two apart.

Verifying the wrong thing and believing it was the right thing is the expensive
kind of wrong, and it is invisible: both runs print the same summary.

The rule, and the reason for each half:

* no folders on the command line falls back to the saved setting, **and says
  so** - a command that silently uses a setting produces output nobody can
  attribute;
* folders on the command line still win, because naming one is an instruction
  and ignoring it would be the same bug pointing the other way;
* neither, and it names the Settings page rather than only the syntax.
"""

from __future__ import annotations

import pytest

from app.cli import ROOTS_STATE_KEY, _saved_roots
from app.storage.sqlite_store import SqliteStore


class Settings:
    """Only the field `_saved_roots` reads."""

    def __init__(self, fts_db):
        self.fts_db = fts_db


@pytest.fixture()
def store_with(tmp_path):
    def make(value: str | None):
        database = tmp_path / "index.db"
        with SqliteStore(database) as store:
            if value is not None:
                store.set_state(ROOTS_STATE_KEY, value)
        return Settings(database)
    return make


# --- the key both sides use -------------------------------------------------

def test_the_window_and_the_command_line_use_one_key():
    """Named in `cli.py` and imported by the window, rather than spelled out in
    both. Two literals that must match are two literals that will not."""
    # Read as text rather than imported: `shell` pulls in QtWidgets, and a
    # repo-hygiene check that needs a display is one that does not run in CI or
    # on a headless machine - which is where a mismatch would first bite.
    from pathlib import Path

    # `_save_roots` moved out of `MainWindow` into `SettingsController`
    # (work order 202626082352 section 7); the window's method of that name
    # only forwards, so the key is named in the controller now.
    source = (Path(__file__).resolve().parents[2] / "app" / "ui" / "controllers"
              / "settings_controller.py").read_text(encoding="utf-8")

    assert "ROOTS_STATE_KEY" in source, (
        "the window writes its own literal; two literals that must match are "
        "two literals that will not")
    assert ROOTS_STATE_KEY == "ui:roots"


# --- reading it -------------------------------------------------------------

def test_the_saved_folders_are_found(store_with):
    settings = store_with(r"D:\Docs|D:\Projects")

    assert _saved_roots(settings) == [r"D:\Docs", r"D:\Projects"]


def test_a_single_folder_needs_no_separator(store_with):
    assert _saved_roots(store_with(r"D:\Docs")) == [r"D:\Docs"]


def test_blank_entries_are_dropped(store_with):
    """`|` joining an empty list, or a folder removed in the window, leaves
    separators behind. An empty string as a path is an index run over the
    current directory."""
    assert _saved_roots(store_with("|D:\\Docs||")) == [r"D:\Docs"]


def test_nothing_saved_is_an_empty_list_not_an_error(store_with):
    assert _saved_roots(store_with(None)) == []


def test_a_first_run_with_no_database_at_all_is_an_empty_list(tmp_path):
    """**The common case, not an edge case.** Somebody installs, opens a
    terminal and runs `index` before ever opening the window. A traceback about
    a missing database would be the first thing the application ever said."""
    assert _saved_roots(Settings(tmp_path / "not-created-yet.db")) == []


def test_a_database_that_cannot_be_read_is_an_empty_list(tmp_path):
    """A locked or corrupt store means "no saved folders" - which is what a
    fresh install has, and leads to the message naming the Settings page."""
    broken = tmp_path / "index.db"
    broken.write_bytes(b"this is not a database")

    assert _saved_roots(Settings(broken)) == []
