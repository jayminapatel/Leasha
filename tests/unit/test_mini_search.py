r"""Search from anywhere. Workspace §3a.

Layer: L5.

**This is the feature the product is demonstrated with**, and the order says
to treat its polish accordingly: press a key in any application, type, press
Enter, the document opens and the box is gone.

**A conflict is reported, never swallowed.** `RegisterHotKey` fails when
something else already owns the combination, and that is the single most
likely thing to happen on a real machine — half the world has `Ctrl+Alt+Space`
bound to something. A shortcut that silently does not work is
indistinguishable from a broken application.

**It runs the Search tab's policy, not one of its own.** Somebody who summoned
a box from inside Excel is the least likely person to be in the mood to debug
a query, and a second policy would be a second set of answers to one question.

The registration itself is Windows-only; everything here that can be checked
without Windows is, and the part that cannot says so rather than pretending.
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

from app.ui.hotkey import (                                     # noqa: E402
    DEFAULT_HOTKEY, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, HotkeyListener,
    available, describe, parse, spell,
)
from app.ui.widgets.mini_search import (                        # noqa: E402
    CHIP_ORDER, ROWS, MiniSearch, chip_label, kind_bucket, row_label,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def engine():
    """A real engine over three real documents, keyword-only."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "mini.db").connect()
    for name, text in (
        ("safety.txt", "the safety induction training record"),
        ("pumps.txt", "pump station commissioning notes"),
        ("survey.txt", "the leeds site survey, safety section"),
    ):
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

    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()


@pytest.fixture(scope="module")
def mixed_engine():
    r"""Files, code and mail, all matching one word - §5a needs more than one
    kind in the result set to prove the chips count each of them correctly."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "mixed.db").connect()
    for name, ext, text in (
        ("alpha.txt", "txt", "widget assembly notes"),
        ("beta.txt", "txt", "widget shipping schedule"),
        ("gamma.py", "py", "def widget(): pass"),
    ):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext=ext, size_bytes=1,
            mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    message_id = store.upsert_file(
        "pst://msg/1", parent_dir="pst://msg", size_bytes=1, mtime_ns=1,
        status="INDEXED", source_kind="pst_message")
    with store.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(file_id, subject, sender, recipients, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, "Widget delivery", "chris@example.com",
             json.dumps([]), 0, 0),
        )
    store.replace_chunks(
        message_id, [{"ordinal": 0, "text": "widget delivery confirmation"}])

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

        def embed_all(self, _t):
            raise RuntimeError("no model")

    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()


def _searched(qapp, box, query):
    box.box.setText(query)
    box._search()
    for _ in range(60):
        qapp.processEvents()
        if box.list.count():
            return
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# The shortcut, parsed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("typed", "canonical"), [
    ("Ctrl+Alt+L", "Ctrl+Alt+L"),
    ("ctrl+alt+l", "Ctrl+Alt+L"),
    ("Alt+Ctrl+L", "Ctrl+Alt+L"),
    ("ctrl-alt-l", "Ctrl+Alt+L"),
    ("Ctrl+Shift+F3", "Ctrl+Shift+F3"),
    ("Win+Space", "Win+Space"),
])
def test_a_shortcut_is_read_and_spelled_one_way(typed, canonical):
    r"""**One spelling, whatever was typed.** `Alt+Ctrl+L` and `Ctrl+Alt+L`
    are the same shortcut, and two spellings in Settings would be two rows
    nobody can tell apart - the same reasoning saved searches needed."""
    assert parse(typed).text == canonical


@pytest.mark.parametrize("typed", [
    "L", "Ctrl", "", None, "Ctrl+A+B", "Ctrl+nonsense", "   ", "+++",
])
def test_anything_that_is_not_a_shortcut_is_refused(typed):
    assert parse(typed) is None


def test_a_bare_letter_is_never_a_global_shortcut():
    r"""**At least one modifier, always.** Registering `L` alone would take
    that key away from every application on the machine, which is not a thing
    any application may do."""
    assert parse("L") is None
    assert parse("Ctrl+L") is not None


def test_the_default_is_usable_and_deliberate():
    r"""The obvious candidates are all taken: `Ctrl+Space` is the IME switch
    on any machine with a second layout, `Win+S` is Windows' own search,
    `Ctrl+Shift+F` is find-in-files in every editor a developer has open.

    *2026-10-08, the owner: Ctrl+Shift+Space.* Another program on his laptop
    holds Ctrl+Alt+L, so the box never opened there."""
    from app.ui.hotkey import MOD_SHIFT

    found = parse(DEFAULT_HOTKEY)
    assert found is not None and found.usable
    assert found.modifiers == MOD_CONTROL | MOD_SHIFT
    assert found.text == "Ctrl+Shift+Space"


def test_the_repeat_bit_is_set():
    r"""**`MOD_NOREPEAT`, and it matters.** Without it, holding the
    combination fires dozens of times a second and each one opens the box and
    takes focus - a keyboard held a moment too long becomes a machine that
    cannot be typed on."""
    source = (pathlib.Path(__file__).resolve().parents[2]
              / "app" / "ui" / "hotkey.py").read_text(encoding="utf-8")
    assert "MOD_NOREPEAT" in source
    assert "| MOD_NOREPEAT" in source
    assert MOD_NOREPEAT == 0x4000


def test_spelling_puts_the_modifiers_in_the_order_people_write_them():
    assert spell(MOD_CONTROL | MOD_ALT, ord("L")) == "Ctrl+Alt+L"


# ---------------------------------------------------------------------------
# What Settings is told
# ---------------------------------------------------------------------------

def test_a_shortcut_that_is_not_one_says_what_is_missing():
    said = describe("L")
    assert "Ctrl" in said and "Alt" in said
    assert "_" not in said and "()" not in said


def test_a_shortcut_something_else_owns_says_so_in_words():
    r"""**The most likely thing to happen on a real machine.** A control that
    appears to be set beside a keystroke that does nothing is the failure this
    sentence exists to prevent."""
    said = describe("Ctrl+Alt+L", registered=False)
    if available():                              # pragma: no cover - owner's box
        assert "already using" in said
        assert "different" in said
    else:
        assert "Windows" in said, "off Windows it should say why"


def test_off_windows_it_says_nothing_is_listening_rather_than_lying():
    if available():                              # pragma: no cover
        pytest.skip("this asserts the non-Windows answer")
    said = describe("Ctrl+Alt+L")
    assert "Windows" in said and "listening" in said


def test_taking_a_shortcut_off_windows_returns_false_and_never_raises():
    # 2026-09-20: the same guard its sibling above already had. Without it this
    # asserts the non-Windows answer *on Windows*, where the hotkey really does
    # register - so it failed on the owner's own machine, in every suite run, for
    # the one platform the application ships on.
    if available():                              # pragma: no cover - owner's box
        pytest.skip("this asserts the non-Windows answer")
    listener = HotkeyListener()
    assert listener.start("Ctrl+Alt+L", lambda: None) is False
    assert listener.registered is False
    listener.stop()
    listener.stop()                              # twice is safe


def test_a_shortcut_that_cannot_be_parsed_is_not_even_attempted():
    listener = HotkeyListener()
    assert listener.start("nonsense", lambda: None) is False
    assert listener.hotkey is None


def test_the_registration_uses_ctypes_and_not_an_optional_package():
    r"""The reasoning `single_instance.py` records: pywin32 is optional -
    needed only for PST ingestion - and the feature the whole product is
    demonstrated with must not depend on an optional package."""
    source = (pathlib.Path(__file__).resolve().parents[2]
              / "app" / "ui" / "hotkey.py").read_text(encoding="utf-8")
    assert "ctypes.WinDLL" in source

    # **The import, not the word.** The module argues in prose for *not*
    # using pywin32, so a bare `"win32" not in source` matches its own
    # explanation - the third time in this project that a guard has been
    # written against a name its own justification contains. Grep for what
    # the code would actually do.
    for importing in ("import win32", "from win32", "import pywin32"):
        assert importing not in source, f"hotkey.py has `{importing}`"


# ---------------------------------------------------------------------------
# The box
# ---------------------------------------------------------------------------

def test_it_opens_empty_and_ready_to_type(qapp, engine):
    box = MiniSearch(engine)
    box.box.setText("left over")
    box.summon()
    assert not box.isHidden()
    assert box.box.text() == ""
    assert box.list.count() == 0
    box.dismiss()


def test_typing_finds_documents_and_shows_what_they_are(qapp, engine):
    box = MiniSearch(engine)
    box.summon()
    _searched(qapp, box, "safety")
    rows = [box.list.item(i).text() for i in range(box.list.count())]
    assert any("safety.txt" in row for row in rows)
    assert any("survey.txt" in row for row in rows)
    box.dismiss()


def test_the_first_result_is_already_chosen(qapp, engine):
    r"""**Enter has to work without arrowing.** The promise is a keystroke,
    a word and Enter; a list that needs a down-arrow first is three actions
    where the demonstration says two."""
    box = MiniSearch(engine)
    box.summon()
    _searched(qapp, box, "safety")
    assert box.list.currentRow() == 0
    box.dismiss()


def test_enter_opens_the_document_and_the_box_vanishes(qapp, engine):
    box = MiniSearch(engine)
    chosen: list = []
    box.chosen.connect(chosen.append)
    box.summon()
    _searched(qapp, box, "safety")
    box._take()
    assert chosen and getattr(chosen[0], "name", "") == "safety.txt"
    assert box.isHidden(), "it stayed on screen after opening something"


def test_enter_with_nothing_found_hands_the_query_to_the_window(qapp, engine):
    r"""**Better than doing nothing**: somebody who pressed Enter meant
    something to happen, and this is the way out of a box too small for the
    question being asked."""
    box = MiniSearch(engine)
    expanded: list = []
    box.expanded.connect(expanded.append)
    box.summon()
    box.box.setText("nothing at all matches this")
    box._search()
    for _ in range(30):
        qapp.processEvents()
        time.sleep(0.01)
    box._take()
    assert expanded == ["nothing at all matches this"]
    assert box.isHidden()


def test_escape_closes_it(qapp, engine):
    r"""**The one thing nobody will forgive is a window they cannot get rid
    of** - and this one is stay-on-top, over their work."""
    box = MiniSearch(engine)
    box.summon()
    box.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                Qt.KeyboardModifier.NoModifier))
    assert box.isHidden()


def test_dismissing_leaves_nothing_behind(qapp, engine):
    """A box that reopens holding the last search is a box showing somebody
    else's question — on a machine anyone might walk past."""
    box = MiniSearch(engine)
    box.summon()
    _searched(qapp, box, "safety")
    box.dismiss()
    assert box.box.text() == "" and box.list.count() == 0


def test_it_stays_on_top_and_out_of_the_taskbar(qapp, engine):
    r"""A Tool window has no taskbar entry, which is what makes this feel
    like a summoned thing rather than a second application to close."""
    box = MiniSearch(engine)
    flags = box.windowFlags()
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.Tool


def test_the_list_is_short_on_purpose(qapp, engine):
    r"""**Seven is a ceiling, not a screenful.** This is not a results page -
    it is "the thing I was thinking of, now". A list long enough to scroll is
    one somebody reads instead of recognises."""
    assert ROWS <= 10
    box = MiniSearch(engine)
    box.summon()
    _searched(qapp, box, "the")
    assert box.list.count() <= ROWS


def test_a_row_says_what_it_is_and_where_it_lives_and_no_more(qapp):
    r"""Two facts. A snippet would make each row three lines tall and turn a
    recogniser into a reader - and the person already knows what they are
    looking for, or they would have opened the window."""
    class _Group:
        name = "safety.txt"
        folder = "work/2024"
        path = "C:/work/2024/safety.txt"

    label = row_label(_Group())
    assert "safety.txt" in label and "work/2024" in label
    assert label.count("\n") == 0


def test_a_row_with_no_name_falls_back_to_the_filename(qapp):
    class _Group:
        name = ""
        folder = ""
        path = "C:/work/2024/report.pdf"

    assert row_label(_Group()) == "report.pdf"


def test_it_runs_the_search_tabs_policy(qapp, engine):
    r"""§3a is explicit: the kid-safe surface from the search-experience
    order. Somebody who summoned this from inside Excel is the least likely
    person to want to debug a query."""
    source = (pathlib.Path(__file__).resolve().parents[2] / "app" / "ui"
              / "widgets" / "mini_search.py").read_text(encoding="utf-8")
    # 2026-10-04: the box searches through `run_search`, the Search tab's own
    # steps, for surface SEARCH and with the Settings switches - it used to
    # take the surface's defaults (`for_surface(SEARCH)`) and ignore them.
    assert "run_search(" in source and "surface=SEARCH" in source


def test_a_late_answer_never_overwrites_a_later_keystroke(qapp, engine):
    """The same generation rule the panes follow, on a box where somebody is
    typing fast by definition."""
    box = MiniSearch(engine)
    box.summon()
    _searched(qapp, box, "safety")
    box._generation += 3
    box._show(object(), generation=1)
    assert box.list.count() > 0, "a stale reply cleared the list"
    box.dismiss()


def test_typing_pauses_neither_read_nor_count_saved_searches(qapp, monkeypatch):
    r"""2026-10-04, code review: every 180 ms pause read the saved searches from
    the store and wrote a run of each one used. Now they are read once per
    summon, and a run is counted once - when Enter opens a result."""
    from app.search import run as run_module
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "saved.db").connect()
    file_id = store.upsert_file("C:/work/safety.txt", parent_dir="C:/work", ext="txt",
                                size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file")
    store.replace_chunks(file_id, [{"ordinal": 0, "text": "the safety induction record"}])
    store.save_search("induct", "safety induction", "all")

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

    engine = SearchEngine(store, _NoVectors(), _NoModel())
    reads: list = []
    real_load = run_module.load_saved
    monkeypatch.setattr(run_module, "load_saved", lambda s: reads.append(1) or real_load(s))
    try:
        box = MiniSearch(engine)
        box.summon()
        for _ in range(50):
            qapp.processEvents()
            if box._saved is not None:
                break
            time.sleep(0.01)
        assert box._saved and len(reads) == 1
        for typed in ("saved:induct", "saved:induct ", "saved:induct"):
            _searched(qapp, box, typed)
        assert len(reads) == 1, "a typing pause read the saved searches again"
        assert store.saved_searches()[0]["run_count"] == 0, "a typing pause counted a run"
        box._take()
        for _ in range(50):
            qapp.processEvents()
            if store.saved_searches()[0]["run_count"]:
                break
            time.sleep(0.01)
        assert store.saved_searches()[0]["run_count"] == 1
    finally:
        engine.close()
        store.close()


def test_a_box_with_no_engine_is_not_an_error(qapp):
    box = MiniSearch(None)
    box.summon()
    box.box.setText("anything")
    box._search()
    box._take()
    assert box.isHidden()


def test_the_setting_is_declared_shown_and_read():
    from app.core.config import SETTING_KEYS, Settings
    from app.core.settings_registry import SETTINGS

    for key in ("MINI_SEARCH_ENABLED", "MINI_SEARCH_HOTKEY"):
        assert key in SETTING_KEYS
        assert any(setting.key == key for setting in SETTINGS)
    assert Settings.model_fields["mini_search_enabled"].default is True
    assert Settings.model_fields["mini_search_hotkey"].default == DEFAULT_HOTKEY


def test_the_controls_exist_and_are_named_for_the_registry(qapp):
    from app.ui.widgets.search_behaviour_box import SearchBehaviourBox

    built = SearchBehaviourBox()
    assert built.mini_search.objectName() == "MINI_SEARCH_ENABLED"
    assert built.mini_hotkey.objectName() == "MINI_SEARCH_HOTKEY"
    built.mini_hotkey.setText("Ctrl+Alt+K")
    assert built.values()["MINI_SEARCH_HOTKEY"] == "Ctrl+Alt+K"


def test_settings_says_out_loud_whether_the_shortcut_was_taken(qapp):
    from app.ui.widgets.search_behaviour_box import SearchBehaviourBox

    built = SearchBehaviourBox()
    built.say_hotkey("Ctrl+Alt+L", registered=False)
    assert built.mini_status.text().strip()
    built.say_hotkey("L")
    assert "Ctrl" in built.mini_status.text()


# ---------------------------------------------------------------------------
# §4a: selection-to-search
# ---------------------------------------------------------------------------

def test_the_prefill_setting_is_declared_shown_and_read(qapp):
    r"""**Its own switch, separate from `MINI_SEARCH_ENABLED`.** Reading a
    selection out of whatever application somebody was looking at is the more
    intrusive half of the feature, and "off" for one must not mean "off" for
    both."""
    from app.core.config import SETTING_KEYS, Settings
    from app.core.settings_registry import SETTINGS
    from app.ui.widgets.search_behaviour_box import SearchBehaviourBox

    assert "MINI_SEARCH_PREFILL_SELECTION" in SETTING_KEYS
    assert any(setting.key == "MINI_SEARCH_PREFILL_SELECTION"
              for setting in SETTINGS)
    assert Settings.model_fields["mini_search_prefill_selection"].default is True

    built = SearchBehaviourBox()
    assert built.mini_prefill.objectName() == "MINI_SEARCH_PREFILL_SELECTION"
    built.mini_prefill.setChecked(False)
    assert built.values()["MINI_SEARCH_PREFILL_SELECTION"] is False


def test_a_selection_pre_fills_the_box_selected(qapp, engine):
    r"""**One keystroke replaces it.** The box opens with the text already
    there *and* highlighted, so typing over it needs no Select All, no
    Backspace, nothing but what somebody would already be doing."""
    box = MiniSearch(engine)
    box.summon("the leeds site survey")
    assert box.box.text() == "the leeds site survey"
    assert box.box.selectedText() == "the leeds site survey"
    box.dismiss()


def test_summoning_with_no_selection_opens_plain(qapp, engine):
    """No selection is the common case, and it must look exactly like the box
    always looked - `summon()` with nothing given, unaffected."""
    box = MiniSearch(engine)
    box.box.setText("left over")
    box.summon("")
    assert box.box.text() == ""
    box.dismiss()


def test_offer_prefill_fills_the_box_when_nothing_has_happened_yet(qapp, engine):
    r"""**The async-arrival path.** Reading the selection happens on a worker
    after the box is already open, so `offer_prefill` is where the answer
    lands - and the ordinary case is that nothing has happened in between."""
    box = MiniSearch(engine)
    box.summon()
    box.offer_prefill("the leeds site survey")
    assert box.box.text() == "the leeds site survey"
    assert box.box.selectedText() == "the leeds site survey"
    box.dismiss()


def test_offer_prefill_is_ignored_once_the_box_is_dismissed(qapp, engine):
    """The person closed the box before the worker answered - the box being
    hidden must not be reopened or refilled by a late arrival."""
    box = MiniSearch(engine)
    box.summon()
    box.dismiss()
    box.offer_prefill("arrived too late")
    assert box.box.text() == ""
    assert box.isHidden()


def test_offer_prefill_never_overwrites_a_real_keystroke(qapp, engine):
    """Somebody typed in the gap between the box opening and the selection
    arriving - what they typed is real and the arriving text is not."""
    box = MiniSearch(engine)
    box.summon()
    box.box.setText("already typing")
    box.offer_prefill("the selection that arrived late")
    assert box.box.text() == "already typing"
    box.dismiss()


def test_offer_prefill_does_nothing_for_an_empty_answer(qapp, engine):
    """No selection: the worker answers with "", and the box stays exactly
    as it opened."""
    box = MiniSearch(engine)
    box.summon()
    box.offer_prefill("")
    assert box.box.text() == ""
    box.dismiss()


def test_a_pre_fill_is_never_searched_by_itself(qapp, engine):
    """Pre-fill only. Nobody typed Enter, so nothing may have been sent to the
    engine yet - the list stays empty until they do something."""
    box = MiniSearch(engine)
    box.summon("safety")
    assert box.list.count() == 0
    box.dismiss()


def test_clipboard_snapshot_restores_byte_perfect(qapp):
    r"""**The load-bearing test.** Whatever was on the clipboard before this
    feature ran must be there afterwards, format for format and byte for
    byte - a search box that leaves somebody's clipboard full of the wrong
    thing is a much worse bug than one that fails to pre-fill."""
    from PySide6.QtCore import QByteArray, QMimeData
    from PySide6.QtGui import QGuiApplication

    from app.ui.selection import restore_clipboard, snapshot_clipboard

    clipboard = QGuiApplication.clipboard()
    original_bytes = bytes(range(256))

    data = QMimeData()
    data.setText("what was really on the clipboard")
    data.setData("application/x-leasha-test", QByteArray(original_bytes))
    clipboard.setMimeData(data)

    snapshot = snapshot_clipboard()
    # Something else lands on the clipboard in between - the whole reason a
    # snapshot has to be an independent copy rather than a live reference.
    clipboard.setText("something else was copied in the meantime")

    restore_clipboard(snapshot)

    restored = clipboard.mimeData()
    assert restored.text() == "what was really on the clipboard"
    assert bytes(restored.data("application/x-leasha-test").data()) == (
        original_bytes)


def test_no_selection_leaves_the_clipboard_untouched(qapp, monkeypatch):
    r"""**No change within the timeout means no selection.** A synthetic
    Ctrl+C into a window with nothing highlighted must not be mistaken for an
    answer - the clipboard's text staying exactly as it was is the signal
    this reads, and it must give back `None` rather than the stale text."""
    import app.ui.selection as selection

    from PySide6.QtGui import QGuiApplication

    monkeypatch.setattr(selection, "available", lambda: True)
    monkeypatch.setattr(selection, "_send_copy", lambda: True)
    monkeypatch.setattr(selection, "COPY_TIMEOUT_S", 0.05)

    clipboard = QGuiApplication.clipboard()
    clipboard.setText("unchanged throughout")

    assert selection.read_foreground_selection() is None
    assert clipboard.text() == "unchanged throughout"


def test_a_selection_is_read_and_the_clipboard_is_restored(qapp, monkeypatch):
    r"""The other half: a copy that *does* change the clipboard is read back
    and the clipboard is put back exactly as it was found."""
    import app.ui.selection as selection

    from PySide6.QtGui import QGuiApplication

    clipboard = QGuiApplication.clipboard()
    clipboard.setText("what was on the clipboard before")

    def _fake_copy() -> bool:
        clipboard.setText("the words that were highlighted")
        return True

    monkeypatch.setattr(selection, "available", lambda: True)
    monkeypatch.setattr(selection, "_send_copy", _fake_copy)

    found = selection.read_foreground_selection()

    assert found == "the words that were highlighted"
    assert clipboard.text() == "what was on the clipboard before"


def test_off_windows_nothing_is_read_and_nothing_is_touched(qapp, monkeypatch):
    import app.ui.selection as selection

    from PySide6.QtGui import QGuiApplication

    monkeypatch.setattr(selection, "available", lambda: False)
    clipboard = QGuiApplication.clipboard()
    clipboard.setText("must not move")

    assert selection.read_foreground_selection() is None
    assert clipboard.text() == "must not move"


def test_a_failed_copy_returns_none_and_never_raises(qapp, monkeypatch):
    import app.ui.selection as selection

    monkeypatch.setattr(selection, "available", lambda: True)
    monkeypatch.setattr(selection, "_send_copy", lambda: False)

    assert selection.read_foreground_selection() is None


# ---------------------------------------------------------------------------
# §5a: live category-count chips
# ---------------------------------------------------------------------------

def test_kind_bucket_sorts_email_code_and_everything_else():
    assert kind_bucket("email") == "mail"
    assert kind_bucket("py") == "code"
    assert kind_bucket("pdf") == "files"
    assert kind_bucket("") == "files"


@pytest.mark.parametrize(("bucket", "count", "said"), [
    ("files", 1, "1 file"), ("files", 7, "7 files"),
    ("mail", 1, "1 email"), ("mail", 5, "5 emails"),
    ("code", 1, "1 code result"), ("code", 3, "3 code results"),
])
def test_chip_label_is_plain_words_and_singular_where_it_matters(
    bucket, count, said
):
    assert chip_label(bucket, count) == said


def test_chips_count_the_result_set_already_in_hand(qapp, mixed_engine):
    r"""**Counts equal the result set's.** Three kinds went in - two files, one
    code, one mail - and the chips must add up to exactly what was fetched,
    not to some other number."""
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")

    assert box._chip_counts == {"files": 2, "code": 1, "mail": 1}
    assert sum(box._chip_counts.values()) == len(box._all_groups)
    assert not box.chips.isHidden()
    assert [button.text() for button in box._chip_buttons] == [
        "2 files", "1 email", "1 code result"]
    box.dismiss()


def test_no_chips_when_there_is_nothing_to_count(qapp, engine):
    """A single-kind result set is not worth a chip row - three buttons all
    saying the same number as the list above them is noise, not an answer."""
    box = MiniSearch(engine)
    box.summon()
    _searched(qapp, box, "safety")

    assert set(box._chip_counts) <= {"files"}
    assert box.chips.isHidden()
    box.dismiss()


def test_tab_cycles_the_chips_and_re_filters_instantly(qapp, mixed_engine):
    r"""Tab steps through the kinds present and back to "all", and each step
    is a filter over rows already fetched - never a new call to the engine."""
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")

    calls = []
    real_search = mixed_engine.search
    mixed_engine.search = lambda *a, **k: (calls.append(1) or real_search(*a, **k))
    try:
        assert box._active_chip is None
        assert box.list.count() == 4

        box._cycle_chip()                        # -> "files" (first in CHIP_ORDER)
        assert box._active_chip == "files"
        assert [g.kind for g in box._rows] == ["txt", "txt"]

        box._cycle_chip()                        # -> "mail"
        assert box._active_chip == "mail"
        assert [g.kind for g in box._rows] == ["email"]

        box._cycle_chip()                        # -> "code"
        assert box._active_chip == "code"
        assert [g.kind for g in box._rows] == ["py"]

        box._cycle_chip()                        # -> back to "all"
        assert box._active_chip is None
        assert box.list.count() == 4
    finally:
        mixed_engine.search = real_search

    assert calls == [], "cycling the chips must never call the engine again"
    box.dismiss()


def test_tab_in_the_box_is_caught_by_the_event_filter_not_focus_change(
    qapp, mixed_engine
):
    r"""The load-bearing mechanic: Tab pressed while the box has focus must
    reach the chip cycle, not Qt's own focus-next-widget handling."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")

    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab,
                      Qt.KeyboardModifier.NoModifier)
    handled = box.eventFilter(box.box, event)

    assert handled is True
    assert box._active_chip == "files"
    box.dismiss()


def test_clicking_a_chip_selects_it_and_clicking_cycling_stay_in_sync(
    qapp, mixed_engine
):
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")

    mail_button = next(b for b in box._chip_buttons if "mail" in b.objectName())
    mail_button.click()

    assert box._active_chip == "mail"
    assert mail_button.isChecked()
    assert all(not b.isChecked() for b in box._chip_buttons if b is not mail_button)
    assert [g.kind for g in box._rows] == ["email"]
    box.dismiss()


def test_dismissing_or_a_fresh_summon_clears_the_chips(qapp, mixed_engine):
    box = MiniSearch(mixed_engine)
    box.summon()
    _searched(qapp, box, "widget")
    assert box._chip_counts

    box.dismiss()
    assert box._chip_counts == {}
    assert box._all_groups == []
    assert box.chips.isHidden()

    box.summon()
    assert box._chip_counts == {}
    assert box.chips.isHidden()
    box.dismiss()
