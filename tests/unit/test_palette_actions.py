"""The command palette's registry and matching (IA-1 / MS-11, 2026-10-14). Qt-free.

Layer: L5 (presenter half).
"""

from __future__ import annotations

from app.ui.presenter import actions as pa


def _entries():
    ran = []
    pages = [pa.page_entry(t, lambda t=t: ran.append(t), icon="mail")
             for t in ("Search", "Mail", "Photos", "Files", "Settings")]
    settings = [pa.setting_entry(c, lambda c=c: ran.append(c)) for c in ("Appearance", "Accounts")]
    verbs = [pa.action_entry("&Preview pane", lambda: ran.append("preview"), keys="Ctrl+Shift+P"),
             pa.action_entry("Start indexing", lambda: ran.append("index"), keys="F5"),
             pa.action_entry("Sync now", lambda: ran.append("sync"), words=("accounts",)),
             pa.action_entry("Save this search…", lambda: ran.append("save"))]
    return pa.build(pages, settings, verbs), ran


def test_every_kind_is_listed_once_and_in_its_order_for_an_empty_line():
    entries, _ran = _entries()
    shown = pa.match(entries, "")
    assert [e.kind for e in shown][:5] == [pa.PAGE] * 5
    assert {e.kind for e in shown} == {pa.PAGE, pa.SETTING, pa.ACTION}
    assert len({e.key for e in shown}) == len(shown)


def test_a_word_finds_the_page_it_starts_and_runs_it():
    entries, ran = _entries()
    shown = pa.match(entries, "mai")
    assert shown[0].text == "Go to Mail"
    shown[0].run()
    assert ran == ["Mail"]


def test_words_match_fuzzily_by_start_inside_and_letter_by_letter():
    entries, _ran = _entries()
    assert pa.match(entries, "pre pa")[0].text == "Preview pane"
    assert pa.match(entries, "index")[0].text == "Start indexing"
    assert pa.match(entries, "prvw")[0].text == "Preview pane"
    assert pa.match(entries, "accounts")[0].text in ("Settings: Accounts", "Sync now")
    assert "Sync now" in [e.text for e in pa.match(entries, "accounts")]


def test_the_accelerator_marker_is_not_part_of_the_words():
    assert pa.clean_text("&Preview pane") == "Preview pane"
    assert pa.clean_text("Q&&A") == "Q&A"


def test_a_line_starting_with_the_marker_shows_actions_only():
    entries, _ran = _entries()
    shown = pa.match(entries, ">")
    assert shown and {e.kind for e in shown} == {pa.ACTION}
    narrowed = pa.match(entries, "> sync")
    assert [e.text for e in narrowed] == ["Sync now"]
    assert pa.match(entries, ">mail") == [], "no page, and no search line, after >"


def test_plain_text_always_ends_with_search_for_it():
    entries, _ran = _entries()
    nothing = pa.match(entries, "quarterly gas invoice")
    assert [e.kind for e in nothing] == [pa.SEARCH]
    assert nothing[0].text == "Search for 'quarterly gas invoice'"
    assert nothing[0].query == "quarterly gas invoice"
    some = pa.match(entries, "mail")
    assert some[0].text == "Go to Mail" and some[-1].kind == pa.SEARCH


def test_the_search_line_runs_what_the_caller_gives_it():
    entries, _ran = _entries()
    searched = []
    line = pa.match(entries, "zebra", search=lambda q: (lambda: searched.append(q)))[-1]
    line.run()
    assert searched == ["zebra"]


def test_the_first_of_two_entries_with_one_key_is_kept():
    first = pa.page_entry("Mail", lambda: None, keys="Ctrl+M")
    again = pa.page_entry("Mail", lambda: None)
    assert pa.build([first], [again]) == [first]


def test_the_limit_holds_and_still_leaves_room_for_the_search_line():
    many = [pa.action_entry(f"Thing {n}", lambda: None) for n in range(80)]
    shown = pa.match(many, "thing", limit=10)
    assert len(shown) == 10 and shown[-1].kind == pa.SEARCH
