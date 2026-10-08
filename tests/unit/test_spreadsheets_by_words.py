r""""Find spreadsheets by meaning" (`INDEX_SPREADSHEET_MEANING`), off by default.

Layer: L3

2026-10-08, the owner. Measured on the owner's index: spreadsheets were 52% of
all 386,665 passages (xlsx 24.6%, xls 14.3%, xlsm 9.9%, csv 3.4%), so over half
of a 15-hour run's embedding went on rows of cells. Off, a spreadsheet's
passages are written and found by their words, marked `embedded = 2`, and
never queued for the model; its file is INDEXED at once. On again, the next
run gives them vectors without reading a single spreadsheet again.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.core.settings_registry import SETTINGS
from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.indexing import index_summary

NO_PACING = ResourceLimits(pause_on_battery=False, cpu_percent=0)


class Vectors:
    def __init__(self) -> None:
        self.rows: dict[int, int] = {}
        self.deleted_for: list[int] = []

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        gone = {int(one) for one in file_ids}
        self.deleted_for.extend(gone)
        self.rows = {cid: fid for cid, fid in self.rows.items() if fid not in gone}

    def add(self, *, chunk_ids, file_ids, vectors, **_kw) -> int:
        chunk_ids, file_ids = list(chunk_ids), list(file_ids)
        for cid, fid in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(cid)] = int(fid)
        return len(chunk_ids)

    def count(self) -> int:
        return len(self.rows)

    def __getattr__(self, name):
        return lambda *args, **kwargs: False


def _corpus(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.txt").write_text("pump station commissioning report",
                                     encoding="utf-8")
    (root / "levels.csv").write_text(
        "tank,level,unit\nT-101,4.2,m\nT-102,3.9,m\n", encoding="utf-8")
    return root


def _run(db: Path, root: Path, vectors: Vectors, *, meaning: bool):
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                limits=NO_PACING, spreadsheet_meaning=meaning)
        embedder = Embedder(dim=4, encoder=lambda texts: [[1.0, 0, 0, 0] for _ in texts])
        stats = Pipeline(store, vectors, embedder, config).run()
        sheet = store.get_file(str(root / "levels.csv"))
        flags = [int(r[0]) for r in store.conn.execute(
            "SELECT embedded FROM chunks WHERE file_id = ?", (sheet.id,))]
        return stats, store.stats(), sheet, flags


def test_a_spreadsheet_is_found_by_its_words_and_never_sent_to_the_model(tmp_path):
    root, vectors = _corpus(tmp_path / "docs"), Vectors()
    _stats, counts, sheet, flags = _run(tmp_path / "i.db", root, vectors, meaning=False)

    assert flags and set(flags) == {SqliteStore.KEYWORD_ONLY}, flags
    assert sheet.status == "INDEXED", "a keyword-only file was left waiting for vectors"
    assert sheet.id not in set(vectors.rows.values()), "the spreadsheet reached the model"
    assert vectors.count() == 1, "the ordinary document lost its vector"
    assert counts["chunks_keyword_only"] == len(flags)

    with SqliteStore(tmp_path / "i.db") as store:
        hits = store.conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'T101 OR \"T-101\"'"
        ).fetchone()[0]
    assert hits >= 1, "the spreadsheet is not findable by its words"


def test_switching_it_on_gives_meaning_without_reading_the_spreadsheet_again(tmp_path):
    root, vectors, db = _corpus(tmp_path / "docs"), Vectors(), tmp_path / "i.db"
    _run(db, root, vectors, meaning=False)

    stats, _counts, sheet, flags = _run(db, root, vectors, meaning=True)

    assert stats.indexed == 0, "a file was read again to give it meaning"
    assert set(flags) == {1}, flags
    assert sheet.id in set(vectors.rows.values())


def test_a_spreadsheet_read_again_with_it_off_loses_its_old_vectors(tmp_path):
    root, vectors, db = _corpus(tmp_path / "docs"), Vectors(), tmp_path / "i.db"
    _stats, _counts, sheet, _flags = _run(db, root, vectors, meaning=True)
    assert sheet.id in set(vectors.rows.values())

    time.sleep(0.01)
    (root / "levels.csv").write_text(
        "tank,level,unit\nT-101,4.4,m\nT-102,3.1,m\n", encoding="utf-8")
    _stats, _counts, sheet, flags = _run(db, root, vectors, meaning=False)

    assert set(flags) == {SqliteStore.KEYWORD_ONLY}
    assert sheet.id not in set(vectors.rows.values()), (
        "vectors for passages that no longer exist were left behind")


def test_keyword_only_passages_are_not_counted_as_missing_vectors(tmp_path):
    root, vectors, db = _corpus(tmp_path / "docs"), Vectors(), tmp_path / "i.db"
    _stats, counts, _sheet, _flags = _run(db, root, vectors, meaning=False)
    with SqliteStore(db) as store:
        coverage = store.vector_coverage(vectors.count())

    assert coverage["coverage"] == 1.0 and coverage["missing"] == 0, coverage
    assert coverage["vectors_ready"]
    rows = index_summary(counts, {"rows": vectors.count()})
    covers = [row for row in rows if row.label == "Meaning-based search covers"]
    assert covers and not covers[0].warn, rows
    assert any(row.label == "Spreadsheet passages, by words only" for row in rows)


def test_the_setting_is_off_by_default_and_has_a_control():
    setting = next(s for s in SETTINGS if s.key == "INDEX_SPREADSHEET_MEANING")
    assert setting.default is False
    assert setting.surface == "indexing.tuning"
