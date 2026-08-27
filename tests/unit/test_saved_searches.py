r"""Searches somebody named, and can run again.

Layer: L1/L4/L5. Adoptions §3.

**A query, not a result set.** That is the whole design and it is the only
thing here worth arguing about. Storing the ids of what matched would have
been simpler and would have produced a feature that quietly goes stale: the
invoice indexed on Tuesday would never appear in `saved:invoices`, and nothing
on screen would say why. So a saved search re-executes, and the acceptance
test for it is literally that a document created after the save turns up in
the results.

**Nothing saves itself.** §3b, and it is a rule about trust rather than about
scope: a list somebody curated one at a time is worth something, and a list an
application filled in for them is a list they have to prune. `suggest_name`
fills a box in a dialog the person opened; that is the whole of the automation.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search.saved import (
    NAME_MAX, SavedSearch, clean_name, expand_saved, find, mentions_saved,
    name_key, names_in, ordered, suggest_name, summary,
)


@pytest.fixture()
def store():
    from app.storage.sqlite_store import SqliteStore

    built = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "saved.db").connect()
    yield built
    built.close()


# ---------------------------------------------------------------------------
# The name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("typed", "expected"), [
    ("  Leeds   surveys ", "Leeds surveys"),
    ("invoices", "invoices"),
    ("", ""),
    ("   ", ""),
])
def test_a_name_is_cleaned_rather_than_refused(typed, expected):
    """Somebody who typed two spaces meant one. **An error message about
    whitespace is an application telling a person off for nothing.**"""
    assert clean_name(typed) == expected


@pytest.mark.parametrize("character", ['"', "'", ":", "/", "\\"])
def test_the_characters_the_grammar_would_misread_are_removed(character):
    r"""A search called `type:pdf` produces `saved:type:pdf` - a token that
    cannot be typed back out of the box, so the one thing a saved search must
    do is the one thing it could not."""
    cleaned = clean_name(f"a{character}b")
    assert character not in cleaned
    assert cleaned


def test_a_name_is_capped_rather_than_truncated_mid_menu():
    assert len(clean_name("x" * 200)) == NAME_MAX


def test_two_spellings_of_one_name_are_one_saved_search():
    """`Leeds` and `leeds` as two rows is two rows nobody can tell apart."""
    assert name_key("Leeds") == name_key("  leeds ")


def test_the_fold_is_unicode_aware_like_the_mail_columns_had_to_be(store):
    r"""**Folded in Python, because SQLite cannot.** Migration v14 needed a
    whole extra column for exactly this: `SELECT lower('JOSÉ')` returns
    `josÉ`, so a `UNIQUE` index over any SQL-side fold would let two spellings
    of one name both exist. `str.casefold` does the job, and it is applied
    before anything reaches the database rather than after.

    The measurement is in the test rather than in a comment, so the day
    SQLite grows ICU by default this stops being an argument from memory.
    """
    assert name_key("JOSÉ") == name_key("josé")
    assert store.conn.execute("SELECT lower('JOSÉ')").fetchone()[0] == "josÉ"

    # And `casefold` over `lower` even in Python: ß folds to ss, ẞ does not
    # lower to it. A name is a word somebody typed, not an ASCII identifier.
    assert name_key("STRASSE") == name_key("straße")
    assert "straße".lower() != "strasse"


# ---------------------------------------------------------------------------
# Save, list, rename, delete — the round trip §3a asks for
# ---------------------------------------------------------------------------

def test_a_saved_search_survives_a_round_trip(store):
    assert store.save_search("Invoices", "type:pdf from:accounts", "documents")
    found = store.saved_search("invoices")
    assert found["name"] == "Invoices"
    assert found["query"] == "type:pdf from:accounts"
    assert found["scope"] == "documents"


def test_saving_over_a_name_replaces_it_rather_than_adding_a_second(store):
    """What somebody means by typing a name they can see on the list."""
    store.save_search("weekly", "leeds", "all")
    store.save_search("Weekly", "leeds site", "mail")
    rows = store.saved_searches()
    assert len(rows) == 1
    assert rows[0]["query"] == "leeds site"
    assert rows[0]["name"] == "Weekly", "the newest spelling is the one shown"


def test_saving_over_a_name_keeps_how_often_it_was_run(store):
    """The count is a fact about how often they reach for it, not about the
    text - so editing the query must not reset it and re-sort the menu."""
    store.save_search("weekly", "leeds")
    store.note_saved_search_run("weekly")
    store.note_saved_search_run("weekly")
    store.save_search("weekly", "leeds site")
    assert store.saved_search("weekly")["run_count"] == 2


def test_an_empty_name_or_an_empty_query_saves_nothing(store):
    assert not store.save_search("", "leeds")
    assert not store.save_search("weekly", "   ")
    assert store.saved_searches() == []


def test_rename_moves_it_and_keeps_the_query(store):
    store.save_search("weekly", "leeds site")
    assert store.rename_saved_search("weekly", "Leeds site visits")
    assert store.saved_search("weekly") is None
    assert store.saved_search("leeds site visits")["query"] == "leeds site"


def test_renaming_onto_a_name_already_used_is_refused_not_merged(store):
    """**False rather than a silent merge.** Two saved searches becoming one
    is data loss, and the caller is a dialog that can say so."""
    store.save_search("a", "one")
    store.save_search("b", "two")
    assert not store.rename_saved_search("a", "B")
    assert store.saved_search("a")["query"] == "one"


def test_changing_only_the_case_of_a_name_is_allowed(store):
    """It collides with itself, and refusing that would be a rule with no
    purpose beyond the shape of the uniqueness check."""
    store.save_search("weekly", "leeds")
    assert store.rename_saved_search("weekly", "Weekly")
    assert store.saved_search("weekly")["name"] == "Weekly"


def test_renaming_something_that_is_not_there_is_false_not_a_crash(store):
    assert not store.rename_saved_search("nothing", "something")


def test_delete_forgets_it(store):
    store.save_search("weekly", "leeds")
    assert store.delete_saved_search("WEEKLY")
    assert store.saved_searches() == []
    assert not store.delete_saved_search("weekly")


def test_the_list_puts_the_ones_actually_used_first(store):
    """A list read at a glance should open on the search somebody runs every
    Monday, not on whichever name happens to sort first."""
    store.save_search("aaa", "one")
    store.save_search("zzz", "two")
    for _ in range(3):
        store.note_saved_search_run("zzz")
    assert [row["name"] for row in store.saved_searches()] == ["zzz", "aaa"]


def test_ties_break_alphabetically_so_the_order_is_stable(store):
    for name in ("beta", "alpha", "gamma"):
        store.save_search(name, "x")
    assert [row["name"] for row in store.saved_searches()] == [
        "alpha", "beta", "gamma"]


def test_recording_a_run_of_something_unknown_is_not_an_error(store):
    store.note_saved_search_run("never saved")
    store.note_saved_search_run("")


# ---------------------------------------------------------------------------
# The `/` menu — "values with counts via the existing machinery"
# ---------------------------------------------------------------------------

def test_saved_searches_appear_in_the_value_menu_with_their_counts(store):
    """**Through `distinct_value_counts`, not through a second path.** The
    menu, the CLI completion and the model grammar all read one catalogue;
    a saved search offered by machinery of its own would be a fourth place
    for them to disagree."""
    store.save_search("invoices", "type:pdf")
    store.note_saved_search_run("invoices")
    store.note_saved_search_run("invoices")

    rows = store.distinct_value_counts("saved")
    assert [(row.value, row.count) for row in rows] == [("invoices", 2)]
    assert rows[0].exact


def test_the_number_beside_a_saved_search_says_runs_not_files():
    r"""**The honesty of the row.** Counting matching files would mean running
    every saved search behind a keystroke, which is the unbounded work the
    value menu exists to keep out - and printing a file count that is really a
    run count is a number somebody would believe and act on."""
    from app.storage.sqlite_store import ValueCount
    from app.ui.presenter import value_rows

    row = value_rows("saved", [ValueCount("invoices", 12, True)])[0]
    assert "12 runs" in row
    assert "files" not in row
    assert "1 run" in value_rows("saved", [ValueCount("weekly", 1, True)])[0]


def test_the_menu_offers_saved_searches_through_the_ordinary_suggestion_path(store):
    from app.ui.presenter import value_suggestions

    store.save_search("invoices", "type:pdf")
    store.save_search("weekly", "leeds")
    assert set(value_suggestions(store, "saved", "")) == {"invoices", "weekly"}
    assert value_suggestions(store, "saved", "inv") == ["invoices"]


def test_saved_is_offered_in_the_search_box_and_not_in_the_code_box():
    r"""**A list is not an affordance if half of it does nothing.** The Code
    box runs a repository search with no scope and no main engine, so `/saved`
    there would be a row that opens, completes, and silently searches for the
    words `saved` and a name."""
    from app.ui.widgets.code_commands import (
        ALL_CATALOGUE, SEARCH_CATALOGUE, search_matching,
    )

    assert "saved" in {c.name for c in SEARCH_CATALOGUE}
    assert "saved" not in {c.name for c in ALL_CATALOGUE}
    assert "saved" in {c.name for c in search_matching("/sa")}


def test_the_filter_catalogue_still_matches_the_parser_exactly():
    r"""**The guard `/saved` had to be kept out of `COMMANDS` for.** That
    tuple and `_FIELD_ALIASES` must be the same set - offered and parsed - and
    `saved:` is neither offered as a filter nor parsed as one. It is an
    expansion, replaced before the parser is called.
    """
    from app.search.commands import ACTIONS, COMMANDS
    from app.search.query import _FIELD_ALIASES

    documented = {s for command in COMMANDS for s in command.spellings}
    assert documented == set(_FIELD_ALIASES)
    assert not {s for command in ACTIONS for s in command.spellings} & documented


def test_a_bare_slash_still_offers_every_filter_plus_the_actions():
    from app.search.commands import (
        ACTIONS, COMMANDS, matching, matching_all,
    )

    assert len(matching("/")) == len(COMMANDS)
    assert len(matching_all("/")) == len(COMMANDS) + len(ACTIONS)


def test_the_model_is_never_told_about_saved():
    """A model cannot guess a name somebody chose, and a `saved:` it invented
    would search for two words and look deliberate doing it."""
    from app.search.commands import grammar_for_model

    assert "saved:" not in grammar_for_model()


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------

def test_the_slash_form_becomes_the_token_like_every_other_switch():
    from app.search.commands import expand_slashes

    assert expand_slashes("/saved invoices") == "saved:invoices"


def test_a_token_expands_to_the_query_it_stands_for():
    saved = [SavedSearch("invoices", "type:pdf from:accounts", "documents")]
    assert expand_saved("saved:invoices", saved) == (
        "type:pdf from:accounts", "documents")


def test_it_expands_in_place_rather_than_replacing_the_whole_box():
    r"""`saved:weekly leeds` is the saved search **plus** leeds. Throwing away
    something somebody just typed is not a behaviour any sentence on screen
    makes acceptable."""
    saved = [SavedSearch("weekly", "type:pdf", "all")]
    query, _scope = expand_saved("saved:weekly leeds", saved)
    assert query == "type:pdf leeds"


def test_a_name_with_a_space_survives_the_quoting_the_menu_inserts():
    from app.ui.presenter import as_typed_value

    entry = SavedSearch("leeds surveys", "site survey", "all")
    token = f"saved:{as_typed_value(entry.name)}"
    assert token == 'saved:"leeds surveys"'
    assert expand_saved(token, [entry])[0] == "site survey"
    assert entry.as_token() == token


def test_a_name_nobody_saved_is_left_exactly_as_typed():
    r"""The rule `expand_slashes` follows for an unknown `/word`. **Silently
    deleting part of a query is the one behaviour that makes a search box
    impossible to trust** - so an unknown name stays, finds nothing, and looks
    like what it is."""
    assert expand_saved("saved:nothing leeds", []) == ("saved:nothing leeds", "")


def test_an_ordinary_query_is_untouched_and_reports_no_scope():
    """`""` for the scope is what tells the caller to leave the control alone.
    A saved search that did not run must not move somebody's scope."""
    assert expand_saved("leeds site survey", []) == ("leeds site survey", "")
    assert not mentions_saved("leeds site survey")
    assert not mentions_saved("look in c:/saved/reports")


def test_the_first_token_decides_the_scope():
    a = SavedSearch("a", "one", "mail")
    b = SavedSearch("b", "two", "code")
    assert expand_saved("saved:a saved:b", [a, b])[1] == "mail"


def test_the_names_a_query_refers_to_are_readable_without_expanding_it():
    assert names_in('saved:a saved:"two words" leeds') == ("a", "two words")


def test_a_name_is_matched_however_it_is_capitalised():
    saved = [SavedSearch("Invoices", "type:pdf", "all")]
    assert find(saved, "invoices").query == "type:pdf"
    assert expand_saved("saved:INVOICES", saved)[0] == "type:pdf"


# ---------------------------------------------------------------------------
# A smart folder, not a snapshot — the acceptance test
# ---------------------------------------------------------------------------

def test_running_a_saved_search_finds_documents_indexed_after_it_was_saved(store):
    r"""**The item's own words, tested literally.** *Run = re-execute live (a
    smart folder, not a snapshot).* A stored list of results would pass every
    other test in this file and fail this one, which is why it is here.
    """
    import time

    from app.search.engine import SearchEngine

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

        def embed_all(self, _t):
            raise RuntimeError("no model")

    def add(name: str, text: str) -> None:
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext="txt", size_bytes=1,
            mtime_ns=time.time_ns(), status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    add("first.txt", "the Leeds site survey")
    store.save_search("leeds", "leeds survey", "all")

    engine = SearchEngine(store, _NoVectors(), _NoModel())
    try:
        saved = [SavedSearch(**{k: row[k] for k in
                                ("name", "query", "scope", "run_count")})
                 for row in store.saved_searches()]
        query, scope = expand_saved("saved:leeds", saved)
        assert scope == "all"
        before = engine.search(query, use_cache=False).results
        assert len(before) == 1

        # Indexed *after* the save. A snapshot would never see it.
        add("second.txt", "another Leeds survey, written later")
        after = engine.search(query, use_cache=False).results
        assert len(after) == 2
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# The empty box: recent, then saved
# ---------------------------------------------------------------------------

def test_the_empty_box_lists_saved_searches_beneath_the_recent_ones():
    from app.ui.first_contact import RECENT_HEADING, SAVED_HEADING, sections

    found = sections(
        [{"query": "leeds site"}],
        [SavedSearch("invoices", "type:pdf", "all", 3)])
    assert [heading for heading, _rows in found] == [
        RECENT_HEADING, SAVED_HEADING]


def test_a_section_with_nothing_in_it_is_not_drawn():
    """A heading over an empty list is a promise the box does not keep."""
    from app.ui.first_contact import SAVED_HEADING, sections

    assert sections([], []) == ()
    assert [h for h, _ in sections([], [SavedSearch("a", "x")])] == [SAVED_HEADING]


def test_a_saved_row_inserts_the_reference_rather_than_a_frozen_copy():
    r"""**"A smart folder, not a snapshot", one level down.** Inserting the
    stored query would put a copy in the box; edit it, press Enter, and you
    have run something that is no longer the thing you saved."""
    from app.ui.first_contact import saved_rows

    (label, insert), = saved_rows([SavedSearch("invoices", "type:pdf", "all")])
    assert insert == "saved:invoices"
    assert "invoices" in label and "type:pdf" in label


def test_a_saved_row_shows_what_the_search_actually_does():
    """A name six months old says nothing. **A saved search nobody dares run
    may as well not exist.**"""
    assert summary(SavedSearch("x", "type:pdf from:dave")) == "type:pdf from:dave"
    long_one = summary(SavedSearch("x", "word " * 40))
    assert len(long_one) <= 48 and long_one.endswith("…")


def test_the_rows_are_ordered_the_way_the_menu_orders_them():
    entries = ordered([SavedSearch("a", "x", "all", 1),
                       SavedSearch("b", "y", "all", 9)])
    assert [e.name for e in entries] == ["b", "a"]


def test_a_malformed_row_costs_one_entry_and_not_the_list():
    from app.ui.first_contact import saved_rows

    rows = saved_rows([{"name": "ok", "query": "x"}, {"name": "", "query": "y"},
                       None, "not a row"])
    assert [insert for _label, insert in rows] == ["saved:ok"]


# ---------------------------------------------------------------------------
# §3b — nothing saves itself
# ---------------------------------------------------------------------------

def test_nothing_is_ever_saved_without_being_asked(store):
    r"""**Searched is not saved.** The searches table has logged every query
    since Layer 4; if any of that ever reached this table the list would fill
    with a hundred half-typed queries and the feature would be worthless.
    """
    store.log_search("leeds site survey", filters=None, hits=3, elapsed_ms=10,
                     rerank_on=False)
    assert store.saved_searches() == []


def test_the_suggested_name_is_a_default_and_drops_the_machinery():
    assert suggest_name("type:pdf the leeds site survey") == "leeds site survey"
    assert suggest_name("type:pdf -draft") == ""


def test_a_suggested_name_is_already_a_legal_one():
    """It goes straight into the box; a default that would then be rejected
    is worse than an empty one."""
    suggested = suggest_name('"site survey" from:dave leeds')
    assert suggested == clean_name(suggested)


# ---------------------------------------------------------------------------
# The box's own object
# ---------------------------------------------------------------------------

def test_expanding_applies_the_saved_scope_and_only_then():
    from app.ui.saved_box import SavedSearches

    seen = []
    box = SavedSearches(None, seen.append)
    box._took([SavedSearch("weekly", "type:pdf", "mail")])

    assert box.expand("/saved weekly") == "type:pdf"
    assert seen == ["mail"]

    assert box.expand("leeds") == "leeds"
    assert seen == ["mail"], "an ordinary search must not move the scope"


def test_the_box_expands_slashes_before_saved_names():
    """`/saved weekly` has to become `saved:weekly` first, or the saved half
    never sees a token at all."""
    from app.ui.saved_box import SavedSearches

    box = SavedSearches()
    box._took([SavedSearch("weekly", "type:pdf")])
    assert box.expand("/saved weekly leeds") == "type:pdf leeds"


def test_the_box_with_no_store_is_a_box_with_no_saved_searches():
    from app.ui.saved_box import SavedSearches

    box = SavedSearches()
    box.refresh()
    box.save("weekly", "leeds")
    assert box.all == ()
    assert box.rows() == ()
    assert box.expand("saved:weekly") == "saved:weekly"


def test_the_whole_journey_from_a_slash_to_a_search(store):
    r"""**The outcome, not the mechanism.** Every other test here proves one
    joint. This one types what a person types and asserts what they see: the
    row in the menu, the count beside the name, the text left in the box, and
    the query that finally runs.

    It exists because this project has twice shipped a feature whose parts all
    worked and whose whole did not - a spelling correction that reached the
    notice and not the query, and a filter menu offering values the parser
    would not match. Both would have passed a file of joints like the one
    above.
    """
    pytest.importorskip("PyQt6")

    from app.ui.presenter import value_suggestions
    from app.ui.saved_box import SavedSearches
    from app.ui.widgets.search_bar import build_input

    store.save_search("invoices", "type:pdf from:accounts", "documents")
    store.note_saved_search_run("invoices")
    store.note_saved_search_run("invoices")

    box, popup, _saved = build_input(None, lambda *_a: None, lambda *_a: None,
                                     store=store)

    def rows():
        return [popup._model.item(i).text()
                for i in range(popup._model.rowCount())]

    # 1. `/` shows it, and `/sa` narrows to it.
    box.setText("/sa")
    box.textEdited.emit("/sa")
    assert [row.split()[0] for row in rows()] == ["/saved"]

    # 2. Choosing it leaves the syntax the box can be edited by hand in.
    popup.activated[str].emit(rows()[0])
    assert box.text() == "saved:"
    assert popup.value_of == "saved"

    # 3. The values, as the worker delivers them - with the count.
    counts: dict = {}
    found = value_suggestions(store, "saved", "", counts=counts)
    popup.set_values("saved", [counts.get(value, value) for value in found])
    assert rows() == ["invoices                       2 runs"]

    # 4. Picking one completes the token.
    popup.activated[str].emit(rows()[0])
    assert box.text() == "saved:invoices "

    # 5. And what runs is the saved query, at the saved scope.
    scopes: list = []
    saved_box = SavedSearches(store, scopes.append)
    saved_box._took(ordered(store.saved_searches()))
    assert saved_box.expand(box.text()) == "type:pdf from:accounts"
    assert scopes == ["documents"]


def test_the_view_reads_saved_searches_through_a_worker_and_never_directly():
    r"""**The rule `test_no_store_call_outside_a_worker` encodes**, checked
    here at the level of this feature: `saved_box` holds the answer and hands
    the store to `workers`, because a query on the interface thread is a
    freeze waiting for a busy index - and this one sits between a keystroke
    and a search.
    """
    import pathlib as _pathlib

    source = (_pathlib.Path(__file__).resolve().parents[2]
              / "app" / "ui" / "saved_box.py").read_text(encoding="utf-8")
    for call in ("_store.saved_searches(", "_store.save_search(",
                 "_store.rename_saved_search(", "_store.delete_saved_search("):
        assert call not in source, f"saved_box.py calls {call} on the UI thread"
