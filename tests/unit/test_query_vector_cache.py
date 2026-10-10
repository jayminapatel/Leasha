r"""Work order 1h §5b: a query's vector is embedded once per model and sentence.

Layer: L4

2026-10-10. The query's meaning vector was never cached, so §2b's relax loop
- which re-runs the same words with an `AND` or a filter dropped, leaving
`embed_text` exactly as it was - embedded the sentence again on every pass,
on the text model and on the picture lane's CLIP text tower both. Chat's
widening rounds reach the same engine through `search` and repeat the same
way. `vector.QueryVectorCache` keeps the vectors, keyed by the model and the
sentence.
"""

from __future__ import annotations

import threading

import pytest

from app.search import vector
from app.search.engine import NOTICE_RELAXED, SearchEngine
from app.search.policy import SEARCH, for_surface
from app.search.query import parse_query
from app.search.vector import QueryVectorCache


class _Embedder:
    def __init__(self, model_name: str = "text-model", width: int = 384) -> None:
        self.model_name = model_name
        self.width = width
        self.calls: list[str] = []

    def embed(self, texts):
        self.calls.extend(texts)
        return [[0.1] * self.width for _ in texts]

    def warm_up(self) -> None:
        pass


class _FlakyEmbedder(_Embedder):
    """Fails once, then answers - so a cached failure would show."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def embed(self, texts):
        if not self.failed:
            self.failed = True
            self.calls.extend(texts)
            raise RuntimeError("the model is still loading")
        return super().embed(texts)


class _NoVectors:
    def search(self, *_args, **_kwargs):
        return []


@pytest.fixture()
def store(tmp_path):
    from app.storage.sqlite_store import SqliteStore

    db = SqliteStore(tmp_path / "index.db").connect()
    for name, text in (
        ("essay.txt", "My homework about volcanoes. A volcano erupts when "
                      "magma rises."),
        ("notes.txt", "Shopping list: bread, milk, cheese."),
    ):
        file_id = db.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext="txt",
            size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file",
        )
        db.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
    yield db
    db.close()


def test_the_relax_loop_embeds_a_repeated_query_once(store):
    """`volcano AND bread` finds nothing, and the relaxed `volcano bread`
    is the same sentence to the meaning model - so it is embedded once, on
    the text model and on the picture lane's CLIP text tower alike."""
    from tests.unit.test_search_images import FakeClipTextEmbedder, FakeImageVectorStore

    text_model = _Embedder()
    clip_text = FakeClipTextEmbedder()
    engine = SearchEngine(store, _NoVectors(), text_model,
                          image_vectors=FakeImageVectorStore([]),
                          clip_text_embedder=clip_text, log_usage=False)
    try:
        response = engine.search("volcano AND bread", policy=for_surface(SEARCH),
                                 use_cache=False)
    finally:
        engine.close()

    assert any(n.code == NOTICE_RELAXED for n in response.notices), \
        "the query was not relaxed, so this test proves nothing"
    assert text_model.calls == ["volcano bread"], text_model.calls
    assert clip_text.calls == ["volcano bread"], clip_text.calls


def test_a_repeated_search_past_the_result_cache_embeds_once(store):
    """`use_cache=False` asks for results computed from the index as it is;
    the sentence's vector is the same either way, so it is not embedded
    again."""
    text_model = _Embedder()
    engine = SearchEngine(store, _NoVectors(), text_model, log_usage=False)
    try:
        engine.search("volcano magma", use_cache=False)
        engine.search("volcano   magma", use_cache=False)
    finally:
        engine.close()

    assert text_model.calls == ["volcano magma"]


def test_search_without_a_cache_embeds_every_time():
    """Every caller but the engine passes none, and behaves as it did."""
    text_model = _Embedder()
    for _ in range(2):
        vector.search(_NoVectors(), text_model, parse_query("volcano"))
    assert text_model.calls == ["volcano", "volcano"]


def test_a_failed_embedding_is_not_kept():
    cache = QueryVectorCache()
    text_model = _FlakyEmbedder()
    problems: list[str] = []
    vector.search(_NoVectors(), text_model, parse_query("volcano"),
                  problems=problems, query_vectors=cache)
    assert problems and len(cache) == 0
    vector.search(_NoVectors(), text_model, parse_query("volcano"),
                  query_vectors=cache)
    assert text_model.calls == ["volcano", "volcano"]
    assert len(cache) == 1


def test_two_models_never_share_a_vector():
    """The text model and CLIP's text tower embed into different spaces."""
    cache = QueryVectorCache()
    text_model, clip_text = _Embedder("text-model"), _Embedder("clip-text", 512)
    cache.put(text_model, "a red bicycle", [1.0])
    assert cache.get(clip_text, "a red bicycle") is None
    # Two objects of one model are still two entries: a replaced embedder
    # (a restarted host, a changed setting) starts clean.
    assert cache.get(_Embedder("text-model"), "a red bicycle") is None
    assert cache.get(text_model, "a red bicycle") == [1.0]


def test_whitespace_is_normalised_and_case_is_not():
    cache = QueryVectorCache()
    model = _Embedder()
    cache.put(model, "  Quarterly   report ", [1.0])
    assert cache.get(model, "Quarterly report") == [1.0]
    assert cache.get(model, "quarterly report") is None


def test_the_cache_is_bounded_and_drops_the_oldest():
    cache = QueryVectorCache(limit=2)
    model = _Embedder()
    cache.put(model, "one", [1.0])
    cache.put(model, "two", [2.0])
    cache.get(model, "one")                    # now the most recent
    cache.put(model, "three", [3.0])
    assert len(cache) == 2
    assert cache.get(model, "two") is None
    assert cache.get(model, "one") == [1.0]
    assert cache.get(model, "three") == [3.0]


def test_the_cache_is_safe_across_threads():
    cache = QueryVectorCache(limit=8)
    model = _Embedder()
    errors: list[BaseException] = []

    def hammer(offset: int) -> None:
        try:
            for number in range(2_000):
                text = f"query {(number + offset) % 20}"
                cache.put(model, text, [float(number)])
                cache.get(model, text)
        except BaseException as exc:            # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(cache) <= 8
