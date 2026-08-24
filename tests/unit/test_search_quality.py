"""Does a plain sentence find the right document? A floor, not a ceiling.

Layer: L4

The work order's first instruction was to measure before building, and the
measurement found two faults that no unit test had - because every unit test
asked "does this function return what I expect" and none asked "does search
work".

**One: every term was ANDed, stopwords included.** "drawings of the pump
station" became `drawings AND of AND the AND pump AND station`, and the document
contains neither "of" nor "the". Nineteen of twenty sentences returned *zero
results* - not badly ranked, not ranked at all.

**Two: ANDing the remaining content words was still too strict.** People
describe documents with words that are *about* them rather than *in* them -
"email", "version", "deck". One such word excluded everything.

These tests are the guard. They are deliberately loose thresholds: the point is
to catch a change that breaks search, not to chase a number.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search import keyword
from app.search.commands import expand_slashes
from app.search.evaluate import evaluate
from app.search.query import parse_query
from app.storage.sqlite_store import SqliteStore
from tests.fixtures.evaluation import CORPUS, QUESTIONS, load_into

#: What a competent model should emit for each constrained sentence, using only
#: operators the parser has. **Hand-written on purpose**: this measures the
#: ceiling translation could reach, not what any particular model produces.
IDEAL_TRANSLATIONS = {
    "the email from Chris about buying a licence": "buying licence from:chris",
    "what Dave asked about the licence": "licence from:dave",
    "Priya's approval to go ahead": "approval from:priya",
    "the safety report Dave sent me": "safety report from:dave type:email",
    "the licence quote from the last few months": "licence quote after:180d",
    "the audit dates we were sent about six months ago": "audit dates after:1y",
    "the holiday rota from the last couple of months": "holiday rota after:90d",
    "the spreadsheet with the audit findings": "audit findings type:xlsx",
    "the draft version of the Leeds safety report": "draft leeds safety report type:docx",
    "the slide deck about site rules": "site rules type:pptx",
    "what did I send to Priya": "to:priya",
    "emails with something attached about the licence": "licence has:attachment",
}


@pytest.fixture(scope="module")
def searcher():
    folder = pathlib.Path(tempfile.mkdtemp())
    with SqliteStore(folder / "eval.db") as store:
        load_into(store)

        def search(query: str) -> list[str]:
            parsed = parse_query(expand_slashes(query))
            return [hit["path"] for hit in keyword.search(store, parsed, limit=10)]

        yield search


def test_every_question_has_an_answer_in_the_corpus():
    """A question whose document is absent scores zero and looks like a
    retrieval failure. Two of them were - "what did I send to Priya" pointed at
    a message Priya *sent*, and a date question described a document five months
    old as "over a year ago". Both were benchmark bugs that read as search bugs.
    """
    paths = [doc.path.lower() for doc in CORPUS]
    for question in QUESTIONS:
        assert any(question.expects.lower() in path for path in paths), (
            f"{question.expects!r} is not in the corpus, so "
            f"{question.sentence!r} can never be satisfied"
        )


def test_a_plain_sentence_returns_something(searcher):
    """The floor. Nineteen of twenty once returned nothing at all, which is a
    different and much worse failure than ranking badly."""
    report = evaluate(QUESTIONS, searcher, k=10)
    empty = [o for o in report.outcomes if o.returned == 0]
    assert not empty, (
        f"{len(empty)} sentences returned no results at all: "
        f"{[o.question.sentence for o in empty[:3]]}"
    )


def test_topic_questions_mostly_find_their_document(searcher):
    """Keyword only, no embeddings. The bar is deliberately modest - the vector
    half is what should carry paraphrases like "when it expires"."""
    report = evaluate(QUESTIONS, searcher, k=10)
    assert report.topic_recall >= 0.6


def test_translation_is_what_makes_constraints_work(searcher):
    """**The finding that justifies Layer 8a**, and the work order predicted it:
    topic matching decent, constraints ignored.

    A plain sentence cannot honour "from Chris" - the words go into the text
    search and the sender field is never consulted. Translated into
    `from:chris`, it is a filter.
    """
    plain = evaluate(QUESTIONS, searcher, k=1)
    translated = evaluate(
        QUESTIONS, searcher, k=1,
        translate=lambda sentence: IDEAL_TRANSLATIONS.get(sentence, sentence),
    )

    assert translated.constrained_recall > plain.constrained_recall + 0.3, (
        f"translation moved constrained recall from {plain.constrained_recall:.0%} "
        f"to {translated.constrained_recall:.0%}; if that gap closes, either "
        f"search improved or the benchmark stopped testing anything"
    )


@pytest.mark.parametrize("kind", ["sender", "type", "recipient", "attachment"])
def test_each_kind_of_constraint_is_honoured_once_translated(kind, searcher):
    """Split by kind, because they fail for different reasons and are worth
    different amounts to fix. Averaging them hides which one is broken."""
    translated = evaluate(
        QUESTIONS, searcher, k=3,
        translate=lambda sentence: IDEAL_TRANSLATIONS.get(sentence, sentence),
    )
    assert translated.by_constraint().get(kind, 0) >= 0.6


def test_stopwords_do_not_exclude_everything():
    """The bug, pinned directly. One absent "of" must not empty a result set."""
    expression = parse_query("drawings of the pump station").fts_match()
    assert " \"of\" " not in expression and "\"the\"" not in expression
    assert "drawings" in expression and "station" in expression


def test_more_than_one_word_does_not_require_every_word():
    """Anything above one word is a description, and demanding every word of a
    description is how a search box earns a reputation for finding nothing.

    The threshold was three at first, on the reasoning that a short query is a
    keyword search. Measured, that left six of twenty sentences returning
    nothing; at one, none do.
    """
    assert " OR " in parse_query("pump valve leeds").fts_match()
    assert " OR " in parse_query("the email about buying a licence").fts_match()


def test_precision_is_still_available_to_anybody_who_wants_it():
    """Loosening the default is only safe because the strict forms still exist
    and still mean exactly what they say."""
    assert '"site" + "survey"' in parse_query('"site survey"').fts_match()
    assert " AND " in parse_query("pump AND valve").fts_match()
    assert "NOT" in parse_query("pump -draft").fts_match()
