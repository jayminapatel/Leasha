r"""A re-read passage whose words did not change keeps its row and its vector.

Layer: L1 (the store) and L3 (the pipeline, over a real LanceDB). 2026-10-10.

**Why.** Measured on the owner's laptop, the meaning step is about 97% of a run,
at 5-13 passages a second on the processor. `replace_chunks` deleted every
passage of a re-read file and inserted them all again with new ids and
`embedded = 0`, so a `.docx` saved again with the same words, a forced "Read
again", or a date that moved alone sent every passage back to the model for
the vector it already had - identical text gives an identical vector.

What must still be true, and is pinned here:

* the file's passages are exactly the ones handed in - old ones replaced, never
  appended - in their new order, with `UNIQUE(file_id, ordinal)` respected when
  a passage is inserted in the middle and the ordinals after it shift;
* the keyword index agrees with the rows (`integrity-check`), inside a batch
  and outside one;
* only the new passages reach the model; a file with nothing new is INDEXED at
  once; a kept passage keeps its vector in LanceDB and a gone one loses it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.storage.vector_store import VectorStore

DIM = 8


def _file(store: SqliteStore, path: str = r"D:\docs\report.txt") -> int:
    return store.upsert_file(path, size_bytes=10, mtime_ns=1, source_kind="file")


def _rows(store: SqliteStore, file_id: int) -> list[tuple]:
    return [tuple(row) for row in store.conn.execute(
        "SELECT id, ordinal, text, embedded FROM chunks WHERE file_id = ? ORDER BY ordinal",
        (file_id,))]


def _fts_agrees(store: SqliteStore) -> None:
    """FTS5's own check of an external-content index against its table."""
    store.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('integrity-check')")
    rows = store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    indexed = store.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    assert rows == indexed


# -- the store ---------------------------------------------------------------

def test_the_same_words_keep_their_rows_ids_and_embedded_flags(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = _file(store)
        passages = [{"text": f"paragraph {n} about the pump station"} for n in range(4)]
        first = store.replace_chunks(file_id, passages)
        store.mark_embedded(first)

        again = store.replace_chunks(file_id, [dict(p) for p in passages])

        assert list(again) == list(first)
        assert again.to_embed == [] and again.removed == []
        assert all(embedded == 1 for *_x, embedded in _rows(store, file_id))
        _fts_agrees(store)
        assert len(store.search_bm25("pump")) == 4


def test_a_passage_inserted_in_the_middle_shifts_the_ordinals_after_it(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = _file(store)
        old = store.replace_chunks(file_id, [{"text": t} for t in ("alpha", "bravo", "charlie")])
        store.mark_embedded(old)

        new = store.replace_chunks(
            file_id, [{"text": t} for t in ("alpha", "INSERTED", "bravo", "charlie")])

        rows = _rows(store, file_id)
        assert [text for _i, _o, text, _e in rows] == ["alpha", "INSERTED", "bravo", "charlie"]
        assert [ordinal for _i, ordinal, _t, _e in rows] == [0, 1, 2, 3]
        assert [new[0], new[2], new[3]] == list(old), "a kept passage changed its id"
        assert new.to_embed == [new[1]] and new.removed == []
        assert [e for *_x, e in rows] == [1, 0, 1, 1]
        _fts_agrees(store)
        assert store.search_bm25("inserted")


def test_gone_passages_are_deleted_and_named(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = _file(store)
        old = store.replace_chunks(file_id, [{"text": t} for t in ("alpha", "bravo", "charlie")])
        store.mark_embedded(old)

        new = store.replace_chunks(file_id, [{"text": t} for t in ("charlie", "delta")])

        assert new.removed == [old[0], old[1]]
        assert new[0] == old[2] and new.to_embed == [new[1]]
        assert [(o, t) for _i, o, t, _e in _rows(store, file_id)] == [(0, "charlie"), (1, "delta")]
        assert not store.search_bm25("bravo"), "the keyword index kept a gone passage"
        _fts_agrees(store)


def test_repeated_passages_pair_one_to_one(tmp_path: Path) -> None:
    """Two identical signatures must not both claim the first old row."""
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = _file(store)
        old = store.replace_chunks(file_id, [{"text": t} for t in ("sig", "body", "sig")])
        new = store.replace_chunks(file_id, [{"text": t} for t in ("sig", "sig", "sig")])
        assert new[0] == old[0] and new[1] == old[2]
        assert new[2] not in old and new.removed == [old[1]]
        assert len(set(new)) == 3
        _fts_agrees(store)


def test_a_moved_page_or_label_is_rewritten_and_the_vector_kept(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = _file(store)
        old = store.replace_chunks(file_id, [{"text": "total 42", "page": 1, "label": "Q3!A1",
                                              "char_start": 0, "char_end": 8}])
        store.mark_embedded(old)
        new = store.replace_chunks(file_id, [{"text": "total 42", "page": 2, "label": "Q4!B7",
                                              "char_start": 10, "char_end": 18}])
        row = store.conn.execute("SELECT * FROM chunks WHERE id = ?", (new[0],)).fetchone()
        assert new[0] == old[0] and new.to_embed == []
        assert (row["page"], row["label"], row["char_start"], row["char_end"], row["embedded"]) \
            == (2, "Q4!B7", 10, 18, 1)
        _fts_agrees(store)


def test_a_kept_passage_without_a_vector_is_still_to_be_embedded(tmp_path: Path) -> None:
    """Still waiting from an earlier run (0) or keyword-only until now (2):
    kept, and queued, because it has no vector to keep."""
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = _file(store)
        old = store.replace_chunks(file_id, [{"text": "waiting"}, {"text": "sheet"}])
        store.mark_keyword_only([old[1]])
        new = store.replace_chunks(file_id, [{"text": "waiting"}, {"text": "sheet"}])
        assert list(new) == list(old) and new.to_embed == list(old)


def test_inside_a_batch_the_deferred_index_still_agrees(tmp_path: Path) -> None:
    """A new file's rows wait for the end of the batch (`_deferred`); the same
    file written again in that batch, and an old file changed in it, take the
    trigger path. Whatever the mix, the index matches the rows."""
    with SqliteStore(tmp_path / "k.db") as store:
        settled = _file(store, r"D:\docs\old.txt")
        store.mark_embedded(store.replace_chunks(settled, [{"text": "kept words"},
                                                           {"text": "old words"}]))
        with store.batch():
            fresh = _file(store, r"D:\docs\new.txt")
            first = store.replace_chunks(fresh, [{"text": "fresh one"}, {"text": "fresh two"}])
            second = store.replace_chunks(fresh, [{"text": "fresh two"}, {"text": "fresh three"}])
            changed = store.replace_chunks(settled, [{"text": "new words"}, {"text": "kept words"}])
        assert second[0] == first[1] and second.removed == [first[0]]
        assert changed[1] != changed[0] and changed.to_embed == [changed[0]]
        _fts_agrees(store)
        assert store.search_bm25("three") and not store.search_bm25('"fresh one"')
        assert store.search_bm25("kept") and not store.search_bm25('"old words"')


def test_a_new_file_in_a_batch_still_defers_its_keyword_rows(tmp_path: Path) -> None:
    """The 2026-09-30 saving is kept: a never-read file's rows wait."""
    import app.storage.sqlite_store as store_module

    if store_module._TRIGGER_SWITCH is None:                    # noqa: SLF001
        pytest.skip("this Python cannot switch a connection's triggers")
    with SqliteStore(tmp_path / "k.db") as store:
        with store.batch():
            file_id = _file(store)
            store.replace_chunks(file_id, [{"text": "waiting words"}])
            state = store._deferred()                            # noqa: SLF001
            assert state is not None and state.chunks and file_id in state.chunk_files
        _fts_agrees(store)


# -- the vector store ---------------------------------------------------------

def test_the_vector_delete_spares_the_kept_chunks(tmp_path: Path) -> None:
    with VectorStore(tmp_path / "vectors", dim=DIM) as vectors:
        vectors.ensure_table()
        vectors.add(chunk_ids=[1, 2, 3, 4], file_ids=[7, 7, 7, 8],
                    vectors=[[1.0] + [0.0] * (DIM - 1)] * 4)
        vectors.delete_by_file_ids_except([7], [2])
        ids = sorted(int(r) for r in vectors._table.to_arrow()["chunk_id"].to_pylist())  # noqa: SLF001
        assert ids == [2, 4]
        vectors.delete_by_file_ids_except([8], [])
        ids = sorted(int(r) for r in vectors._table.to_arrow()["chunk_id"].to_pylist())  # noqa: SLF001
        assert ids == [2]


# -- the pipeline, end to end over a real LanceDB ----------------------------

class _Counting:
    """An embedder that counts what it was asked for. Deterministic vectors."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def make(self) -> Embedder:
        def encode(texts):
            self.texts.extend(texts)
            out = []
            for text in texts:
                seed = sum(ord(ch) for ch in text) or 1
                raw = [((seed * (i + 3)) % 17 + 1) / 17 for i in range(DIM)]
                norm = sum(v * v for v in raw) ** 0.5
                out.append([v / norm for v in raw])
            return out
        return Embedder(dim=DIM, encoder=encode)


def _paragraphs(n: int, words: int = 420) -> str:
    """One paragraph long enough to be a passage of its own (~512 tokens)."""
    return " ".join(f"word{n}x{k}" for k in range(words)) + "."


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))


def _run(store, vectors, root, counting, **config):
    pipeline = Pipeline(store, vectors, counting.make(),
                        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, **config))
    return pipeline.run()


def _vector_ids(vectors: VectorStore) -> set[int]:
    if vectors._table is None:                                    # noqa: SLF001
        return set()
    return {int(i) for i in vectors._table.to_arrow()["chunk_id"].to_pylist()}  # noqa: SLF001


def _chunk_ids(store: SqliteStore) -> set[int]:
    return {int(r[0]) for r in store.conn.execute("SELECT id FROM chunks")}


def test_a_forced_re_read_of_the_same_words_embeds_nothing(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    for n in range(20):
        _write(root / f"doc{n}.txt", "\n\n".join(_paragraphs(n * 10 + k) for k in range(3)))
    counting = _Counting()
    with SqliteStore(tmp_path / "index.db") as store, \
            VectorStore(tmp_path / "vectors", dim=DIM) as vectors:
        _run(store, vectors, root, counting)
        first = len(counting.texts)
        assert first >= 20
        before = _vector_ids(vectors)
        assert before == _chunk_ids(store)

        counting.texts.clear()
        stats = _run(store, vectors, root, counting, force=True)

        assert stats.indexed == 20, "the forced run did not read the files again"
        assert counting.texts == [], "unchanged passages went back to the model"
        assert _vector_ids(vectors) == before == _chunk_ids(store)
        statuses = {r[0] for r in store.conn.execute(
            "SELECT status FROM files WHERE source_kind = 'file'")}
        assert statuses == {FileStatus.INDEXED}


def test_one_paragraph_edited_re_embeds_only_what_changed(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    paragraphs = [_paragraphs(k) for k in range(6)]
    target = root / "long.txt"
    _write(target, "\n\n".join(paragraphs))
    counting = _Counting()
    with SqliteStore(tmp_path / "index.db") as store, \
            VectorStore(tmp_path / "vectors", dim=DIM) as vectors:
        _run(store, vectors, root, counting)
        passages = len(counting.texts)
        assert passages >= 4, "the document did not chunk into several passages"
        before = _chunk_ids(store)

        # The last paragraph edited: everything before it is the same words.
        paragraphs[-1] = paragraphs[-1].replace("word5x7 ", "EDITED ")
        _write(target, "\n\n".join(paragraphs))
        counting.texts.clear()
        _run(store, vectors, root, counting)

        sent = len(counting.texts)
        assert 0 < sent < passages, (sent, passages)
        assert any("EDITED" in text for text in counting.texts)
        after = _chunk_ids(store)
        assert len(before & after) == passages - sent, "kept passages lost their rows"
        assert _vector_ids(vectors) == after, "a kept vector was lost or a gone one left"
        status = store.conn.execute("SELECT status FROM files").fetchone()[0]
        assert status == FileStatus.INDEXED


def test_a_paragraph_deleted_with_nothing_new_takes_only_its_vector(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    paragraphs = [_paragraphs(k) for k in range(5)]
    target = root / "long.txt"
    _write(target, "\n\n".join(paragraphs))
    counting = _Counting()
    with SqliteStore(tmp_path / "index.db") as store, \
            VectorStore(tmp_path / "vectors", dim=DIM) as vectors:
        _run(store, vectors, root, counting)
        before = _chunk_ids(store)
        # Deleting the last paragraph may leave the passages before it as they
        # were - then nothing is new - or re-cut the last one. Either way the
        # vectors must be exactly the passages that remain.
        _write(target, "\n\n".join(paragraphs[:-1]))
        counting.texts.clear()
        _run(store, vectors, root, counting)
        after = _chunk_ids(store)
        assert after < before or counting.texts, "nothing changed at all"
        assert _vector_ids(vectors) == after
        status = store.conn.execute("SELECT status FROM files").fetchone()[0]
        assert status == FileStatus.INDEXED


def test_the_start_of_run_repair_keeps_the_vectors_a_file_already_has(tmp_path: Path) -> None:
    """The delete before the add used to be every vector of the file. A file
    with some passages embedded and some not - two vectors lost, say - had
    the two refilled by the run's start (`_drain_unembedded`), and the
    whole-file delete before that add took the other passages' vectors with
    it while their rows still said `embedded = 1`. Now it spares them."""
    root = tmp_path / "docs"
    root.mkdir()
    _write(root / "long.txt", "\n\n".join(_paragraphs(k) for k in range(6)))
    counting = _Counting()
    with SqliteStore(tmp_path / "index.db") as store, \
            VectorStore(tmp_path / "vectors", dim=DIM) as vectors:
        _run(store, vectors, root, counting)
        ids = sorted(_chunk_ids(store))
        assert len(ids) >= 4
        lost = ids[:2]
        vectors.delete_by_chunk_ids(lost)
        with store.write() as conn:
            conn.executemany("UPDATE chunks SET embedded = 0 WHERE id = ?",
                             [(i,) for i in lost])
        counting.texts.clear()
        _run(store, vectors, root, counting)
        assert len(counting.texts) == 2, "the repair did not refill the two"
        assert _vector_ids(vectors) == set(ids), "the repair cost the file its other vectors"
