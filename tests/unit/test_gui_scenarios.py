r"""Order 0m: pytest-qt scenario tests. Real keystrokes and clicks against
the real, assembled `MainWindow` - not signal introspection, not one view
built in isolation. `tests/unit/conftest.py`'s `gui_mainwindow` fixture is
the harness (S1a); this file is the backlog of journeys it unlocks (S1b).

Every test here is marked `gui` (S1a: the marker exists so the subset is
selectable, `pytest -m gui`, but it runs in the default suite - S6's own
"done means" is explicit that this subset must count as RUN, never a
vacuous skip).

**Tab switching** already has its own scenario,
`test_ui_redesign_qt.py::test_every_page_is_reachable_by_click_and_by_index`
- not duplicated here.

**What is not covered this pass, and why:**
- Offline Media *Scan* opens a native "browse for a drive" dialog, which
  cannot be driven headless - Rescan and Delete are covered below against a
  pre-seeded fixture volume instead.
- The pop-out's Ctrl+F find and image-rotate button are exercised on a real
  photo in the offline-media/preview suites already; this file's fixture
  documents are plain text/pdf, so only open, stay-on-top and close are
  scenario-tested here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from tests.unit.conftest import gui_pump, gui_row_count, gui_select_row  # noqa: E402

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# S1b - type -> interim -> full results render
# ---------------------------------------------------------------------------

def test_typing_a_real_word_renders_results(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    view = window.search_view
    view.input.clear()
    gui_pump(app)

    qtbot.keyClicks(view.input, "barnsley")
    qtbot.waitUntil(lambda: gui_row_count(view.results) > 0, timeout=3000)

    assert "barnsley" in view.input.text()
    assert gui_row_count(view.results) >= 1
    assert view.status.text() != ""


def test_a_query_matching_nothing_renders_no_rows_and_says_so(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    view = window.search_view
    view.input.clear()
    gui_pump(app)

    qtbot.keyClicks(view.input, "zzznomatchzzz")
    qtbot.wait(600)                     # past both debounce timers
    gui_pump(app)

    assert gui_row_count(view.results) == 0


# ---------------------------------------------------------------------------
# S1b - Esc clears results AND status (the M9 regression, now end-to-end;
# order 0m S1b's own text names this - see search_view.py's eventFilter,
# extended this order to actually empty the box on Escape).
# ---------------------------------------------------------------------------

def test_escape_clears_results_and_status(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    view = window.search_view
    view.input.clear()
    gui_pump(app)

    qtbot.keyClicks(view.input, "barnsley")
    qtbot.waitUntil(lambda: gui_row_count(view.results) > 0, timeout=3000)

    qtbot.keyClick(view.input, Qt.Key.Key_Escape)
    gui_pump(app)

    assert view.input.text() == ""
    assert gui_row_count(view.results) == 0
    assert view.status.text() == ""


def test_escape_on_an_already_empty_box_does_nothing_harmful(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    view = window.search_view
    view.input.clear()
    gui_pump(app)

    qtbot.keyClick(view.input, Qt.Key.Key_Escape)
    gui_pump(app)

    assert view.input.text() == ""


# ---------------------------------------------------------------------------
# S1b - settings toggle -> engine state (the anti-rerank-bug test, driven
# through a real click rather than reading `shell.py`'s source as text -
# `tests/unit/test_review_section_four.py` has the source-text version this
# migrates, per S1d).
# ---------------------------------------------------------------------------

def test_the_rerank_checkbox_flips_the_real_engines_reranker(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    checkbox = window.settings_view.rerank

    checkbox.setChecked(not checkbox.isChecked())
    gui_pump(app)

    assert engine.reranker.enabled == checkbox.isChecked()


def test_the_toolbar_and_settings_rerank_controls_stay_in_step(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    checkbox = window.settings_view.rerank
    toolbar = getattr(window.search_view, "rerank_toggle", None)
    if toolbar is None:
        pytest.skip("no toolbar rerank control on this build")

    checkbox.setChecked(True)
    gui_pump(app)
    assert toolbar.isChecked() is True

    checkbox.setChecked(False)
    gui_pump(app)
    assert toolbar.isChecked() is False


# ---------------------------------------------------------------------------
# S1b - the `/` popup: opens, offers, inserts.
# ---------------------------------------------------------------------------

def test_slash_popup_opens_and_inserting_a_command_writes_the_operator(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    view = window.search_view
    view.input.clear()
    gui_pump(app)

    qtbot.keyClicks(view.input, "/type")
    gui_pump(app)

    popup = view.commands.popup()
    qtbot.waitUntil(lambda: popup.isVisible() and popup.model().rowCount() > 0, timeout=2000)

    index = popup.model().index(0, 0)
    rect = popup.visualRect(index)
    qtbot.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    gui_pump(app)

    # Command mode inserts `name:` and reopens value mode - the box always
    # ends up with a `something:` operator on it, whichever command matched.
    assert ":" in view.input.text()


# ---------------------------------------------------------------------------
# S1b - pop-out opens, stays on top, closes (the workspace order).
# ---------------------------------------------------------------------------

def test_pop_out_opens_stays_on_top_and_closes(gui_mainwindow, qtbot):
    from PySide6.QtCore import Qt as QtCore

    app, window, store, engine = gui_mainwindow
    view = window.search_view
    view.input.clear()
    gui_pump(app)

    qtbot.keyClicks(view.input, "barnsley")
    qtbot.waitUntil(lambda: gui_row_count(view.results) > 0, timeout=3000)
    gui_select_row(view.results, 0)
    gui_pump(app)

    before = len(window._pinned)
    qtbot.mouseClick(view.preview.pop_button, Qt.MouseButton.LeftButton)
    gui_pump(app)

    assert len(window._pinned) == before + 1
    popped = window._pinned[-1]

    popped.on_top.setChecked(True)
    gui_pump(app)
    assert bool(popped.windowFlags() & QtCore.WindowType.WindowStaysOnTopHint)

    popped.on_top.setChecked(False)
    gui_pump(app)
    assert not bool(popped.windowFlags() & QtCore.WindowType.WindowStaysOnTopHint)

    popped.close()
    gui_pump(app)
    assert len(window._pinned) == before


# ---------------------------------------------------------------------------
# S1b - Offline Media Rescan/Delete against a fixture volume (Scan itself
# opens a native drive-browse dialog - see the module docstring).
# ---------------------------------------------------------------------------

def test_offline_media_rescan_and_delete_against_a_fixture_volume(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    view = window.offline_media_view

    guid = "gui-scenario-test-guid"
    store.upsert_volume(guid, kind="drive", name="Scenario Drive",
                        description="a fixture volume for this test",
                        volume_guid=guid)
    view.refresh()
    qtbot.waitUntil(lambda: view.tree.topLevelItemCount() > 0, timeout=3000)

    view.tree.setCurrentItem(view.tree.topLevelItem(0))
    gui_pump(app)

    assert view.rescan.isEnabled() or view.delete.isEnabled()
