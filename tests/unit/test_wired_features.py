r"""Features that were built, tested and ticked - and that nothing in the window
could reach. Each test here presses the real control on the real `MainWindow`
(`gui_mainwindow`, order 0m's harness) and asserts the feature comes up.

The audit that found them: every public class or function under `app/` with no
caller outside its own file, checked against the work order that promised it.
A unit test of the widget alone cannot catch this class of gap - the widget
passes on its own, which is exactly how these shipped unwired.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt  # noqa: E402

from tests.unit.conftest import gui_pump  # noqa: E402

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# Order 0j section 2 - the Photo Tagger
# ---------------------------------------------------------------------------

def test_the_settings_button_opens_the_photo_tagger(gui_mainwindow, qtbot):
    from app.ui.widgets.photo_tagger_window import PhotoTaggerWindow

    app, window, store, engine = gui_mainwindow
    assert window._photo_tagger is None, "built lazily, not at startup"

    qtbot.mouseClick(window.settings_view.name_people_button, Qt.MouseButton.LeftButton)
    gui_pump(app)

    opened = window._photo_tagger
    assert isinstance(opened, PhotoTaggerWindow)
    assert opened.isVisible()
    assert opened.page is not None
    opened.close()


def test_opening_it_twice_reuses_one_window(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    window._open_photo_tagger()
    first = window._photo_tagger
    first.close()
    window._open_photo_tagger()
    assert window._photo_tagger is first
    assert first.isVisible()
    first.close()


def test_the_go_menu_has_people_in_photos(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    actions = [a for a in window._menu_actions if a.text() == "People in photos"]
    assert len(actions) == 1
    actions[0].trigger()
    gui_pump(app)
    assert window._photo_tagger is not None and window._photo_tagger.isVisible()
    window._photo_tagger.close()


def test_opening_a_photo_from_the_tagger_goes_through_the_window(gui_mainwindow, qtbot, monkeypatch):
    app, window, store, engine = gui_mainwindow
    seen = []
    monkeypatch.setattr(window, "_open_path", lambda path, *a, **k: seen.append(path))
    window._open_photo_tagger()
    # `opened` was connected to the bound method at construction; rebuild the
    # connection against the patched one by asking the window afresh.
    window._photo_tagger.close()
    window._photo_tagger = None
    window._open_photo_tagger()
    window._photo_tagger.opened.emit("C:/photos/a.jpg")
    assert seen == ["C:/photos/a.jpg"]
    window._photo_tagger.close()


def test_the_tagger_reloads_each_time_it_is_shown(gui_mainwindow, qtbot, monkeypatch):
    app, window, store, engine = gui_mainwindow
    window._open_photo_tagger()
    tagger = window._photo_tagger
    tagger.close()
    calls = []
    monkeypatch.setattr(tagger.page, "reload", lambda: calls.append(1))
    tagger.show()
    gui_pump(app)
    assert calls, "a window kept across two index runs must not show old piles"
    tagger.close()
