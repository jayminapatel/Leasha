r"""The Settings page's Mail archives list is read again each time the page is opened.

Layer: L5

2026-10-16, the owner: "why has the pst files not in the settings page". The list was read
when the window opened and when a run finished. After a reset the window opened on an empty
index, the first run listed 21 archives, and the page could not show them for the hours
that run would take.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.mark.gui
def test_an_archive_listed_by_a_run_in_progress_shows_when_settings_is_opened(
        gui_mainwindow, qtbot, tmp_path):
    app, window, store, _engine = gui_mainwindow
    path = str(tmp_path / "2009.pst")
    store.upsert_file(path, parent_dir=str(tmp_path), ext="pst", size_bytes=1,
                      mtime_ns=1, status="PENDING", source_kind="file")
    box = window.settings_view.mail_archives
    assert path not in [row["path"] for row in box._rows], "it was listed before the visit"

    window.rail.setCurrentIndex(window._tab_index[window.settings_view])
    qtbot.waitUntil(lambda: path in [row["path"] for row in box._rows], timeout=10_000)
