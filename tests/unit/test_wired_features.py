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


# ---------------------------------------------------------------------------
# Adoptions section 1 - "Why is this here?"
# ---------------------------------------------------------------------------

def _first_row_point(view):
    index = view.results._model.index(0, 0)
    return view.results._list.visualRect(index).center()


def _type_and_settle(qtbot, app, view, text):
    view.input.clear()
    gui_pump(app)
    qtbot.keyClicks(view.input, text)
    qtbot.waitUntil(lambda: view.results._model.rowCount() > 0, timeout=3000)
    qtbot.wait(700)
    gui_pump(app)


def test_the_result_menu_offers_why_and_it_answers_in_plain_words(gui_mainwindow, qtbot, monkeypatch):
    import app.ui.results_view as results_module

    app, window, store, engine = gui_mainwindow
    view = window.search_view
    _type_and_settle(qtbot, app, view, "barnsley")

    captured = {}
    monkeypatch.setattr(results_module, "show_for",
                        lambda widget, point, path, actions: captured.update(actions=actions))
    shown = []
    monkeypatch.setattr(results_module, "show_why",
                        lambda parent, row, terms, prefs: shown.append((row, terms, prefs)))

    view.results._on_context_menu(_first_row_point(view))
    actions = captured["actions"]
    assert actions.explain is not None, "the menu must offer Why is this here?"

    actions.explain()
    row, terms, prefs = shown[0]
    assert "barnsley" in [t.lower() for t in terms]

    from app.ui.widgets.why_dialog import why_text
    body = why_text(row, terms, prefs)
    assert "barnsley" in body.lower(), body
    assert "score" not in body.lower(), "facts, never scores"


def test_switching_explanations_off_removes_the_menu_entry(gui_mainwindow, qtbot, monkeypatch):
    import app.ui.results_view as results_module

    app, window, store, engine = gui_mainwindow
    view = window.search_view
    _type_and_settle(qtbot, app, view, "barnsley")
    captured = {}
    monkeypatch.setattr(results_module, "show_for",
                        lambda widget, point, path, actions: captured.update(actions=actions))
    monkeypatch.setattr(view, "_search_preferences", {"explain_results": False})

    view.results._on_context_menu(_first_row_point(view))
    assert captured["actions"].explain is None


def test_a_meaning_only_row_says_so(gui_mainwindow):
    from app.ui.presenter import ResultRow, Snippet, why_lines

    row = ResultRow(rank=0, chunk_id=1, file_id=1, path="C:/a.txt", display_path="a.txt",
                    snippet=Snippet("x", ()), explain="", sources=(1,), text="unrelated words")
    assert any("meaning" in line.lower() for line in why_lines(row, ["invoice"]))
    assert why_lines(row, ["invoice"], {"explain_results": False}) == ()


def test_the_dialog_says_when_there_is_nothing_more(gui_mainwindow):
    from app.ui.presenter import ResultRow, Snippet
    from app.ui.widgets.why_dialog import NOTHING_MORE, why_text

    row = ResultRow(rank=0, chunk_id=1, file_id=1, path="C:/a.txt", display_path="a.txt",
                    snippet=Snippet("x", ()), explain="", sources=(), text="")
    assert why_text(row, [], {}) == NOTHING_MORE
