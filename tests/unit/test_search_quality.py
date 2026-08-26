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
