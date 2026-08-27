r"""Where is this defined, rather than where is it used.

Layer: L4. §4c said measure before accepting a ranking change, so:

    query "SearchEngine", five files, one of them declaring it

    1. app/cli.py            uses it twice
    2. app/ui/shell.py       uses it three times
    3. tests/test_engine.py  uses it twice
    4. docs/DESIGN.md        mentions it twice, in prose
    5. app/search/engine.py  DECLARES it            <- last

BM25 rewards term frequency and a declaration appears **once**, so the only
file that answers the question ranked below a Markdown file that talked about
it. With the boost it is first.

    weight   rank of the declaring file
    0.00     5 of 5
    0.02     4 of 5
    0.05     2 of 5
    0.10     1 of 5
    0.20     1 of 5
    0.40     1 of 5   <- shipped
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search.definitions import WEIGHT, boost, declares, looks_like_symbol


# --------------------------------------------------------------------------
# Recognising a declaration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "class SearchEngine:",
    "struct SearchEngine {",
    "record SearchEngine(int x)",
    "interface SearchEngine {",
    "trait SearchEngine {",
    "enum SearchEngine {",
    "def SearchEngine(store):",
    "func SearchEngine() error {",
    "fn SearchEngine() -> Self {",
    "function SearchEngine(store) {",
    "const SearchEngine = (store) => {",
    "let SearchEngine = 5",
    "SearchEngine(store, vectors) {",
    "SearchEngine := newThing()",
])
def test_the_shapes_a_declaration_takes(line):
    """One regex family for the C family, Python, Java, C#, Go, Rust and
    TypeScript. **Deliberately approximate** - a real answer needs a parser
    per language, and twelve parsers or nothing is not the trade a search box
    should make."""
    assert declares(line, "SearchEngine")


@pytest.mark.parametrize("line", [
    "engine = SearchEngine(store, vectors, embedder)",
    "from app.search.engine import SearchEngine",
    "The SearchEngine is the heart of Layer 4.",
    "return self.SearchEngine",
    "# SearchEngine does the fusing",
])
def test_using_a_name_is_not_declaring_it(line):
    assert not declares(line, "SearchEngine")


def test_a_declaration_of_something_else_does_not_count():
    assert not declares("class SearchEngineFactory:", "SearchEngine")
    assert not declares("class OtherThing:", "SearchEngine")


def test_nothing_to_look_for_is_not_an_error():
    assert not declares("", "Thing")
    assert not declares("class Thing:", "")
    assert not declares(None, None)


def test_a_symbol_with_regex_characters_in_it_is_escaped():
    """A name is user input. `a.b*` compiled raw is either a wrong match or a
    crash."""
    assert not declares("class ab:", "a.b")
    assert declares("class a_b:", "a_b")


# --------------------------------------------------------------------------
# When it fires at all
# --------------------------------------------------------------------------

def test_one_symbol_shaped_word_and_nothing_else():
    """**Exactly one term.** Two words is a description, and boosting a
    declaration of the first of them answers a question nobody asked. This is
    what keeps ordinary searching free of it."""
    assert looks_like_symbol(("SearchEngine",)) == "SearchEngine"
    assert looks_like_symbol(("search", "engine")) is None
    assert looks_like_symbol(()) is None


def test_a_short_word_is_not_a_symbol():
    assert looks_like_symbol(("id",)) is None
    assert looks_like_symbol(("of",)) is None


def test_an_ordinary_english_word_reaches_it_and_costs_nothing():
    """"volcano" is identifier-shaped, so it gets this far - and no
    declaration pattern can match in prose, which makes the boost a no-op
    rather than a wrong answer. Cheaper than trying to tell code from English
    in a search box."""
    assert looks_like_symbol(("volcano",)) == "volcano"
    assert not declares("My homework about volcanoes and lava", "volcano")


# --------------------------------------------------------------------------
# The reordering
# --------------------------------------------------------------------------

def _hit(name, score, text):
    return {"path": name, "rrf_score": score, "text": text}


def test_the_declaration_rises_above_the_uses():
    hits = [_hit("uses.py", 0.0164, "Thing(); Thing(); Thing()"),
            _hit("real.py", 0.0153, "class Thing:")]
    assert [h["path"] for h in boost(hits, "Thing")] == ["real.py", "uses.py"]


def test_order_within_each_group_is_preserved():
    r"""**The boost partitions the list; it does not shuffle it.**

    Everything that declares the symbol keeps its relative order, and so does
    everything that does not. That is what stops a false positive - a comment
    the pattern matched - from ever outranking a true declaration that scored
    higher.
    """
    hits = [_hit("a.py", 0.0164, "class Thing:"),
            _hit("b.py", 0.0160, "Thing()"),
            _hit("c.py", 0.0150, "class Thing {"),
            _hit("d.py", 0.0140, "Thing()")]
    assert [h["path"] for h in boost(hits, "Thing")] == [
        "a.py", "c.py", "b.py", "d.py"]


def test_a_corpus_where_nothing_declares_it_is_untouched():
    hits = [_hit("a.py", 0.0164, "Thing()"), _hit("b.py", 0.0150, "Thing()")]
    assert [h["path"] for h in boost(hits, "Thing")] == ["a.py", "b.py"]


def test_the_reason_is_left_on_the_hit():
    hits = [_hit("a.py", 0.01, "class Thing:")]
    assert boost(hits, "Thing")[0]["declares"] is True


def test_weight_zero_is_the_identity():
    hits = [_hit("uses.py", 0.0164, "Thing()"),
            _hit("real.py", 0.0153, "class Thing:")]
    assert [h["path"] for h in boost(hits, "Thing", weight=0)] == [
        "uses.py", "real.py"]


# --------------------------------------------------------------------------
# Through the engine, and the ordering bug it exposed
# --------------------------------------------------------------------------

_CORPUS = {
    "app/search/engine.py": "class SearchEngine:\n    def __init__(self, store,"
                            " vectors, embedder):\n        self.store = store",
    "app/ui/shell.py": "engine = SearchEngine(store, vectors, embedder)\n"
                       "    self._engine = engine\n    engine.warm_up()",
    "app/cli.py": "from app.search.engine import SearchEngine\n    engine = "
                  "SearchEngine(store, vectors, model)\n    print(engine)",
    "tests/test_engine.py": "def test_engine(): e = SearchEngine(s, v, m)\n"
                            "    SearchEngine(s, v, m).close()",
    "docs/DESIGN.md": "The SearchEngine is the heart of Layer 4. SearchEngine "
                      "fuses both halves.",
}


@pytest.fixture(scope="module")
def engine():
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "code.db").connect()
    for path, text in _CORPUS.items():
        file_id = store.upsert_file(
            f"C:/repo/{path}", parent_dir="C:/repo", ext=path.split(".")[-1],
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


def test_the_file_that_declares_it_comes_first(engine):
    """The measurement, closed: rank 5 of 5 before, rank 1 after."""
    response = engine.search("SearchEngine", use_cache=False)
    assert response.results[0].path.endswith("search/engine.py")


def test_the_boost_runs_after_the_recency_blend(engine):
    r"""**The bug that made this do nothing at all.**

    Placed above `recency.blend`, the reorder was computed and then thrown
    away: `blend` re-sorts from `rrf_score`, which the definition boost
    deliberately does not write to. Two rankers in sequence, and the second
    silently discarded the first - the order they run in *is* the behaviour.

    Asserted by source order rather than by outcome, because an outcome test
    passes for the wrong reason whenever the scores happen to agree.
    """
    import app.search.engine as engine_module

    text = pathlib.Path(engine_module.__file__ or "").read_text(encoding="utf-8")
    assert text.index("recency.blend(fused)") < text.index(
        "definitions.boost(fused, symbol)")


def test_a_sentence_never_reaches_the_boost(engine):
    """Two words is a description. Nothing here should move."""
    response = engine.search("SearchEngine heart", use_cache=False)
    assert response.results[0].path.endswith("DESIGN.md")


def test_the_shipped_weight_still_lifts_it(engine):
    """A measured constant that nothing checks decays into a claim."""
    assert WEIGHT >= 0.10, (
        "0.10 was the threshold at which the declaration reached first place; "
        "below it the measurement in this file's docstring no longer holds")
