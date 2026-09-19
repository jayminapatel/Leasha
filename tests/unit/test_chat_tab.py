"""The Chat tab's decisions and its rules, checked without a display.

Layer: L5 (no Qt - the scenarios that need a window are in test_chat_tab_qt.py)

Work order 202626270611 section 3 and 5. What is pinned here:

* numbering is first-mention and never changes (4e-2);
* the shelf is one list with pin / remove / re-add rules (3c);
* sessions round-trip through the adapter, on the store's own methods when it
  has them and on the keyed-state fallback until it does (3d);
* **the no-badge rule** (3a, 3e): no fixed string the tab can show matches the
  warning-banner deny-list, and the deny-list is shown able to catch one.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from app.chat.types import ChatTurn, Receipt
from app.ui.chat_sessions import (
    ChatSession, ChatSessions, new_session, session_from_dict, session_to_dict,
)
from app.ui.presenter import chat as presenter
from app.ui.presenter.chat import (
    MAX_UNPINNED, Numbering, Shelf, closing_line, receipt_to_result,
    render_answer_html, title_from_question, unavailable_text,
)
from tests.unit.chat_fakes import (
    AGREEMENT, DEPOSIT_TEXT, LETTER, FakeSessionBackend, deposit_turn,
    search_results,
)
from tests.unit.test_search_policy import SURFACE_JARGON

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "app" / "ui"

#: Every module whose fixed strings the tab can put on screen.
TAB_MODULES = (
    UI / "chat_view.py", UI / "chat_sessions.py",
    UI / "controllers" / "chat_controller.py", UI / "presenter" / "chat.py",
    *sorted((UI / "widgets").glob("chat_*.py")),
)


# ---------------------------------------------------------------------------
# Numbering and the prose
# ---------------------------------------------------------------------------

def test_numbers_are_given_in_first_mention_order_and_never_change():
    numbers = Numbering()
    assert numbers.feed("Rent is due [2]. Deposit [1]. Again [2].") == [2, 1]
    assert numbers.display(2) == 1 and numbers.display(1) == 2
    # More text arrives; nothing already shown is renumbered.
    numbers.feed("Rent is due [2]. Deposit [1]. Again [2]. And a third [3].")
    assert [numbers.display(n) for n in (2, 1, 3)] == [1, 2, 3]
    assert numbers.in_order() == [2, 1, 3]


def test_a_marker_still_arriving_is_held_back_not_drawn():
    numbers = Numbering()
    assert "[" not in render_answer_html("The deposit is held [", numbers)
    assert "[1" not in render_answer_html("The deposit is held [1", numbers)
    whole = render_answer_html("The deposit is held [1]", numbers, linkable={1})
    assert '<a href="receipt:1">1</a>' in whole


def test_only_a_source_that_exists_is_a_link():
    numbers = Numbering()
    out = render_answer_html("Yes [1] and [2].", numbers, linkable={1})
    assert 'href="receipt:1"' in out
    assert 'href="receipt:2"' not in out and "<sup>2</sup>" in out


def test_the_answer_is_escaped_so_a_document_cannot_write_markup():
    out = render_answer_html("It said <b>pay now</b> & left [1]", Numbering(), {1})
    assert "<b>" not in out and "&lt;b&gt;" in out and "&amp;" in out


def test_a_receipt_becomes_a_result_card_with_its_own_identity():
    a = receipt_to_result(LETTER, 1)
    b = receipt_to_result(AGREEMENT, 2)
    assert (a.path, a.text, a.label) == (LETTER.path, LETTER.quote, "page 2")
    assert a.file_id != b.file_id and a.chunk_id != b.chunk_id
    nameless = receipt_to_result(Receipt(None, "C:/m/x.msg", "x", "q"), 3)
    other = receipt_to_result(Receipt(None, "C:/m/y.msg", "y", "q"), 4)
    assert nameless.file_id < 0 and nameless.file_id != other.file_id


# ---------------------------------------------------------------------------
# The shelf
# ---------------------------------------------------------------------------

def test_the_shelf_is_what_the_conversation_touched_and_pinned_come_first():
    shelf = Shelf()
    assert shelf.add_receipt(LETTER) and shelf.add_receipt(AGREEMENT)
    assert not shelf.add_receipt(LETTER), "a document sits on the shelf once"
    shelf.toggle_pin(AGREEMENT.path)
    assert shelf.paths() == [AGREEMENT.path, LETTER.path]


def test_a_removed_document_stays_out_until_it_is_added_on_purpose():
    shelf = Shelf()
    shelf.add_receipt(LETTER)
    shelf.remove(LETTER.path)
    assert shelf.paths() == []
    assert not shelf.add_receipt(LETTER), "a later mention must not put it back"
    assert shelf.add(LETTER.path, explicit=True), "a drag can"
    assert shelf.items[0].pinned, "and what you added yourself is kept"


def test_the_shelf_drops_the_oldest_loose_document_never_a_pinned_one():
    shelf = Shelf()
    shelf.add("C:/keep.txt", explicit=True)
    for i in range(MAX_UNPINNED + 3):
        shelf.add(f"C:/loose-{i}.txt")
    assert "C:/keep.txt" in shelf.paths()
    assert "C:/loose-0.txt" not in shelf.paths()
    assert len([i for i in shelf.items if not i.pinned]) == MAX_UNPINNED


def test_the_shelf_round_trips():
    shelf = Shelf()
    shelf.add_receipt(LETTER)
    shelf.toggle_pin(LETTER.path)
    shelf.remove("C:/gone.txt")
    again = Shelf.from_dict(json.loads(json.dumps(shelf.to_dict())))
    assert again.paths() == shelf.paths() and again.items[0].pinned
    assert "C:/gone.txt" in again.removed


# ---------------------------------------------------------------------------
# Small wording decisions (4e-4)
# ---------------------------------------------------------------------------

def test_a_stopped_answer_and_an_empty_search_say_where_they_end():
    assert closing_line(None, stopped=True) == presenter.STOPPED_LINE
    empty_find = ChatTurn("assistant", "Here is what I found.", kind="find", result_set=[])
    assert "Nothing" in closing_line(empty_find)
    full_find = ChatTurn("assistant", "Here.", kind="find", result_set=search_results(2))
    assert closing_line(full_find) == "", "the grid's own last row says 'That's all'"


def test_a_session_is_named_by_the_start_of_its_first_question():
    assert title_from_question("") == "New chat"
    assert title_from_question("What did we agree about the deposit?") == \
        "What did we agree about the deposit?"
    long = title_from_question("word " * 40)
    assert long.endswith("...") and len(long) <= 52


def test_the_page_says_what_to_do_when_the_helper_is_missing_never_a_trace():
    said = unavailable_text("")
    assert "Ollama" in said and "Check again" in said and "Searching" in said
    assert unavailable_text("Start Ollama first.") == "Start Ollama first."
    assert "Traceback" not in said


# ---------------------------------------------------------------------------
# Sessions: records and the adapter
# ---------------------------------------------------------------------------

def _session_with_an_answer() -> ChatSession:
    session = new_session()
    session.title, session.titled = "Deposit", True
    session.turns = [ChatTurn("user", "What about the deposit?"), deposit_turn(),
                     ChatTurn("assistant", "Photos.", kind="find",
                              result_set=search_results(2))]
    session.shelf.add_receipt(LETTER)
    session.shelf.toggle_pin(LETTER.path)
    return session


def test_a_session_survives_being_written_out_and_read_back():
    original = _session_with_an_answer()
    back = session_from_dict(json.loads(json.dumps(session_to_dict(original))))
    assert back.title == "Deposit" and back.titled
    assert [t.role for t in back.turns] == ["user", "assistant", "assistant"]
    assert back.turns[1].receipts == [LETTER, AGREEMENT]
    assert [r.path for r in back.turns[2].result_set] == [r.path for r in search_results(2)]
    assert back.shelf.items[0].pinned


def test_the_adapter_uses_the_stores_own_methods_when_it_has_them():
    backend = FakeSessionBackend()
    sessions = ChatSessions(backend)
    session = _session_with_an_answer()
    sessions.save(session_to_dict(session))
    assert list(backend.records) == [session.id]
    assert [s.id for s in sessions.load()] == [session.id]
    sessions.delete(session.id)
    assert backend.records == {} and sessions.load() == []


class _StateOnly:
    """A store with nothing but keyed state - what every store has today."""

    def __init__(self) -> None:
        self.state: dict = {}

    def get_state(self, key, default=""):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = value


def test_until_the_store_has_session_methods_the_same_records_live_in_keyed_state():
    backend = _StateOnly()
    sessions = ChatSessions(backend)
    first, second = _session_with_an_answer(), new_session()
    second.updated = first.updated + 10
    second.turns = [ChatTurn("user", "later")]
    sessions.save(session_to_dict(first))
    sessions.save(session_to_dict(second))
    assert [s.id for s in sessions.load()] == [second.id, first.id], "newest first"
    sessions.delete(first.id)
    assert [s.id for s in ChatSessions(backend).load()] == [second.id]


def test_a_broken_record_is_skipped_not_fatal():
    backend = _StateOnly()
    good = _session_with_an_answer()
    backend.state["chat:sessions"] = json.dumps(
        {good.id: session_to_dict(good), "bad": {"id": "bad", "turns": [5]}})
    assert [s.id for s in ChatSessions(backend).load()] == [good.id]
    backend.state["chat:sessions"] = "not json"
    assert ChatSessions(backend).load() == []


def test_a_store_that_hands_back_json_text_or_a_payload_wrapper_still_loads():
    good = _session_with_an_answer()

    class Wrapped:
        def load_sessions(self):
            return [json.dumps(session_to_dict(good)),
                    {"id": good.id + "x", "title": "T",
                     "payload": json.dumps({"turns": [], "shelf": {}})}]

    found = ChatSessions(Wrapped()).load()
    assert {s.id for s in found} == {good.id, good.id + "x"}


# ---------------------------------------------------------------------------
# No badges (3a, 3e)
# ---------------------------------------------------------------------------

def _docstring_nodes(tree: ast.AST) -> set:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                nodes.add(id(body[0].value))
    return nodes


def fixed_strings(path: Path) -> list[str]:
    """Every string literal a module could put on screen (docstrings excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in skip and n.value.strip()]


DENY = [re.compile(p, re.IGNORECASE) for p in presenter.WARNING_WORDS]


def test_the_deny_list_would_catch_a_banner():
    """A guard that cannot fail proves nothing."""
    for banner in ("AI-generated content - please verify", "Chat can make mistakes.",
                   "This answer may be inaccurate", "Experimental feature",
                   "Warning: double-check important info"):
        assert any(p.search(banner) for p in DENY), banner


def test_no_fixed_string_in_the_tab_is_a_warning_banner():
    checked = 0
    for path in TAB_MODULES:
        if path.name == "chat.py" and path.parent.name == "presenter":
            # This module *defines* the deny-list; its own regexes are not
            # strings the tab shows. Its shown strings are checked by name.
            strings = [getattr(presenter, name) for name in (
                "PLACEHOLDER", "EMPTY_HEADING", "EMPTY_HINT", "STOPPED_LINE",
                "FAILED_LINE", "NOT_BUILT_LINE", "SHELF_EMPTY")]
            strings.append(unavailable_text(""))
        else:
            strings = fixed_strings(path)
        for text in strings:
            checked += 1
            for pattern in DENY:
                assert not pattern.search(text), (path.name, pattern.pattern, text)
    assert checked > 100, "the scan found suspiciously little to read"


def test_the_fixed_words_are_plain_english():
    for text in ([getattr(presenter, n) for n in (
            "PLACEHOLDER", "EMPTY_HEADING", "EMPTY_HINT", "STOPPED_LINE",
            "FAILED_LINE", "NOT_BUILT_LINE", "SHELF_EMPTY")] + [unavailable_text("")]):
        lowered = text.lower()
        for jargon in SURFACE_JARGON + ("token", "llm", "rag ", "prompt"):
            assert jargon not in lowered, (jargon, text)


def test_the_view_stays_under_the_view_guard():
    lines = [l for l in (UI / "chat_view.py").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert len(lines) < 250, len(lines)
