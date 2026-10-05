"""The reader chosen for Outlook archives is the one the page shows.

2026-10-05. The owner's index had "Through Outlook" saved, every run obeyed
it, and the drop-down opened on "Automatic - direct if possible, else Outlook"
every time because nothing ever set it from what was saved. "It should have
read them direct." These fail on the code before the fix.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")


@pytest.fixture()
def window(tmp_path):
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication

    from tools import grab_ui

    def build(saved: str):
        from app.core.config import load_settings
        from app.storage.sqlite_store import SqliteStore

        env = tmp_path / ".env"
        env.write_text(grab_ui.ENV.format(d=tmp_path.as_posix()), encoding="utf-8")
        store = SqliteStore(load_settings(env).fts_db).connect()
        if saved:
            store.set_state("ui:pst_backend", saved)
        store.close()
        built.extend(grab_ui.build_window(tmp_path, theme="light", show=True))
        app, shown = built[0], built[1]
        grab_ui._reach(app, shown, {"page": "Indexing", "category": "What gets read"})
        return app, shown

    built: list = []
    yield build
    if built:
        app, shown, closers = built
        shown.close()
        QThreadPool.globalInstance().waitForDone(10_000)
        (QApplication.instance() or app).processEvents()
        for close in closers:
            close()
        # The reader is one object for the whole process; put it back.
        from pathlib import Path

        from app.extract.base import extractor_for

        extractor_for(Path("x.pst")).backend = "auto"


def test_a_saved_choice_of_outlook_is_what_the_drop_down_shows(window):
    app, shown = window("outlook")
    view = shown.settings_view
    assert view.pst_backend.currentData() == "outlook"
    assert view.pst_backend.currentText() == "Through Outlook (MAPI)"
    # And the sentence on Indexing, What gets read follows it.
    from PyQt6.QtWidgets import QLabel

    said = " ".join(label.text() for label in shown.indexing_view.findChildren(QLabel))
    assert "Outlook archives are read through Outlook." in said
    # Showing it is not choosing it: nothing was written back.
    assert shown._store.get_state("ui:pst_backend", "") == "outlook"


def test_nothing_saved_is_automatic_and_says_nothing_extra(window):
    _app, shown = window("")
    view = shown.settings_view
    assert view.pst_backend.currentData() == "auto"
    assert view.pst_note.isHidden()


def test_the_note_is_there_only_for_outlook_with_direct_reading_available(window):
    _app, shown = window("outlook")
    view = shown.settings_view
    view._pst_direct_available = True
    view._refresh_pst_note()
    assert not view.pst_note.isHidden()
    assert "Reading them directly is available" in view.pst_note.text()
    view._pst_direct_available = False
    view._refresh_pst_note()
    assert view.pst_note.isHidden(), "no direct reading: Outlook is the only way, nothing to add"
    view._pst_direct_available = True
    view.pst_backend.setCurrentIndex(view.pst_backend.findData("auto"))
    assert view.pst_note.isHidden(), "choosing Automatic takes the note away at once"
