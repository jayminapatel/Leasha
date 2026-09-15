"""Layer: L1 / L3 - order 0b `docs/WORKORDER-202626270114-index-tuning.md` §6c.

**numpy/pyarrow end-to-end, no per-float boxing on the hot path.**

Before this item, `Embedder.embed()` returned `list[list[float]]` - each
component widened from the model's native float32 to a Python `float`
(float64) via `.tolist()` - and `VectorStore.add` turned that straight back
into a `list[dict]`, with a fresh `[float(x) for x in vector]` per chunk. A
batch of 256 chunks at 384 dimensions boxed and reboxed roughly 98,000 floats
for a column LanceDB's own schema already declares `float32`
(`ensure_table`'s `pa.list_(pa.float32(), self.dim)`). `_table.add(rows)` then
converted that list of dicts to Arrow *again*, internally, to match the
schema - the hidden second conversion the order's own 2026-08-27 investigation
note named as "where the real cost and the real fix both are", and which was
left open at the time because fixing it needed `app/storage/vector_store.py`,
outside that thread's file scope.

Two things this file pins down, matching the item's own wording exactly:

* the embedder returns float32 arrays (`np.ndarray`, not `list[list[float]]`);
* `vector_store.add` builds one Arrow table per batch and hands *that* to
  LanceDB, never a list of per-chunk dicts for LanceDB to convert a second
  time - checked directly, not inferred from a timing.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.errors import AppErrorException
from app.index.embedder import Embedder
from app.storage.vector_store import VectorStore

pytest.importorskip("lancedb")
pytest.importorskip("pyarrow")

DIM = 384


def encoder_for(dim: int = DIM):
    """A deterministic fake encoder - no model on disk, no download."""
    def encode(texts):
        vectors = []
        for index, _text in enumerate(texts):
            rng = np.random.default_rng(index + 1)
            vectors.append(rng.random(dim, dtype=np.float32))
        return vectors
    return encode


# --- the embedder's half: returns float32 arrays -----------------------


def test_embed_returns_a_float32_numpy_block() -> None:
    """§6c: 'embedder returns float32 arrays' - the literal acceptance text."""
    embedder = Embedder(encoder=encoder_for())
    vectors = embedder.embed(["a", "b", "c"])
    assert isinstance(vectors, np.ndarray), \
        f"expected an ndarray, got {type(vectors)}"
    assert vectors.dtype == np.float32
    assert vectors.shape == (3, DIM)


def test_embed_all_yields_float32_rows_lazily() -> None:
    """`embed_all` still yields one row at a time - see `embed_bench.py`'s own
    reasoning: materialising a million vectors before the first store write
    would be several GB of list. A numpy block changes the container, not the
    laziness."""
    embedder = Embedder(encoder=encoder_for(), batch_size=2)
    stream = embedder.embed_all(["a", "b", "c", "d", "e"])
    first = next(stream)
    assert isinstance(first, np.ndarray) and first.dtype == np.float32
    assert first.shape == (DIM,)

    rest = list(stream)
    assert len(rest) == 4
    assert all(isinstance(r, np.ndarray) and r.dtype == np.float32 for r in rest)


def test_empty_input_is_still_a_plain_empty_list() -> None:
    """Unaffected by §6c: a file with no chunks must not touch the model or
    build a numpy block to say so."""
    embedder = Embedder()
    assert embedder.embed([]) == []
    assert not embedder.loaded


# --- the store's half: one Arrow table per batch, not a list of dicts ---


@pytest.fixture
def store(tmp_path):
    with VectorStore(tmp_path / "vectors", dim=DIM) as opened:
        yield opened


def test_add_hands_lancedb_one_arrow_table_not_a_list_of_dicts(store, monkeypatch) -> None:
    """The acceptance criterion, checked directly rather than inferred from a
    timing: `vector_store.add` builds the Arrow table itself and calls
    `_table.add()` with it once per batch - never a `list[dict]` for LanceDB
    to convert a second time."""
    import pyarrow as pa

    store.ensure_table()
    seen: list = []
    original_add = store._table.add

    def spy(payload, *args, **kwargs):
        seen.append(payload)
        return original_add(payload, *args, **kwargs)

    monkeypatch.setattr(store._table, "add", spy)

    rng = np.random.default_rng(3)
    vectors = rng.random((5, DIM), dtype=np.float32)
    store.add(chunk_ids=list(range(5)), file_ids=[1] * 5, vectors=vectors)

    assert len(seen) == 1, "one add() call, one table, per batch"
    payload = seen[0]
    assert isinstance(payload, pa.Table), (
        f"must hand LanceDB a pyarrow.Table directly, got {type(payload)} - "
        "a list would mean LanceDB is still converting it a second time"
    )
    assert payload.num_rows == 5
    assert payload.schema.field("vector").type.list_size == DIM


def test_values_survive_the_arrow_round_trip_to_float32_precision(store) -> None:
    rng = np.random.default_rng(7)
    vectors = rng.random((4, DIM), dtype=np.float32)
    store.add(chunk_ids=[1, 2, 3, 4], file_ids=[1, 1, 1, 1], vectors=vectors)

    for chunk_id, expected in zip([1, 2, 3, 4], vectors):
        got = store.vector_for(chunk_id)
        assert got is not None
        np.testing.assert_allclose(got, expected, atol=1e-6)


def test_add_still_accepts_a_list_of_lists(store) -> None:
    """Backward compatible: the image lane (`clip_embedder.py`), `cli.py`'s
    reembed path and every hand-written test pass plain `list[list[float]]`,
    never a numpy block - §6c must not narrow what `add()` accepts."""
    vectors = [[0.1] * DIM, [0.2] * DIM]
    written = store.add(chunk_ids=[1, 2], file_ids=[1, 1], vectors=vectors)
    assert written == 2
    assert store.count() == 2


def test_add_still_accepts_a_list_of_1d_numpy_rows(store) -> None:
    """What `Pipeline._embed_texts` actually hands `add()` after §6e dedup
    re-indexes embedded rows: a Python list whose elements are the 1D numpy
    rows `embed_all` yields, not one stacked 2D block."""
    rng = np.random.default_rng(9)
    rows = [rng.random(DIM, dtype=np.float32) for _ in range(3)]
    written = store.add(chunk_ids=[1, 2, 3], file_ids=[1, 1, 1], vectors=rows)
    assert written == 3
    assert store.count() == 3


def test_wrong_dimension_is_still_refused_before_any_write(store) -> None:
    """§6c changes how the batch is built, not what it validates - a
    mismatched vector must still be refused rather than corrupt the table."""
    with pytest.raises(AppErrorException) as caught:
        store.add(chunk_ids=[1], file_ids=[1], vectors=[[0.1] * (DIM - 1)])
    assert "dimension" in (caught.value.error.details or "").lower()
    assert store.count() == 0


def test_mismatched_batch_lengths_are_still_refused(store) -> None:
    with pytest.raises(AppErrorException):
        store.add(chunk_ids=[1, 2], file_ids=[1],
                   vectors=[[0.1] * DIM, [0.2] * DIM])


def test_end_to_end_embedder_into_store(store) -> None:
    """The whole point of §6c in one line: what the embedder now returns is
    exactly what the store now accepts, with nothing boxed in between."""
    embedder = Embedder(encoder=encoder_for(), batch_size=64)
    texts = [f"chunk {i}" for i in range(10)]
    vectors = list(embedder.embed_all(texts))

    written = store.add(
        chunk_ids=list(range(10)), file_ids=[1] * 10, vectors=vectors,
    )
    assert written == 10
    assert store.count() == 10
