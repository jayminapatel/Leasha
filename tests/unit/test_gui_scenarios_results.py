r"""Order 0q section 8's last open item: "pytest-qt scenario per item (0m
convention)" - real keystrokes and clicks against the assembled
`MainWindow`, using order 0m's harness (`tests/unit/conftest.py`'s
`gui_mainwindow`), which did not exist when this bullet was first written
and left deliberately unticked for exactly that reason (see the order's own
2026-09-05 dated note).

Each scenario below mirrors an already-covered isolated-widget test
(named in its own docstring) through the real window instead, per the
"not signal introspection, not one view built in isolation" rule
`docs/WORKORDER-CONVENTIONS.md` S5b states.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PyQt6")

from tests.unit.conftest import gui_pump  # noqa: E402

pytestmark = pytest.mark.gui


def _seed_mail_row(store, subject: str, sender: str, text: str) -> int:
    """The `mixed_engine` recipe (`test_mini_search.py`) - there is no
    `upsert_message` convenience method, so the `messages` row is a direct
    insert, matching that fixture exactly rather than inventing a second
    construction path."""
    message_id = store.upsert_file(
        f"pst://msg/{subject}", parent_dir="pst://msg", size_bytes=1, mtime_ns=1,
        status="INDEXED", source_kind="pst_message")
    with store.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(file_id, subject, sender, recipients, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, subject, sender, json.dumps([]), 0, 0))
    store.replace_chunks(message_id, [{"ordinal": 0, "text": text}])
    return message_id


# ---------------------------------------------------------------------------
# chevron / multi-match grouping - mirrors
# test_a_click_on_the_chevron_expands_the_group (test_results_view.py)
# ---------------------------------------------------------------------------

def test_a_document_matched_twice_shows_a_chevron_and_expands_on_click(gui_mainwindow, qtbot):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QStyleOptionViewItem

    app, window, store, engine = gui_mainwindow
    view = window.search_view

    file_id = store.upsert_file(
        "C:/work/widget-project.txt", parent_dir="C:/work", ext="txt",
        size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file")
    store.replace_chunks(file_id, [
        {"ordinal": 0, "text": "widgetmatch assembly notes"},
        {"ordinal": 1, "text": "widgetmatch shipping schedule"},
    ])

    view.input.clear()
    gui_pump(app)
    qtbot.keyClicks(view.input, "widgetmatch")
    qtbot.waitUntil(lambda: view.results._model.rowCount() > 0, timeout=3000)
    # Let both the interim and the full tier land before touching the model -
    # a click mid-rebuild is a race against the debounce timers, not the
    # chevron behaviour this test is actually about.
    qtbot.wait(700)
    gui_pump(app)

    from app.ui.result_delegate import ROLE_PAYLOAD

    index = view.results._model.index(0, 0)
    group = index.data(ROLE_PAYLOAD)
    assert group is not None
    assert group.match_count == 2, f"expected a 2-match group, got {group.match_count}"
    # Expand state is tracked on the view (`_expanded: set[file_id]`), not on
    # the immutable `ResultGroup` payload itself.
    assert group.file_id not in view.results._expanded

    option = QStyleOptionViewItem()
    view.results._list.initViewItemOption(option)
    option.rect = view.results._list.visualRect(index)
    chevron_rect = view.results._delegate.subtitle_rect(option, group)
    assert chevron_rect is not None, "a 2-match group must paint a chevron line"

    qtbot.mouseClick(view.results._list.viewport(), Qt.MouseButton.LeftButton,
                     pos=chevron_rect.center())
    gui_pump(app)

    assert group.file_id in view.results._expanded


# ---------------------------------------------------------------------------
# kind tags: mail renders sender-first - mirrors
# test_a_message_group_leads_with_the_sender_not_a_folder (test_result_groups.py)
# ---------------------------------------------------------------------------

def test_a_mail_result_is_labelled_by_sender_not_by_folder(gui_mainwindow, qtbot):
    from app.ui.result_delegate import ROLE_PAYLOAD

    app, window, store, engine = gui_mainwindow
    view = window.search_view

    _seed_mail_row(store, "Widget delivery", "chris@example.com",
                   "widgetmail delivery confirmation")

    view.input.clear()
    gui_pump(app)
    qtbot.keyClicks(view.input, "widgetmail")
    qtbot.waitUntil(lambda: view.results._model.rowCount() > 0, timeout=3000)

    # `kind`/the sender-first name arrive via `decorate_results_async`
    # (`search_view.py`), a second, worker-driven repaint that lands after
    # the first - not the same turn as the initial `show_results`.
    def _decorated() -> bool:
        payload = view.results._model.index(0, 0).data(ROLE_PAYLOAD)
        return payload is not None and payload.kind == "email"

    qtbot.waitUntil(_decorated, timeout=3000)
    group = view.results._model.index(0, 0).data(ROLE_PAYLOAD)
    assert "chris@example.com" in group.name
    assert "pst" not in group.name.lower()


# ---------------------------------------------------------------------------
# stable update: interim -> full swap keeps the selected document current -
# mirrors test_the_current_row_survives_a_rebuild_that_adds_rows
# (test_results_view.py), driven through the real dispatch chain instead of
# calling ResultsView.show_results(keep_scroll=True) directly.
# ---------------------------------------------------------------------------

def test_the_selected_row_survives_the_interim_to_full_swap(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    view = window.search_view

    for name, text in (
        ("alpha.txt", "stableswap alpha document"),
        ("beta.txt", "stableswap beta document"),
        ("gamma.txt", "stableswap gamma document"),
    ):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext="txt", size_bytes=1,
            mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    view.input.clear()
    gui_pump(app)
    qtbot.keyClicks(view.input, "stableswap")
    # The interim tier only (150ms) - not the full tier (400ms) yet.
    qtbot.waitUntil(lambda: view.results._model.rowCount() >= 3, timeout=3000)

    view.results._list.setCurrentIndex(view.results._model.index(1, 0))
    selected = view.results.current_row()
    assert selected is not None
    selected_file_id = selected.file_id

    # Let the full tier land.
    qtbot.wait(700)
    gui_pump(app)
    qtbot.waitUntil(lambda: view.results._model.rowCount() > 0, timeout=2000)

    after = view.results.current_row()
    assert after is not None, "the selection was lost across the tier swap"
    assert after.file_id == selected_file_id, (
        f"selection moved from file {selected_file_id} to {after.file_id} across "
        "the interim-to-full swap - the real dispatch path does not preserve it "
        "the way the isolated ResultsView.show_results(keep_scroll=True) test does")


# ---------------------------------------------------------------------------
# The launched app said "Nothing selected" three times out of three when a result
# was clicked and the preview opened within ~2 s of the rows appearing (order 0m,
# 2026-09-20 note). The ordering that does it: the rows on screen are replaced by
# a search for *different text* (a slow typist, or a busy machine, gets the
# interim tier of "swapproof" and then the full tier of "swapproof alpha"). The
# selection was kept only when the query text was unchanged, so the click was
# silently dropped and `current_row()` was None when the pane was switched on.
# ---------------------------------------------------------------------------

def test_a_clicked_result_is_still_previewed_after_the_rows_are_replaced_by_a_longer_query(
        gui_mainwindow, qtbot):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QToolButton

    app, window, store, engine = gui_mainwindow
    view = window.search_view

    for name, text in (
        ("swapproof-alpha.txt", "swapproof alpha document about valves"),
        ("swapproof-beta.txt", "swapproof beta document about pumps"),
    ):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext="txt", size_bytes=1,
            mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    toggle = [b for b in window.findChildren(QToolButton) if b.objectName() == "toggle_inspector"][0]
    if toggle.isChecked():                       # the pane must be OFF when the click lands
        qtbot.mouseClick(toggle, Qt.MouseButton.LeftButton)

    view.input.clear()
    gui_pump(app)
    qtbot.keyClicks(view.input, "swapproof")
    view.search_now()
    qtbot.waitUntil(lambda: view.results._model.rowCount() >= 2, timeout=5000)

    # Click the beta row (whichever position it has), as a person does.
    target = None
    for row in range(view.results._model.rowCount()):
        view.results._list.setCurrentIndex(view.results._model.index(row, 0))
        current = view.results.current_row()
        if current is not None and "beta" in current.path:
            target = current
            break
    assert target is not None

    # The rows are replaced by a search for different text that still finds it.
    qtbot.keyClicks(view.input, " beta")         # typed on, never cleared - as a person does
    view.search_now()
    qtbot.waitUntil(lambda: view._shown_query == "swapproof beta" and len(view.results._rows) >= 1,
                    timeout=5000)
    qtbot.wait(700)                              # the metadata redraw lands too
    gui_pump(app)

    current = view.results.current_row()
    assert current is not None, "the click was dropped when the rows were replaced"
    assert "beta" in current.path

    # Now open the preview: it shows the file, not "Nothing selected".
    qtbot.mouseClick(toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "beta" in view.preview.title.text().lower(), timeout=4000)
    assert view.preview.title.text() != "Nothing selected"
    qtbot.mouseClick(toggle, Qt.MouseButton.LeftButton)   # leave the pane as found
    view.input.clear()
