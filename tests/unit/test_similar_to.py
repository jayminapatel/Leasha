r"""More like this — the vector half, finally made pointable.

Layer: L4. **Everything this needs already existed**: the passage was embedded
at index time, `VectorStore.search` takes a raw vector, and `vector.hydrate`
turns rows into results. What was missing was a way to *ask*, so semantic
search has been in the product since Layer 4 and has never once been something
a person could point at.

Runs against a real LanceDB table with four-dimensional vectors, so the
neighbour arithmetic is the real one and no embedding model is needed.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest


class _NoModel:
    def embed(self, _text):
        raise RuntimeError("similar_to must never need the model")

    def embed_all(self, _texts):
        raise RuntimeError("similar_to must never need the model")


@pytest.fixture()
def built():
    """Four documents, one of them in two parts, with hand-placed vectors."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    root = pathlib.Path(tempfile.mkdtemp())
    store = SqliteStore(root / "index.db").connect()
    vectors = VectorStore(root / "vectors", dim=4).connect()
    vectors.ensure_table()

    def put(name, chunks, points):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext="txt", size_bytes=1,
            mtime_ns=1, status="INDEXED", source_kind="file")
        chunk_ids = store.replace_chunks(file_id, [
            {"ordinal": n, "text": text} for n, text in enumerate(chunks)])
        vectors.add(chunk_ids, [file_id] * len(chunk_ids), points)
        return file_id, chunk_ids

    file_a, chunks_a = put(
        "safety-a.txt",
        ["Safety report for the Leeds site",
         "Continued: the same Leeds report, page two"],
        [[1.0, 0.0, 0.0, 0.0], [0.99, 0.01, 0.0, 0.0]])
    put("safety-b.txt", ["Site safety findings and actions"],
        [[0.95, 0.05, 0.0, 0.0]])
    put("safety-c.txt", ["Health and safety induction notes"],
        [[0.90, 0.10, 0.0, 0.0]])
    put("pumps.txt", ["Pump station commissioning results"],
        [[0.0, 1.0, 0.0, 0.0]])

    engine = SearchEngine(store, vectors, _NoModel())
    yield engine, file_a, chunks_a
    engine.close()


def _names(response):
    return [result.path.rsplit("/", 1)[-1] for result in response.results]


def test_the_nearest_passages_come_back_nearest_first(built):
    engine, _file_a, chunks_a = built
    assert _names(engine.similar_to(chunks_a[0])) == [
        "safety-b.txt", "safety-c.txt", "pumps.txt"]


def test_a_passage_is_never_its_own_neighbour(built):
    """It is trivially the closest thing to itself, and saying so is not an
    answer."""
    engine, _file_a, chunks_a = built
    response = engine.similar_to(chunks_a[0])
    assert all(result.chunk_id != chunks_a[0] for result in response.results)


def test_the_rest_of_its_own_file_is_dropped_by_default(built):
    r"""**"More like this" answering with the next paragraph of the same
    document is a correct answer to a question nobody asked.**"""
    engine, _file_a, chunks_a = built
    assert "safety-a.txt" not in _names(engine.similar_to(chunks_a[0]))


def test_and_can_be_asked_for(built):
    engine, _file_a, chunks_a = built
    found = _names(engine.similar_to(chunks_a[0], same_file=True))
    assert found[0] == "safety-a.txt"


def test_the_same_file_exclusion_actually_works(built):
    r"""**The bug this pins.** The first version looked the source chunk up
    with `store.chunk_by_id`, a method this store has never had, inside a
    `try` - so the lookup failed on every call, the file id stayed 0, and the
    exclusion above quietly did nothing at all. A guard that turns a typo into
    a silently disabled feature is the defect this codebase keeps finding.
    """
    import app.search.engine as engine_module
    from app.storage.sqlite_store import SqliteStore

    source = pathlib.Path(engine_module.__file__ or "").read_text(encoding="utf-8")
    # The call shape, not the bare word - the comment above the fix names the
    # method it replaced, and a test that cannot tell an explanation from a
    # call is a test that forbids explaining anything.
    assert "store.chunk_by_id(" not in source
    assert "store.get_chunk(" in source
    assert hasattr(SqliteStore, "get_chunk")


def test_a_passage_with_no_vector_is_an_empty_answer_not_a_failure(built):
    """The ordinary state of a run that indexed text and has not embedded it
    yet."""
    engine, _file_a, _chunks_a = built
    response = engine.similar_to(999_999)
    assert response.results == [] and response.vector_count == 0


def test_the_model_is_never_loaded(built):
    """**Read back rather than re-embedded.** Embedding the passage again
    costs a model load and - if the model changed since indexing - gives a
    vector that does not live in the same space as the rows it is compared
    against. `_NoModel` raises if anything reaches for it."""
    engine, _file_a, chunks_a = built
    assert engine.similar_to(chunks_a[0]).results


def test_a_closed_engine_refuses(built):
    """Same guard as `search`: nothing below it is safe once the executor is
    gone."""
    from app.core.errors import AppErrorException

    engine, _file_a, chunks_a = built
    engine.close()
    with pytest.raises(AppErrorException):
        engine.similar_to(chunks_a[0])


def test_the_limit_is_honoured_after_the_exclusions(built):
    """Over-fetched on purpose: applying the limit before dropping the
    source's own siblings would return short."""
    engine, _file_a, chunks_a = built
    assert len(engine.similar_to(chunks_a[0], limit=2).results) == 2


def test_a_vector_store_that_cannot_read_back_is_survivable():
    """An older store, or one still being written. No neighbours, no crash."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "i.db").connect()

    class _Old:
        def search(self, *_args, **_kwargs):
            return []

    engine = SearchEngine(store, _Old(), _NoModel())
    try:
        assert engine.similar_to(1).results == []
    finally:
        engine.close()
