r"""Invalidating the search cache from outside a write batch.

Layer: L1/L5

**A found gap, not a designed feature.** Every existing generation bump
happens inside a `write()` block, because it just changed rows the search
cache is keyed on. Changing a file-type mapping (`FileTypesEditor.save()`)
is different: it writes a config file on disk, not a table - so there was
no natural `write()` block for the bump to piggy-back on, and it simply
never happened. `SqliteStore.bump_generation()` is the missing public entry
point; `MainWindow._file_types_saved` (`app/ui/shell.py`) is its caller.
"""

from __future__ import annotations

from app.storage.sqlite_store import SqliteStore


def test_bump_generation_increments_outside_a_write_block(tmp_path) -> None:
    """The whole point: no write() call surrounds this from the caller's side."""
    with SqliteStore(tmp_path / "index.db") as store:
        before = store.generation
        store.bump_generation()
        assert store.generation == before + 1


def test_bump_generation_is_visible_to_another_connection(tmp_path) -> None:
    """The search cache reads generation from its own connection - a bump
    from the settings/UI side must be visible there, not just to the writer."""
    path = tmp_path / "index.db"
    with SqliteStore(path) as writer:
        before = writer.generation
        writer.bump_generation()

    with SqliteStore(path) as reader:
        assert reader.generation == before + 1
