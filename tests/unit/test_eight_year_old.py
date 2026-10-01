r"""The acceptance test the whole order is written around.

Layer: L4/L5. §7: *the 8-year-old scenarios, as integration tests on the
fixture corpus, and every scenario runs with Ollama absent.*

**Ollama is absent here and cannot become present.** The engine is built with
an embedder that raises if anything reaches for it, and no translator is
constructed at all — so a scenario that only passes on a machine with a model
cannot pass here. That is the point: the acceptance test for the first tab is
a child finding her homework on an ordinary laptop.

Each test below is one sentence from the order, and the assertion is the
outcome a person would describe, not the mechanism that produced it.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search.engine import (
    NOTICE_RELAXED, NOTICE_SPELLING, SearchEngine,
)
from app.search.policy import CODE, SEARCH, for_surface


class _NoVectors:
    def search(self, *_args, **_kwargs):
        return []


class _NoModel:
    """**Ollama absent, and the embedding model too.**

    Anything that reaches for a model in these scenarios fails loudly rather
    than quietly making the test pass on a machine that happens to have one.
    """

    def embed(self, _text):
        raise AssertionError("no scenario here may need the embedding model")

    def embed_all(self, _texts):
        raise AssertionError("no scenario here may need the embedding model")


@pytest.fixture(scope="module")
def homework():
    """A child's folder: her essay, her spelling list, and her father's mail."""
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "home.db").connect()
    for name, text, ext, sender in (
        ("Homework/volcanoes.docx",
         "My homework about volcanoes. A volcano erupts when magma rises "
         "through a crack in the crust. Mount Etna is in Sicily.",
         "docx", ""),
        ("Homework/spellings.docx",
         "Spelling list for this week: separate, necessary, rhythm.",
         "docx", ""),
        ("Homework/rivers.docx",
         "My homework about rivers and how they carve valleys over time.",
         "docx", ""),
        ("Mail/dad-school-trip.eml",
         "Subject: School trip\nFrom: dave.smith@acme.com\nTo: me@acme.com\n\n"
         "The trip to the science museum is on the fifteenth. Bring a packed "
         "lunch.", "eml", "dave.smith@acme.com"),
    ):
        file_id = store.upsert_file(
            f"C:/Users/child/{name}", parent_dir="C:/Users/child",
            ext=ext, size_bytes=1, mtime_ns=1, status="INDEXED",
            source_kind="eml" if ext == "eml" else "file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
        if sender:
            # A sender lives on `messages`, not on `files` - which is what
            # makes `from:` a subquery rather than a column.
            store.set_message(file_id, subject="School trip", sender=sender,
                              recipients='["me@acme.com"]', has_attach=0)

    engine = SearchEngine(store, _NoVectors(), _NoModel())
    yield engine, store
    engine.close()


def _names(response):
    return [result.path.rsplit("/", 1)[-1] for result in response.results]


def _notice(response, code):
    return next((n.message for n in response.notices if n.code == code), None)


# --------------------------------------------------------------------------
# 2a — she spells it wrong
# --------------------------------------------------------------------------

def test_a_misspelled_word_still_finds_her_essay(homework):
    r"""**The sentence the whole order is built on.**

    A child types "volcanno", gets nothing, and concludes her essay is gone -
    she has no model in which a search box is fussy about spelling. Now she
    gets the essay, and the page tells her which word was used.

    (The order's own example, "volcanoe", never reaches this: FTS5 stems it
    to "volcano" and finds the essay directly. See the §2a note.)
    """
    engine, _store = homework
    response = engine.search("volcanno", policy=for_surface(SEARCH),
                             use_cache=False)
    assert "volcanoes.docx" in _names(response)
    assert _notice(response, NOTICE_SPELLING) == "also looked for 'volcano'"


def test_she_is_told_what_was_searched_for(homework):
    """**Changing what somebody typed is only legal because it is on
    screen.** An eight-year-old who sees "also looked for 'homework'" learns
    something; one who sees corrected results and no explanation learns that
    computers are arbitrary."""
    engine, _store = homework
    response = engine.search("homwork", policy=for_surface(SEARCH),
                             use_cache=False)
    assert _notice(response, NOTICE_SPELLING) == "also looked for 'homework'"


# --------------------------------------------------------------------------
# 2b — she types too much
# --------------------------------------------------------------------------

def test_an_over_specified_sentence_still_lands(homework):
    r"""She remembers the phrase not quite right and quotes it, which is what
    a child does when told to "search properly". Every word of an exact
    phrase must match, adjacent and in order, so one wrong word empties the
    page - and the page says so instead of showing nothing."""
    engine, _store = homework
    response = engine.search('"my homework about big volcanoes"',
                             policy=for_surface(SEARCH), use_cache=False)
    assert "volcanoes.docx" in _names(response)
    message = _notice(response, NOTICE_RELAXED)
    assert message and message.startswith("Nothing contains the exact phrase")


def test_the_label_names_what_was_let_go_of(homework):
    """§2b's requirement in one assertion: **never silent.** The label is what
    makes the re-run legal."""
    engine, _store = homework
    response = engine.search("volcanoes type:pdf", policy=for_surface(SEARCH),
                             use_cache=False)
    assert _notice(response, NOTICE_RELAXED) == (
        "Nothing matched with the file-type filter applied - these ignore it.")


# --------------------------------------------------------------------------
# 3a/3b — a plain sentence with a name in it
# --------------------------------------------------------------------------

def test_a_plain_sentence_offers_the_filters_it_recognised(homework):
    """**No model involved.** The name is in the index, so it becomes a chip;
    the typed words are untouched beside it."""
    engine, store = homework
    from app.ui.presenter import chips_for

    sentence = "the email Dave sent about the school trip"
    chips = chips_for(store, sentence, for_surface(SEARCH))
    labels = [chip.label() for chip in chips]
    assert "from dave.smith@acme.com" in labels
    assert engine.search(sentence, policy=for_surface(SEARCH),
                         use_cache=False).parsed.raw == sentence


def test_a_name_the_index_does_not_know_produces_no_chip(homework):
    """High precision by construction: the failure is a chip that does not
    appear, never one that empties the page."""
    _engine, store = homework
    from app.ui.presenter import chips_for

    chips = chips_for(store, "the email Mortimer sent about the trip",
                      for_surface(SEARCH))
    assert not [chip for chip in chips if chip.field == "from"]


# --------------------------------------------------------------------------
# The whole journey, and the tab contract
# --------------------------------------------------------------------------

def test_she_finds_her_homework(homework):
    r"""**The literal acceptance test**: an eight-year-old finds her homework.

    Typed the way a child types - a whole question, with a spelling mistake
    in it - on a machine with no Ollama and no embedding model.
    """
    engine, _store = homework
    response = engine.search("my homework about volcannos",
                             policy=for_surface(SEARCH), use_cache=False)
    assert _names(response)[0] == "volcanoes.docx"


def test_two_misspelled_words_are_deliberately_left_alone(homework):
    """**One unknown word is a typo; two is the wrong corpus.**

    Correcting only the first would leave the second still missing, and the
    words are ORed - so the "corrected" query returns what the one working
    word returned, under a notice claiming something was fixed. The honest
    answer is the unmatched-terms notice.
    """
    engine, _store = homework
    response = engine.search("my homwork about volcannos",
                             policy=for_surface(SEARCH), use_cache=False)
    assert _notice(response, NOTICE_SPELLING) is None
    assert set(response.unmatched) == {"homwork", "volcannos"}


def test_the_same_query_on_the_code_tab_does_none_of_it(homework):
    """**The other half of §1a.** One engine, four contracts: the Code tab
    corrects nothing, relaxes nothing and offers no chips, because
    `recieve_handler` may be exactly what is in the codebase."""
    engine, store = homework
    from app.ui.presenter import chips_for

    response = engine.search("volcanno", policy=for_surface(CODE),
                             use_cache=False)
    assert _notice(response, NOTICE_SPELLING) is None
    assert _notice(response, NOTICE_RELAXED) is None
    # 1 October 2026, owner: plain English is read the same way on every tab - so the Code tab
    # now offers the same chips as Search. Spelling and relaxation stay off.
    assert chips_for(store, "the email Dave sent", for_surface(CODE)) ==         chips_for(store, "the email Dave sent", for_surface(SEARCH))


def test_every_scenario_ran_without_a_model(homework):
    """The fixture's embedder raises on any call. If a scenario above had
    reached for a model, it would have failed rather than passed on a machine
    that happened to have one."""
    engine, _store = homework
    with pytest.raises(AssertionError):
        engine.embedder.embed("anything")
