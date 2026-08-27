r"""Somebody pasted an error message.

Layer: L4. Two things live here: the detection, and **a crash it uncovered in
released code** — a quoted phrase containing a camelCase word made FTS5 answer
`syntax error near "+"` and failed the whole search. Typing
`"getUserName handler"` was enough to do it.

Measured before and after, on a corpus where two of four files hold the line:

    pasted error, as a bag of ORed words   4 of 4 - both irrelevant ones too
    pasted error, as a phrase              2 of 2 - exactly the files with it
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search.pasted import MIN_WORDS, as_phrase, looks_pasted


# --------------------------------------------------------------------------
# Telling a paste from a question
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "AttributeError: 'NoneType' object has no attribute 'chunk_id'",
    "Traceback (most recent call last):",
    'File "app/search/engine.py", line 512, in search',
    "engine.py:512 in search",
    "at com.acme.Thing.run(Thing.java:88)",
    "TypeError: Cannot read properties of undefined (reading 'length')",
    "  File \"x.py\", line 5\n    raise ValueError(text)",
])
def test_these_arrived_from_a_clipboard(text):
    assert looks_pasted(text)


@pytest.mark.parametrize("text", [
    "the safety report for the Leeds site",
    "volcano homework essay",
    "how do I fix the pump station drawings",
    "what did Dave send me about the licence renewal last year",
    "cannot read property length of undefined",
    "type:pdf budget 2024",
    "foo()",
    "",
    "   ",
])
def test_these_were_typed(text):
    r"""**Deliberately hard to trigger.** A false positive turns an ordinary
    multi-word search into a phrase search, which silently narrows it - the
    failure this whole order exists to remove.

    "cannot read property length of undefined" is the interesting one: it is
    the *words* of a real error, typed from memory rather than pasted, and it
    carries no punctuation to give it away. Treating it as a phrase would be
    wrong, because somebody typing from memory rarely has the order right.
    """
    assert not looks_pasted(text)


def test_a_named_tell_outranks_the_size_gates():
    """`engine.py:512` is unambiguous at twenty-three characters, and the
    length rule threw it away until a run of real examples showed it. Nothing
    somebody types looks like a path and a line number."""
    assert len("engine.py:512") < 24
    assert looks_pasted("engine.py:512")


def test_one_sign_is_never_enough():
    """Everything without a named tell needs two independent signs of being
    code, so an ordinary sentence that happens to contain a bracket does not
    become a phrase search."""
    assert not looks_pasted("the meeting (the one last Tuesday) about budgets")


def test_a_short_query_is_a_query_whatever_it_contains():
    assert not looks_pasted("a:b()")
    assert MIN_WORDS == 4


# --------------------------------------------------------------------------
# What gets searched for
# --------------------------------------------------------------------------

def test_the_words_in_order_with_the_punctuation_dropped():
    """**Order is what makes it findable.** The distinctive thing about a
    traceback line is its sequence, not its colons - and FTS5 can match a
    sequence of tokens even though it cannot match what sits between them."""
    assert as_phrase("AttributeError: 'NoneType' object has no attribute 'x'") == (
        "AttributeError NoneType object has no attribute x")


def test_a_whole_stack_trace_is_capped():
    """An adjacency query hundreds of tokens long can match nothing at all
    once one word was chunked away from the next. The first words of a paste
    are the error line; the rest is the frames below it."""
    huge = " ".join(f"word{n}" for n in range(200))
    assert len(as_phrase(huge).split()) == 24


# --------------------------------------------------------------------------
# The crash this uncovered
# --------------------------------------------------------------------------

def test_a_phrase_with_a_camelcase_word_does_not_crash_the_search():
    r"""**Released code answered `fts5: syntax error near "+"`.**

    `_fts_quote` turns `getUserName` into
    `("getUserName" OR ("get" AND "user" AND "name"))`, which is right in a
    term position and is not legal inside an adjacency chain - `+` joins
    strings and nothing else. So typing `"getUserName handler"` into the box
    failed the entire search, and it took a pasted-error query building the
    same shape to find it.
    """
    from app.search.query import parse_query

    expression = parse_query('"getUserName handler"').fts_match()
    assert expression == '("getUserName" + "handler")'
    assert " OR " not in expression and " AND " not in expression


def test_a_phrase_is_still_an_exact_order_request():
    """The narrower reading is also the right one: quoting something is the
    most explicit statement of intent the box offers. The document side of
    camelCase is covered by the `symbols` column, not by loosening this."""
    from app.search.query import parse_query

    assert parse_query('"get user name"').fts_match() == (
        '("get" + "user" + "name")')


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

_CORPUS = {
    "engine.py": "raise AttributeError(\"'NoneType' object has no attribute "
                 "'chunk_id'\")",
    "other.py": "def chunk_id(self): the object has no attribute here either",
    "log.txt": "Traceback: AttributeError: 'NoneType' object has no attribute "
               "'chunk_id'",
    "noise.md": "The object model has no attribute called chunk or id",
    "camel.py": "the getUserName handler is here",
}


@pytest.fixture(scope="module")
def engine():
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "pasted.db").connect()
    for name, text in _CORPUS.items():
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext=name.split(".")[-1],
            size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    class _NoVectors:
        def search(self, *_args, **_kwargs):
            return []

    class _NoModel:
        def embed(self, _text):
            raise RuntimeError("no embedding model in this test")

        def embed_all(self, _texts):
            raise RuntimeError("no embedding model in this test")

    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()


def _names(response):
    return {result.path.rsplit("/", 1)[-1] for result in response.results}


def test_a_pasted_error_finds_the_files_that_contain_it(engine):
    """**The measurement, closed.** As a bag of ORed words this returned all
    four documents, two of which have nothing to do with the error."""
    from app.search.engine import NOTICE_EXACT

    response = engine.search(
        "AttributeError: 'NoneType' object has no attribute 'chunk_id'",
        use_cache=False)
    assert _names(response) == {"engine.py", "log.txt"}
    assert any(notice.code == NOTICE_EXACT for notice in response.notices)


def test_the_notice_says_what_happened_and_how_to_undo_it(engine):
    """A phrase search is a narrowing, and somebody who did not mean to paste
    needs to see both that it happened and the way back."""
    from app.search.engine import NOTICE_EXACT

    response = engine.search("Traceback (most recent call last): line 5 in run",
                             use_cache=False)
    message = next(notice.message for notice in response.notices
                   if notice.code == NOTICE_EXACT)
    assert "pasted" in message and "quotes" in message


def test_a_typed_query_is_untouched(engine):
    from app.search.engine import NOTICE_EXACT

    response = engine.search("object has no attribute", use_cache=False)
    assert len(response.results) == 4
    assert not any(notice.code == NOTICE_EXACT for notice in response.notices)


def test_a_query_that_already_has_quotes_is_left_alone(engine):
    """They said what they wanted. Guessing over the top of an explicit
    instruction is the behaviour this order exists to remove."""
    from app.search.engine import NOTICE_EXACT

    response = engine.search('"getUserName handler"', use_cache=False)
    assert _names(response) == {"camel.py"}
    assert not any(notice.code == NOTICE_EXACT for notice in response.notices)


def test_a_query_with_a_filter_is_left_alone(engine):
    from app.search.engine import NOTICE_EXACT

    response = engine.search(
        "type:py AttributeError: 'NoneType' object has no attribute 'chunk_id'",
        use_cache=False)
    assert not any(notice.code == NOTICE_EXACT for notice in response.notices)
