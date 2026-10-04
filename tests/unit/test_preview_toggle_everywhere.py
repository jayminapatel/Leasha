r"""The Preview icon toggle, the same on every tab that has a preview. 2026-10-04.

Layer: L5

The owner: "there are icons in the search page for preview etc .. why are they
not in the other tabs? i.e consistent". The Search bar's toggle came with its
redesign; Files, Mail and Code had the same preference inside the View menu
only, and Chat's new preview had no toggle at all. One control now
(`view_options.preview_toggle`), on all five.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from tests.unit.conftest import gui_pump  # noqa: E402

pytestmark = pytest.mark.gui

TABS = ("search_view", "files_view", "mail_view", "code_view", "chat_view")


def _toggle(view):
    toggles = getattr(view, "toggles", None)
    assert toggles and "inspector" in toggles, f"{type(view).__name__} has no Preview toggle"
    return toggles["inspector"]


def test_every_tab_with_a_preview_carries_the_same_toggle(gui_mainwindow):
    app, window, _store, _engine = gui_mainwindow
    gui_pump(app)
    seen = []
    for name in TABS:
        toggle = _toggle(getattr(window, name))
        assert toggle.objectName() == "toggle_inspector"
        assert toggle.icon_name == "panel-right" and not toggle.icon().isNull()
        assert toggle.toolTip().startswith("Show or hide the preview pane")
        assert toggle.accessibleName() == "Preview pane"
        assert toggle.isCheckable() and toggle.property("iconToggle") is True
        seen.append((toggle.toolTip(), toggle.accessibleName()))
    assert len(set(seen)) == 1, "the five toggles do not say the same thing"


@pytest.mark.parametrize("name", ["files_view", "mail_view", "code_view"])
def test_the_toggle_flips_the_tabs_own_preference_and_follows_the_menu(gui_mainwindow, name):
    """What the View menu's "Preview" does, one click nearer - and the two
    never disagree: a change from the menu (or Ctrl+Shift+P) moves the toggle."""
    app, window, _store, _engine = gui_mainwindow
    view = getattr(window, name)
    window._show(view)
    gui_pump(app)
    toggle = _toggle(view)
    button = view.view_button
    before = bool(button.prefs.preview)
    assert toggle.isChecked() == before

    toggle.click()
    gui_pump(app)
    assert bool(button.prefs.preview) == (not before), "the toggle did not flip the preference"
    assert toggle.isChecked() == (not before)
    pane = getattr(view, "preview", None) or view.results.preview
    assert pane.isHidden() == before, "the pane did not follow the toggle"

    button.toggle_preview()                      # the menu's and the shortcut's route
    gui_pump(app)
    assert bool(button.prefs.preview) == before
    assert toggle.isChecked() == before, "the toggle did not follow the menu"


def test_the_chat_toggle_hides_the_sources_preview_and_is_remembered(gui_mainwindow, qtbot):
    app, window, store, _engine = gui_mainwindow
    view = window.chat_view
    window._show(view)
    gui_pump(app)
    toggle = _toggle(view)
    pane = view.sources.preview
    view.show_preview(True)
    assert toggle.isChecked() and not pane.isHidden()

    heard = []
    view.preview_toggled.connect(heard.append)
    toggle.click()
    gui_pump(app)
    assert pane.isHidden() and heard == [False]
    qtbot.waitUntil(lambda: store.get_state("ui:chat_preview", "") == "off", timeout=5000)

    view.show_preview(True)
    gui_pump(app)
    assert not pane.isHidden() and toggle.isChecked()
    qtbot.waitUntil(lambda: store.get_state("ui:chat_preview", "") == "on", timeout=5000)


def test_a_theme_change_redraws_every_toggle(gui_mainwindow):
    from app.ui.theme import theme_colours
    from app.ui.view_options import retint_toggles

    app, window, _store, _engine = gui_mainwindow
    colours = theme_colours()
    for name in TABS:
        view = getattr(window, name)
        toggle = _toggle(view)
        toggle.setIcon(toggle.icon().__class__())      # blank it
        assert toggle.icon().isNull()
        retint_toggles(view, colours)
        assert not toggle.icon().isNull(), f"{name}: the toggle was not redrawn"
