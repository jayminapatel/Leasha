"""Help > About Leasha. 2026-10-04.

Layer: L5

From the brand assessment: the window said the version on the splash and
nothing else - no entity, no notices. The box says who made it, which build
this is, and opens the third-party notices; nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from app.ui.widgets.about_dialog import (
    ENTITY,
    WEBSITE,
    AboutDialog,
    build_line,
    lockup_path,
    notices_path,
)

pytestmark = pytest.mark.gui
ROOT = Path(__file__).resolve().parents[2]


def test_the_build_line_says_the_version_and_the_git_description_only_when_it_adds_something():
    assert build_line({"version": "0.3.4"}) == "Version 0.3.4"
    assert build_line({"version": "0.3.4", "git": "v0.3.4"}) == "Version 0.3.4"
    assert build_line({"version": "0.3.4", "git": "v0.3.4-12-gabc1234-dirty"}) == (
        "Version 0.3.4 (v0.3.4-12-gabc1234-dirty)")
    assert build_line({"version": "0.3.4", "git_error": "no git"}) == "Version 0.3.4"


def test_the_notices_file_it_opens_is_the_one_in_the_repository():
    assert notices_path() == ROOT / "docs" / "THIRD_PARTY_NOTICES.md"
    assert notices_path().is_file()


def test_the_box_carries_the_logo_the_entity_the_version_and_two_buttons(qtbot):
    from app.core.version import version

    box = AboutDialog()
    qtbot.addWidget(box)
    assert box.windowTitle() == "About Leasha"
    assert box.logo.pixmap() is not None and not box.logo.pixmap().isNull(), "no logo"
    assert ENTITY in box.entity.text() and WEBSITE in box.entity.text()
    assert f"Version {version()}" in box.version.text()
    assert box.notices.text() == "Third-party notices"
    assert box.close_button.text() == "Close"
    assert box.notices.property("buttonRole") == "secondary"
    assert not box.notices.icon().isNull()


def test_the_lockup_follows_the_ground():
    from app.ui.theme import PALETTES, Theme

    assert lockup_path(PALETTES[Theme.LIGHT]).name == "leasha-lockup.png"
    assert lockup_path(PALETTES[Theme.DARK]).name == "leasha-lockup-reversed.png"
    for theme in (Theme.LIGHT, Theme.DARK):
        assert lockup_path(PALETTES[theme]).is_file()


def test_close_closes_it(qtbot):
    box = AboutDialog()
    qtbot.addWidget(box)
    box.show()
    box.close_button.click()
    assert not box.isVisible()


def test_opening_the_notices_hands_the_file_to_the_desktop(qtbot, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url) or True))
    box = AboutDialog()
    qtbot.addWidget(box)
    assert box.open_notices() is True
    assert opened and opened[0].toLocalFile().endswith("THIRD_PARTY_NOTICES.md")


def test_a_copy_without_the_notices_file_says_so_rather_than_nothing(qtbot, monkeypatch, tmp_path):
    import app.ui.widgets.about_dialog as module

    monkeypatch.setattr(module, "notices_path", lambda: tmp_path / "missing.md")
    box = AboutDialog()
    qtbot.addWidget(box)
    assert box.open_notices() is False
    assert "not in this copy" in box.version.text()


def test_the_help_menu_offers_it(gui_mainwindow):
    _app, window, _store, _engine = gui_mainwindow
    from PySide6.QtWidgets import QMenu

    # `QMenu` by name: asking the first action for its menu's type destroyed
    # the File menu under PySide6 (see `tools/guide_pictures.menu_of`).
    help_menu = next(m for m in window.menuBar().findChildren(QMenu)
                     if m.title().replace("&", "") == "Help")
    texts = [a.text().replace("&", "") for a in help_menu.actions()]
    assert "About Leasha" in texts
