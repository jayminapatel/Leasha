r"""The quick search box, made movable and "world class". 2026-10-08.

Layer: L5.

The owner: *"the search from ctrl alt L is not moveable window which it should
be, can you see if you can improve that box behavior and layout to be world
class and modern and feature rich"* - and then chose all four of:

1. it moves, resizes, and reopens where it was left, on a screen that exists;
2. type chips - All, Files, Mail, Photos, Code - with the `/` menu beside them;
3. a preview beside the results;
4. keys for every action, and recent searches in an empty box.

`test_mini_search.py` holds what the box already promised (Workspace §3a,
Adoptions §4a and §5a); this file holds what the owner asked for on top.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                     # noqa: E402
from PySide6.QtGui import QKeyEvent                                # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from app.ui.widgets.mini_search import (                        # noqa: E402
    CHIP_ORDER, MiniSearch, chip_label, kind_bucket,
)
from tests.unit.test_mini_search import (                       # noqa: E402,F401 - fixtures
    _searched, engine, mixed_engine, qapp,
)


def _fresh_engine():
    """An engine over its own store, so a saved place or a logged search in
    one test is never another test's starting point."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "fresh.db").connect()
    for name, text in (("safety.txt", "the safety induction training record"),
                       ("survey.txt", "the leeds site survey, safety section")):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext="txt", size_bytes=1,
            mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

        def embed_all(self, _t):
            raise RuntimeError("no model")

    return SearchEngine(store, _NoVectors(), _NoModel()), store


def _pump(qapp, seconds=0.3, until=None):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        if until is not None and until():
            return
        time.sleep(0.01)


def _key(box, key, modifiers=Qt.KeyboardModifier.NoModifier):
    """A key press delivered where Qt delivers it: the box, through its filter."""
    from PySide6.QtCore import QEvent

    return box.eventFilter(box.box, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


def _mouse(widget, kind, global_point):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    types = {"press": QEvent.Type.MouseButtonPress, "move": QEvent.Type.MouseMove,
             "release": QEvent.Type.MouseButtonRelease}
    local = QPointF(widget.mapFromGlobal(global_point))
    held = Qt.MouseButton.NoButton if kind == "release" else Qt.MouseButton.LeftButton
    QApplication.sendEvent(widget, QMouseEvent(
        types[kind], local, QPointF(global_point), Qt.MouseButton.LeftButton, held,
        Qt.KeyboardModifier.NoModifier))


# ---------------------------------------------------------------------------
# 1. It moves, and opens where it was left
# ---------------------------------------------------------------------------

def test_dragging_the_top_bar_moves_it_and_it_reopens_there(qapp):
    r"""**The owner's words**: the box "is not moveable window which it should
    be". Pressed on its top bar and dragged, it moves; closed and summoned
    again - by a box built afresh, as after a restart - it is where it was
    left, at that size. The place is written by `state_writes`, off this thread.

    Offscreen there is no window manager, so this drives the hand-drawn move;
    on Windows `startSystemMove` hands the drag to the system (UNVERIFIED here).
    """
    from PySide6.QtCore import QPoint

    from app.ui.presenter.quick_search import PLACE_KEY
    from app.ui.state_writes import pool

    engine, store = _fresh_engine()
    try:
        box = MiniSearch(engine)
        box.summon()
        _pump(qapp, 0.1)
        start = box.pos()
        grab = box.header.mapToGlobal(QPoint(20, 10))
        _mouse(box.header, "press", grab)
        _mouse(box.header, "move", grab + QPoint(25, 18))
        _mouse(box.header, "release", grab + QPoint(25, 18))
        moved = box.pos()
        assert moved == start + QPoint(25, 18), (start, moved)
        size = box.size()
        box.dismiss()
        assert pool().waitForDone(5000)

        saved = json.loads(store.get_state(PLACE_KEY, ""))
        assert (saved["x"], saved["y"], saved["w"], saved["h"]) == (
            moved.x(), moved.y(), size.width(), size.height())

        again = MiniSearch(engine)
        again.summon()
        assert again.pos() == moved and again.size() == size
        again.dismiss()
    finally:
        engine.close()
        store.close()


def test_it_can_be_resized_and_has_a_grip_to_do_it(qapp, engine):
    from PySide6.QtWidgets import QSizeGrip

    from app.ui.presenter.quick_search import MIN_SIZE

    box = MiniSearch(engine)
    assert box.findChildren(QSizeGrip), "nothing to resize it with"
    assert (box.minimumWidth(), box.minimumHeight()) == MIN_SIZE
    box.summon()
    box.resize(780, 590)
    assert box.width() == 780 and box.height() == 590
    box.dismiss()


def test_a_place_on_a_screen_that_has_gone_comes_back_on_one_that_is_there():
    r"""A laptop undocked from the monitor the box was left on: the box comes
    back on the screen that exists, all of it visible - moved, not lost."""
    from app.ui.presenter.quick_search import default_place, fit_on_screens

    laptop = (0, 0, 1920, 1040)
    # Left on a second monitor to the right, which is no longer there.
    assert fit_on_screens((2400, 200, 720, 560), [laptop]) == default_place(laptop, (720, 560))
    # Half off the bottom-right edge: pulled back on, the same size.
    x, y, w, h = fit_on_screens((1700, 900, 720, 560), [laptop])
    assert (w, h) == (720, 560) and x + w <= 1920 and y + h <= 1040
    # Bigger than the screen: made to fit it.
    assert fit_on_screens((0, 0, 4000, 3000), [laptop]) == laptop
    # Two screens: the one it overlaps most keeps it, untouched.
    second = (1920, 0, 2560, 1400)
    assert fit_on_screens((2000, 100, 720, 560), [laptop, second]) == (2000, 100, 720, 560)
    assert fit_on_screens(None, []) is None


def test_a_saved_place_off_screen_is_clamped_when_the_box_opens(qapp):
    from app.ui.presenter.quick_search import PLACE_KEY, place_text

    engine, store = _fresh_engine()
    try:
        store.set_state(PLACE_KEY, place_text((9000, 9000, 700, 520)))
        box = MiniSearch(engine)
        box.summon()
        area = box.screen().availableGeometry()
        assert area.contains(box.geometry()), (area, box.geometry())
        box.dismiss()
    finally:
        engine.close()
        store.close()


def test_a_place_that_cannot_be_read_is_nothing_saved():
    from app.ui.presenter.quick_search import read_place

    assert read_place("") == (None, False, 0)
    assert read_place("{not json") == (None, False, 0)
    assert read_place('{"x": 1, "y": 2, "w": 0, "h": 5}') == (None, False, 0)


def test_the_hotkey_pressed_again_while_it_is_open_hides_it():
    r"""The global shortcut opens it; the same shortcut again, while it is
    open, puts it away - `_summon_mini` looks before it summons."""
    from types import SimpleNamespace

    from app.ui.shell import MainWindow

    calls: list = []
    box = SimpleNamespace(isVisible=lambda: True, dismiss=lambda: calls.append("dismiss"),
                          summon=lambda *a: calls.append("summon"))
    MainWindow._summon_mini(SimpleNamespace(_mini=box, _offer_foreground_selection=lambda: None))
    assert calls == ["dismiss"]


# ---------------------------------------------------------------------------
# 2. Type chips
# ---------------------------------------------------------------------------

def test_there_is_a_chip_for_every_type_the_owner_named(qapp, engine):
    box = MiniSearch(engine)
    labels = [box._scope_buttons[k].text() for k in (None, "files", "mail", "photos", "code")]
    assert labels == ["All", "Files", "Mail", "Photos", "Code"]
    for chip in box._scope_buttons.values():
        assert chip.toolTip() and chip.accessibleName()


def test_a_chip_narrows_the_list_and_its_scope_reaches_the_search_call(
        qapp, mixed_engine, monkeypatch):
    r"""A chip filters the answer in hand at once (§5a: no second search for
    what is already there) - and when that answer was cut off at the engine's
    depth, the chip's own search runs **with the chip's scope**, so the engine
    is asked for mail rather than for a page that happened to hold one email."""
    from app.search import run as run_module
    from app.ui.widgets import mini_search

    seen: list = []
    real = run_module.run_search

    def spy(engine, text, **kwargs):
        seen.append((text, kwargs.get("scope", "all")))
        return real(engine, text, **kwargs)

    monkeypatch.setattr(run_module, "run_search", spy)
    monkeypatch.setattr(mini_search, "FULL_ANSWER", 1)      # every answer counts as cut off
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    assert seen and seen[0] == ("widget", "all")
    seen.clear()

    box._scope_buttons["mail"].click()
    assert [g.kind for g in box._rows] == ["email"]            # at once, from the hand
    _pump(qapp, 3, until=lambda: "mail" in box._deep)
    assert ("widget", "mail") in seen
    assert [g.kind for g in box._rows] == ["email"]

    # A chip already on when somebody types: the search carries it too.
    seen.clear()
    box._select_chip("code")
    _searched(qapp, box, "widget")
    _pump(qapp, 3, until=lambda: ("widget", "code") in seen)
    assert ("widget", "code") in seen, seen
    box.dismiss()


def test_a_chip_on_an_answer_that_was_not_cut_off_asks_nothing_more(
        qapp, mixed_engine, monkeypatch):
    from app.search import run as run_module

    seen: list = []
    real = run_module.run_search
    monkeypatch.setattr(run_module, "run_search",
                        lambda e, t, **k: seen.append(k.get("scope")) or real(e, t, **k))
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    count = len(seen)
    box._select_chip("mail")
    _pump(qapp, 0.3)
    assert len(seen) == count
    box.dismiss()


@pytest.mark.parametrize(("bucket", "typed", "scope", "sent"), [
    (None, "leeds", "all", "leeds"),
    ("mail", "leeds", "mail", "leeds"),
    ("code", "leeds", "code", "leeds"),
    ("files", "leeds", "documents", "leeds"),
    ("photos", "leeds", "documents", "leeds type:image"),
    ("photos", "leeds type:png", "documents", "leeds type:png"),
])
def test_each_chip_is_one_search_scope(bucket, typed, scope, sent):
    from app.ui.presenter.quick_search import scope_request

    assert scope_request(bucket, typed) == (scope, sent)


def test_tab_with_one_kind_in_hand_steps_through_every_chip(qapp, engine):
    box = MiniSearch(engine)
    box.summon()
    _searched(qapp, box, "safety")
    seen = []
    for _ in range(len(CHIP_ORDER) + 1):
        _key(box, Qt.Key.Key_Tab)
        seen.append(box._active_chip)
    assert seen == [*CHIP_ORDER, None]
    _key(box, Qt.Key.Key_Backtab)
    assert box._active_chip == CHIP_ORDER[-1]
    box.dismiss()


def test_a_picture_is_a_photo_and_counts_under_photos():
    assert kind_bucket("jpg") == "photos" and kind_bucket("PNG") == "photos"
    assert chip_label("photos", 1) == "1 photo"
    assert "photos" in CHIP_ORDER


def test_the_slash_menu_is_the_search_tabs_own(qapp, engine):
    """`from:`, `after:`, `has attachment` - the Search tab's `/` menu,
    suggested as you type, on this box."""
    from PySide6.QtTest import QTest

    box = MiniSearch(engine)
    box.summon()
    QTest.keyClicks(box.box, "/fro")
    qapp.processEvents()
    assert box._popup.popup().isVisible()
    shown = [box._popup.completionModel().index(i, 0).data()
             for i in range(box._popup.completionModel().rowCount())]
    assert any("from" in str(text) for text in shown), shown
    box._popup.popup().hide()
    box.dismiss()


# ---------------------------------------------------------------------------
# 3. The preview
# ---------------------------------------------------------------------------

def test_the_preview_opens_beside_the_list_and_follows_the_selection(qapp, mixed_engine):
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    assert box.preview is None or box.preview.isHidden()
    assert _key(box, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
    assert box.preview is not None and not box.preview.isHidden()
    assert box.preview_button.isChecked()
    assert box.preview._row is box._rows[0].best
    box.list.setCurrentRow(2)
    assert box.preview._row is box._rows[2].best
    assert _key(box, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
    assert box.preview.isHidden() and not box.preview_button.isChecked()
    box.dismiss()


def test_the_preview_names_a_message_by_its_subject(qapp, mixed_engine):
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    box.toggle_preview()
    index = next(i for i, g in enumerate(box._rows) if g.kind == "email")
    box.list.setCurrentRow(index)
    assert "pst://" not in box.preview.title.text()
    assert box.preview.pop_button.isHidden(), "pinning needs the main window"
    box.dismiss()


def test_the_preview_stays_open_next_time_if_it_was_left_open(qapp):
    from app.ui.state_writes import pool

    engine, store = _fresh_engine()
    try:
        box = MiniSearch(engine)
        box.summon()
        box.toggle_preview()
        box.dismiss()
        assert pool().waitForDone(5000)
        again = MiniSearch(engine)
        again.summon()
        assert again.preview is not None and not again.preview.isHidden()
        again.dismiss()
    finally:
        engine.close()
        store.close()


# ---------------------------------------------------------------------------
# 4. Keys and recent searches
# ---------------------------------------------------------------------------

def test_each_key_calls_the_right_opener(qapp, mixed_engine, monkeypatch):
    r"""Enter opens (the window's route, through `chosen`), Ctrl+Enter shows
    it in its folder, Ctrl+C copies its path, Ctrl+O opens an email in
    Outlook, Shift+Enter hands everything to the main window - each by the
    one route every page uses (`workers.open_row_async`, `copy_path_async`)."""
    from PySide6.QtTest import QTest

    from app.ui import workers
    from app.ui.presenter.quick_search import OUTLOOK_ONLY

    opened: list = []
    copied: list = []
    monkeypatch.setattr(workers, "open_row_async",
                        lambda store, row, **kw: opened.append((row, kw.get("reveal", False))))
    monkeypatch.setattr(workers, "copy_path_async", lambda row, **kw: copied.append(row))

    def ready():
        box = MiniSearch(mixed_engine)
        box.summon()
        _searched(qapp, box, "widget")
        assert box._rows
        return box

    box = ready()
    first = box._rows[0]
    assert _key(box, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert copied == [first] and box.isVisible(), "Ctrl+C copies and the box stays"
    assert _key(box, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert opened == [(first, True)] and box.isHidden()

    box = ready()
    box.list.setCurrentRow(next(i for i, g in enumerate(box._rows) if g.kind != "email"))
    _key(box, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
    assert len(opened) == 1 and box.isVisible(), "Ctrl+O on a file opens nothing"
    assert box.status.text() == OUTLOOK_ONLY
    box.list.setCurrentRow(next(i for i, g in enumerate(box._rows) if g.kind == "email"))
    _key(box, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
    assert opened[-1][0].kind == "email" and opened[-1][1] is False and box.isHidden()

    box = ready()
    chosen: list = []
    box.chosen.connect(chosen.append)
    expected = box._rows[0]
    QTest.keyClick(box.box, Qt.Key.Key_Return)
    assert chosen == [expected] and box.isHidden(), "Enter opens the highlighted result"

    box = ready()
    expanded: list = []
    box.expanded.connect(expanded.append)
    assert _key(box, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert expanded == ["widget"] and box.isHidden()


def test_ctrl_o_on_an_attachment_opens_the_message_it_came_with(qapp, monkeypatch):
    from types import SimpleNamespace

    from app.core.row_facts import ATTACHMENT_MARKER
    from app.ui import workers

    opened: list = []
    monkeypatch.setattr(workers, "open_row_async",
                        lambda store, row, **kw: opened.append(row))
    box = MiniSearch(None)
    box.summon()
    attachment = SimpleNamespace(kind="pdf", path=f"pst://2024/7{ATTACHMENT_MARKER}plan.pdf",
                                 file_id=9, is_attachment=True, name="plan.pdf", folder="",
                                 when="", best=None)
    box._rows = [attachment]
    box.list.addItem("plan.pdf")
    box.list.setCurrentRow(0)
    box._outlook_current()
    assert opened and getattr(opened[0], "path", "") == "pst://2024/7"


def test_ctrl_c_with_words_selected_in_the_box_still_copies_the_words(
        qapp, mixed_engine, monkeypatch):
    from app.ui import workers

    copied: list = []
    monkeypatch.setattr(workers, "copy_path_async", lambda row, **kw: copied.append(row))
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    box.box.selectAll()
    assert not _key(box, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert copied == []
    box.dismiss()


def test_up_and_down_move_the_highlight(qapp, mixed_engine):
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    assert box.list.currentRow() == 0
    _key(box, Qt.Key.Key_Down)
    _key(box, Qt.Key.Key_Down)
    assert box.list.currentRow() == 2
    _key(box, Qt.Key.Key_Up)
    assert box.list.currentRow() == 1
    box.dismiss()


def test_the_first_escape_empties_the_box_and_the_second_closes_it(qapp, mixed_engine):
    """The owner, 2026-10-08: the first Esc clears the text, the second closes."""
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    assert _key(box, Qt.Key.Key_Escape)
    assert box.box.text() == "" and box.list.count() == 0
    assert box.isVisible(), "the first Escape closed the box"
    assert _key(box, Qt.Key.Key_Escape)
    assert box.isHidden()


def test_an_empty_box_lists_recent_searches_and_enter_runs_one(qapp):
    engine, store = _fresh_engine()
    try:
        store.log_search("leeds site survey", hits=1)
        time.sleep(1.1)                          # searched_at is in whole seconds
        store.log_search("safety record", hits=1)
        box = MiniSearch(engine)
        box.summon()
        _pump(qapp, 3, until=lambda: box.recent.count() >= 2)
        shown = [box.recent.item(i).text() for i in range(box.recent.count())]
        assert shown[:2] == ["safety record", "leeds site survey"]
        assert box.stack.currentWidget() is box.recent_page
        box._take()                              # Enter, with the box empty
        assert box.box.text() == "safety record" and box.isVisible()
        _pump(qapp, 3, until=lambda: box.list.count() > 0)
        assert box.list.count() > 0
        box.dismiss()
    finally:
        engine.close()
        store.close()


def test_recent_searches_are_not_offered_when_settings_says_not_to(qapp):
    from types import SimpleNamespace

    from app.ui.presenter.quick_search import recent_wanted

    engine, store = _fresh_engine()
    try:
        store.log_search("leeds site survey", hits=1)
        box = MiniSearch(engine, offer_recent=lambda: False)
        box.summon()
        _pump(qapp, 0.5)
        assert box.recent.count() == 0
        box.dismiss()
    finally:
        engine.close()
        store.close()
    assert recent_wanted(SimpleNamespace(search_offer_recent=False)) is False
    assert recent_wanted(None, {"search_offer_recent": "false"}) is False
    assert recent_wanted(SimpleNamespace(search_offer_recent=False),
                         {"search_offer_recent": "true"}) is True


def test_the_recent_list_is_read_on_a_worker_never_here(qapp):
    """Non-negotiable #5: a store read on the interface thread is a freeze
    waiting for a busy index."""
    import threading

    engine, store = _fresh_engine()
    try:
        threads: list = []
        real = store.recent_searches

        def counted(*args, **kwargs):
            threads.append(threading.current_thread())
            return real(*args, **kwargs)

        store.recent_searches = counted
        box = MiniSearch(engine)
        box.summon()
        _pump(qapp, 3, until=lambda: bool(threads))
        assert threads and threading.main_thread() not in threads
        box.dismiss()
    finally:
        engine.close()
        store.close()


def test_the_footer_says_the_keys_and_ctrl_o_only_for_an_email(qapp, mixed_engine):
    from PySide6.QtWidgets import QLabel

    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")

    def keys():
        return [w.text() for w in box.hints.findChildren(QLabel)
                if w.objectName() == "miniKey" and not w.isHidden()]

    box.list.setCurrentRow(next(i for i, g in enumerate(box._rows) if g.kind != "email"))
    assert "Enter" in keys() and "Esc" in keys() and "Ctrl+O" not in keys()
    box.list.setCurrentRow(next(i for i, g in enumerate(box._rows) if g.kind == "email"))
    assert "Ctrl+O" in keys()
    box.dismiss()


def test_the_hints_that_do_not_fit_are_dropped_least_needed_first():
    from app.ui.presenter.quick_search import fit_hints, hints

    found = hints(mode="results", mail=True)
    everything = fit_hints(found, [100] * len(found), 10_000)
    assert [h.keys for h in everything] == [h.keys for h in found]
    assert [h.keys for h in fit_hints(found, [100] * len(found), 300)] == [
        "Enter", "Ctrl+O", "Esc"]


# ---------------------------------------------------------------------------
# Mail is called by its subject
# ---------------------------------------------------------------------------

def test_a_message_row_shows_its_subject_and_sender_never_its_key(qapp, mixed_engine):
    r"""Rows from an Outlook archive have paths like `pst://2024/2109476`; the
    number is an entry id nobody has seen. The row says the subject, and who
    sent it on the line under it."""
    from app.ui.widgets.mini_search_rows import ROLE_LINES

    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    index = next(i for i, g in enumerate(box._rows) if g.kind == "email")
    lines = box.list.item(index).data(ROLE_LINES)
    assert lines.title == "Widget delivery"
    assert lines.subtitle.startswith("From chris@example.com")
    assert "pst://" not in lines.title + lines.subtitle + lines.tip
    box.dismiss()


def test_a_message_with_no_details_is_called_message_not_a_number():
    from types import SimpleNamespace

    from app.ui.presenter.quick_search import row_lines

    group = SimpleNamespace(kind="email", path="pst://2024/2109476", name="2109476",
                            folder="", when="", file_id=7)
    assert row_lines(group, None).title == "Message"
    assert row_lines(group, {"subject": "", "sender": "a@b.c"}).title == "(no subject)"
    assert row_lines(group, {"subject": "Trip", "sender": "Mum <m@x.org>"}).subtitle == "From Mum"


def test_a_file_row_says_its_type_and_folder():
    from types import SimpleNamespace

    from app.ui.presenter.quick_search import row_lines

    group = SimpleNamespace(kind="pdf", path="C:/work/2024/report.pdf", name="report.pdf",
                            folder="work > 2024", when="3 days ago", file_id=1)
    lines = row_lines(group, None)
    assert lines.title == "report.pdf" and lines.when == "3 days ago"
    assert lines.subtitle == "PDF · work > 2024" and lines.family == "doc"


# ---------------------------------------------------------------------------
# The look
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_it_takes_the_theme_the_window_is_using(qapp, engine, scheme):
    r"""It is a window of its own, so the main window's sheet never reached it:
    it was drawn in the operating system's look - light in the dark theme."""
    from app.ui import theme

    theme.stylesheet(scheme)
    box = MiniSearch(engine)
    box.summon()
    sheet = box.styleSheet()
    assert theme.PALETTES[scheme]["surface"] in sheet and "#miniCard" in sheet
    assert box.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    box.dismiss()


def test_every_header_button_has_an_icon_and_says_what_it_does(qapp, engine):
    box = MiniSearch(engine)
    box.summon()
    for button in (box.preview_button, box.expand_button, box.close_button):
        assert not button.icon().isNull()
        assert button.toolTip() and button.accessibleName()
    box.dismiss()


def test_a_narrow_box_keeps_its_type_chips_whole(qapp, mixed_engine):
    r"""Made as narrow as it goes, the counts beside the chips step aside
    rather than squeezing "Photos" into "Pho"."""
    from app.ui.presenter.quick_search import MIN_SIZE

    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    assert not box.chips.isHidden()
    box.resize(*MIN_SIZE)
    _pump(qapp, 0.1)
    for chip in box._scope_buttons.values():
        assert chip.width() >= chip.sizeHint().width(), chip.text()
    box.resize(900, 600)
    _pump(qapp, 0.1)
    assert not box.chips.isHidden()
    box.dismiss()


def test_the_box_is_never_narrower_than_its_chips_need(qapp, mixed_engine, monkeypatch):
    """GitHub's Windows runner draws wider text than the laptop, and at the
    560px floor "All" was squeezed below its own label (2026-10-08). The floor
    is shrunk here so the rule shows on any machine's fonts: the box's minimum
    width is what its row of chips needs, never only the floor."""
    from app.ui.presenter import quick_search

    monkeypatch.setattr(quick_search, "MIN_SIZE", (100, 100))
    box = MiniSearch(mixed_engine)
    box.summon()
    _pump(qapp, 0.1)
    need = box.scopes.sizeHint().width()
    assert need > 100
    assert box.minimumWidth() >= need
    box.resize(100, 380)
    _pump(qapp, 0.1)
    for chip in box._scope_buttons.values():
        assert chip.width() >= chip.sizeHint().width(), chip.text()
    box.dismiss()
