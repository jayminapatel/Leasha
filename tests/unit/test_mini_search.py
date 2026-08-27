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

import os
import pathlib
import tempfile
import time

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt                                     # noqa: E402
from PyQt6.QtGui import QKeyEvent                               # noqa: E402
from PyQt6.QtWidgets import QApplication                        # noqa: E402

from app.ui.hotkey import (                                     # noqa: E402
    DEFAULT_HOTKEY, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, HotkeyListener,
    available, describe, parse, spell,
)
from app.ui.widgets.mini_search import (                        # noqa: E402
    ROWS, MiniSearch, row_label,
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
    `Ctrl+Shift+F` is find-in-files in every editor a developer has open."""
    found = parse(DEFAULT_HOTKEY)
    assert found is not None and found.usable
    assert found.modifiers == MOD_CONTROL | MOD_ALT


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
    assert "for_surface(SEARCH)" in source


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
