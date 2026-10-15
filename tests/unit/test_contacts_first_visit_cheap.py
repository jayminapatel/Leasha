"""The Contacts page's first visit does less on the window's thread (2026-10-15).

Layer: L5

`test_navigation_is_instant.py` failed on every full local run: Contacts' first visit took 440-735 ms
against 400. Measured in `gui_mainwindow`, the visit imported `app.ui.contacts.page` and its modules
(150-250 ms), built the editor nobody sees until a contact is picked (70-90 ms) and opened an SVG
file for each new icon colour or size (16-26 ms). These tests pin the three savings by what is built
and read, not by a clock, so a slow laptop cannot make them flaky and a regression cannot hide in a
fast one:

* the editor is not built by the page, the first read, or a sidebar pick - only when a contact is
  opened, a new one is started, or something asks for `page.editor`; and it then behaves as before;
* `ContactsView.warm` imports the page's code on a worker;
* an icon's SVG text is read once, whatever colours and sizes it is drawn in.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.unit.test_contacts_view_world import card, settle, world      # noqa: E402,F401


def test_the_page_and_its_first_read_do_not_build_the_editor(world):
    world.add(card("Ann", "Lee", email="ann@x.example"))
    page = world.page()
    assert page.model.rowCount() == 1
    assert not page.editor_built()
    assert page.right.currentWidget() is page.placeholder
    assert not page.is_editing()
    # The read view still has the panels the editor drives, from the start.
    assert page.enrich_panel.parent() is not None and page.index_panel.parent() is not None
    # A sidebar pick and Ctrl+S with nothing open do not build it either.
    page._picked(page._entry)
    settle(page)
    page._save_if_dirty()
    page.edit_current()
    assert not page.editor_built()


def test_picking_a_contact_builds_the_editor_with_what_the_page_read(world):
    cid = world.add(card("Ann", "Lee", email="ann@x.example"))
    page = world.page()
    page.select_ids([cid])
    settle(page)
    assert page.editor_built()
    ed = page.editor
    assert ed.cid == cid and ed.isEnabled()
    # It is configured from the snapshot that landed before it existed, and lent the page's panels.
    assert set(ed._accounts) == {world.a_home, world.a_work, world.a_fast}
    assert ed._names[world.a_home] == "Home Google"
    assert ed.enrich_panel is page.enrich_panel and ed.index_panel is page.index_panel
    # It sits between the read view and the history, as it did when it was built with the page.
    assert [page.right.widget(i) for i in range(page.right.count())] == [
        page.placeholder, page.reader, ed, page.history]
    page.edit_current()
    assert page.is_editing()


def test_a_new_contact_builds_the_editor(world):
    page = world.page()
    assert not page.editor_built()
    page.new_contact()
    assert page.editor_built() and page.is_editing()
    assert page.editor.cid is None and page.editor.isEnabled()


def test_warm_imports_the_page_on_a_worker(qapp, tmp_path, monkeypatch):
    import threading

    from PySide6.QtCore import QThreadPool

    import app.ui.contacts_view as cv
    from app.storage.sqlite_store import SqliteStore

    seen: list = []
    real = cv._import_page
    monkeypatch.setattr(cv, "_import_page", lambda: (seen.append(threading.current_thread()), real())[1])
    store = SqliteStore(tmp_path / "w.db").connect()
    try:
        view = cv.ContactsView(store)
        assert view.warm() is not None
        QThreadPool.globalInstance().waitForDone(10000)
        assert seen and seen[0] is not threading.main_thread()
        assert view.page is None                           # warmed, not built
    finally:
        store.close()


def test_an_icon_file_is_read_once_for_every_colour_and_size(monkeypatch):
    from pathlib import Path

    from app.ui.widgets import icons

    icons._texts.pop("star", None)
    reads: list = []
    real = Path.read_text

    def counting(self, *a, **k):
        if self.name == "star.svg":
            reads.append(self)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    for colour in ("#101010", "#202020", "#303030"):
        for size in (16, 48):
            assert not icons.icon("star", colour, size=size).isNull()
    assert len(reads) == 1


def test_preload_reads_every_shipped_icon_without_qt(monkeypatch):
    from app.ui.widgets import icons

    monkeypatch.setattr(icons, "_texts", {})
    assert icons.preload() == len(icons.ICON_NAMES)
    assert all("currentColor" in text or "<svg" in text for text in icons._texts.values())
