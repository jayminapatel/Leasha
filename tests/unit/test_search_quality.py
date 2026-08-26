r"""The three queries that produced *"I am not happy with what the search does."*

Layer: L4

From `docs/WORKORDER-202626081059-search-quality.md`. The owner typed three
reasonable things, none of them worked, and the reasons were different in each
case:

```
find a project execution plan as a word document
find a project execution plan as a word document which is the latest
files from repo starting with DF_
```

The work order's §2 is the part worth remembering: `app.cli evaluate --builtin`
had already measured this - 88% on topic alone, 58% with a constraint, 0% on
recipients - and `evaluate.py` records the prediction it was built to test,
*"topic matching will be decent, constraints will be ignored"*. It was right.
The harness was not the problem; these tests are the response to it.

Acceptance A3 and A4 live here. A1 is the owner's own three queries against
their own corpus and cannot be asserted in a unit test - it is in §7 of the work
order for that reason.
"""

from __future__ import annotations

import pytest

from app.search.commands import expand_slashes
from app.search.query import parse_query, to_fts_match


def _terms(query: str) -> list[str]:
    return list(parse_query(query).terms)


def _searched(query: str) -> str:
    return to_fts_match(parse_query(query))


# --- F1: instruction words are not content ---------------------------------

@pytest.mark.parametrize("word", [
    "find", "show", "get", "give", "search", "looking", "need", "want",
    "anything", "latest", "newest", "biggest", "recent",
])
def test_an_instruction_word_is_not_searched_for(word: str) -> None:
    r"""**The owner talking to the application, matched against the corpus.**

    In an archive of project documents `find` hits thousands of files and drags
    the ranking with it. Thirteen words, none of them in `_STOPWORDS`, all of
    them going straight into the FTS expression.
    """
    assert word not in _searched(f"{word} the pump station report")


def test_the_first_query_stops_searching_for_the_word_find() -> None:
    """§1, query one, exactly as typed."""
    searched = _searched("find a project execution plan as a word document")

    assert "find" not in searched
    for wanted in ("project", "execution", "plan"):
        assert wanted in searched


@pytest.mark.parametrize("word", ["find", "show me", "get me"])
def test_a1_prefixing_an_instruction_does_not_change_what_is_searched(word: str) -> None:
    """Acceptance A3: the prefix must make no difference to the expression."""
    plain = _searched("pump station commissioning")
    prefixed = _searched(f"{word} pump station commissioning")

    assert prefixed == plain


def test_an_instruction_word_alone_is_still_searched_for() -> None:
    r"""**Why this is a second list and not more `_STOPWORDS`.**

    Every one of these is also a real thing to look for - "latest version", a
    file named `Search.md`. A stopword is never worth searching for on its own;
    one of these frequently is, so they may only be dropped while something else
    survives.
    """
    assert "latest" in _searched("latest")
    assert "find" in _searched("find")


def test_dropping_instructions_never_empties_the_query() -> None:
    """Two instruction words and nothing else must not become a search for
    nothing - the same guard `_content_terms` already had for stopwords."""
    assert _searched("show me the latest") != ""


def test_instruction_words_stay_in_the_terms_for_the_vector_half() -> None:
    r"""They are noise to BM25 and context to an embedding, which is exactly the
    treatment `_STOPWORDS` already gets: *"report from Dave"* and *"report for
    Dave"* embed differently, and should."""
    assert "find" in _terms("find a project execution plan")


# --- F4: underscores are part of the word ----------------------------------

@pytest.mark.parametrize("typed,expected", [
    ("DF_1234", "DF_1234"),
    ("DF_", "DF_"),                    # was `DF` - the anchor silently gone
    ("__init__.py", "__init__.py"),    # was `init`, `py` - both underscores lost
    ("_private", "_private"),
    ("SNAKE_CASE_", "SNAKE_CASE_"),
])
def test_a4_an_identifier_reaches_the_term_list_intact(typed: str, expected: str) -> None:
    r"""**`[^\W_]` excludes underscore, and that was the whole fault.**

    An underscore survived only between two letters, so a leading or trailing
    one was dropped and the search went off to look for something else. It
    matters more since this application went from 34 source types to 405:
    `__init__`, `_private` and `DF_` prefixes are what somebody types into a
    code search.
    """
    assert _terms(typed) == [expected]


def test_a_run_of_underscores_is_still_punctuation() -> None:
    """Admitting underscore as a word character wholesale would put separator
    runs - the `___` under a heading - into the term list."""
    assert _terms("___") == []
    assert _terms("a _ b") == ["a", "b"]


@pytest.mark.parametrize("typed", ["v1.2", "o'brien", "pump-station", "install*"])
def test_the_shapes_that_already_worked_still_do(typed: str) -> None:
    """The regression that matters: this pattern runs over every query."""
    assert _terms(typed) == [typed]


# --- F3: there was no way to ask for the newest thing -----------------------

@pytest.mark.parametrize("query,expected", [
    ("/newest pump", "newest"),
    ("pump /latest", "newest"),
    ("pump /oldest", "oldest"),
    ("pump sort:date", "newest"),
    ("pump sort:oldest", "oldest"),
    ("pump station", ""),
])
def test_f3_the_sort_switch_is_read(query: str, expected: str) -> None:
    r"""**The one finding that was a missing feature rather than a defect.**

    The complete filter set - `type, from, to, subject, has, after, before,
    path, repo, name, size` - all narrows, and nothing ordered. *"which is the
    latest"* was therefore unanswerable by any mechanism the application had,
    and Interpret could not rescue it either: translation may only emit
    operators that exist.
    """
    from app.search.commands import expand_slashes

    assert parse_query(expand_slashes(query)).sort == expected


def test_a_valueless_switch_does_not_eat_the_next_word() -> None:
    r"""`/newest pump` expanded to `newest:` and the collapse then glued `pump`
    on as its argument - so the sort switch consumed the search term and the
    query became a sort of nothing."""
    from app.search.commands import expand_slashes

    parsed = parse_query(expand_slashes("/newest pump station"))

    assert parsed.sort == "newest"
    assert list(parsed.terms) == ["pump", "station"]


def test_a_date_written_with_slashes_is_still_not_a_switch() -> None:
    """The rule that makes the whole `/` grammar safe: an unknown `/word` is
    left exactly as typed."""
    from app.search.commands import expand_slashes

    assert expand_slashes("12/03 and D:/docs") == "12/03 and D:/docs"


def test_an_unusable_sort_value_is_reported_rather_than_guessed() -> None:
    assert parse_query("pump sort:banana").unknown_operators == ("sort:banana",)


# --- F2 and F7: offered, never applied --------------------------------------

@pytest.mark.parametrize("query,word", [
    ("find a project execution plan as a word document", "word"),
    ("get me all excel files", "excel"),
    ("any mail attachments about pumps", "mail"),
])
def test_f2_a_kind_word_beside_a_document_noun_is_offered(query: str, word: str) -> None:
    r"""`_EXT_GROUPS` has known twelve kind words since Layer 4 and `/type
    excel` works perfectly. **"get me all excel files" does nothing** - the word
    sits in `terms` and no `ext` filter is produced. The vocabulary was there;
    the bridge from prose to filter was not."""
    from app.ui.presenter import kind_suggestion

    assert kind_suggestion(query) == (word, f"type:{word}")


@pytest.mark.parametrize("query", [
    "word count in my thesis",
    "excel formula help",
    "pump station commissioning",
])
def test_a_kind_word_used_as_a_subject_is_not_offered(query: str) -> None:
    r"""*"word count"* and *"excel formula"* are real searches. **Silently
    mapping any kind word to a filter is how somebody loses a document and
    never learns why**, which is why the noun beside it decides."""
    from app.ui.presenter import kind_suggestion

    assert kind_suggestion(query)[0] == ""


def test_the_suggestion_is_an_offer_and_not_a_rewrite() -> None:
    r"""A non-negotiable says a query the application altered must be visible
    and editable. So this produces something to *show*, carrying a link -
    applying it is a click."""
    from app.ui.presenter import NOTICE_KIND_SUGGESTION, window_notices

    found = window_notices("get me all excel files", None)

    assert found and found[0].code == NOTICE_KIND_SUGGESTION
    assert 'href="apply:type:excel"' in found[0].message


def test_nothing_is_suggested_once_the_person_has_said_it() -> None:
    """A query already carrying `type:` needs no help choosing a type."""
    from app.ui.presenter import window_notices

    parsed = parse_query("excel files type:xlsx")

    assert window_notices("excel files type:xlsx", parsed) == []


def test_f7_the_interpret_hint_appears_only_where_it_would_help() -> None:
    r"""**A discoverability fault, not a user error**: a button whose value is
    invisible until pressed will not be pressed. But a hint on every search is
    a hint nobody reads, so it wants a long query, no operators already, and a
    kind word in it."""
    from app.ui.presenter import interpret_hint

    sentence = "find a project execution plan as a word document"

    assert interpret_hint(sentence, enabled=True)
    assert not interpret_hint(sentence, enabled=False)      # Ollama switched off
    assert not interpret_hint("pump station", enabled=True)  # too short
    assert not interpret_hint(f"{sentence} type:docx", enabled=True)


def test_the_hint_does_not_replace_the_parsing_fixes() -> None:
    r"""Search must work with Ollama stopped - the first non-negotiable. F1 to
    F3 are fixed in the parser; Interpret makes good queries better, it does
    not make bad parsing acceptable."""
    from app.ui.presenter import interpret_hint

    assert "find" not in _searched("find a project execution plan")
    assert interpret_hint("find a project execution plan", enabled=False) == ""


# ---------------------------------------------------------------------------
# The other half of /newest: the tabs that browse rather than search
#
# The engine sorts the fused hits, which covers the Search tab. Files, Code and
# Mail never reach the engine - they read the store directly - so a `/newest`
# typed there parsed cleanly and then changed nothing at all. A switch that is
# offered on every tab has to be honoured on every tab; the alternative is a
# menu that quietly lies, which costs more trust than the feature buys.
# ---------------------------------------------------------------------------

def _file(store, path: str, *, mtime_ns: int) -> int:
    return store.upsert_file(
        path=path, size_bytes=100, mtime_ns=mtime_ns, source_kind="file")


def test_browse_files_honours_newest_and_oldest(tmp_path) -> None:
    """The Files and Code tabs sort by date when asked, and by score when not."""
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "index.db") as store:
        _file(store, r"D:\a\old.txt", mtime_ns=1_000)
        _file(store, r"D:\a\new.txt", mtime_ns=9_000)

        def names(query: str) -> list[str]:
            rows = store.browse_files(parse_query(expand_slashes(query)))
            return [row["path"].rsplit("\\", 1)[-1] for row in rows]

        # No text at all: a listing, and newest-first is the standing default.
        assert names("/type txt") == ["new.txt", "old.txt"]
        assert names("/type txt /oldest") == ["old.txt", "new.txt"]
        assert names("/type txt /newest") == ["new.txt", "old.txt"]


def test_browse_files_sort_beats_the_relevance_order(tmp_path) -> None:
    """**The case that proves it is wired to the query, not to the empty box.**

    With text present the rows come back ranked by BM25, so a date sort has to
    override a real ordering rather than merely fill in for a missing one.
    """
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "index.db") as store:
        _file(store, r"D:\a\report report report.txt", mtime_ns=1_000)
        _file(store, r"D:\a\report.txt", mtime_ns=9_000)

        def names(query: str) -> list[str]:
            rows = store.browse_files(parse_query(expand_slashes(query)))
            return [row["path"].rsplit("\\", 1)[-1] for row in rows]

        assert names("report /oldest")[0] == "report report report.txt"
        assert names("report /newest")[0] == "report.txt"


def test_mail_passes_the_sort_through_to_the_store(tmp_path) -> None:
    """`/oldest` on the Mail tab reverses the mailbox, through `mail_filters`."""
    import json

    from app.storage.sqlite_store import SqliteStore
    from app.ui.presenter import mail_filters

    with SqliteStore(tmp_path / "index.db") as store:
        for name, sent_at in (("old", 1_000), ("new", 9_000)):
            file_id = store.upsert_file(
                path=f"D:\\mail.pst\\{name}", size_bytes=10,
                mtime_ns=sent_at * 1_000_000_000, source_kind="pst_message")
            with store.write() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO messages "
                    "(file_id, subject, sender, recipients, sent_at, has_attach) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (file_id, name, "dave", json.dumps([]), sent_at))

        def subjects(query: str) -> list[str]:
            filters = mail_filters(parse_query(expand_slashes(query)))
            return [row["subject"] for row in store.browse_messages(**filters)]

        assert subjects("/from dave") == ["new", "old"]
        assert subjects("/from dave /oldest") == ["old", "new"]
        assert subjects("/from dave /newest") == ["new", "old"]
