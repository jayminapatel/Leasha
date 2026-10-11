r"""An identical copy takes the vectors already made (order 1h 6b), and drawings are words only.

Layer: L3

2026-10-11, measured on the owner's index while its overnight run was in the
meaning phase: 1,610,571 of 6,328,527 passages bound for the model (25%) were
in second-and-later copies of a file with the same `content_hash` - backups,
versioned folders, the same attachment twice - and every copy was embedded
again. Now a passage whose text matches one in an already-embedded copy takes
that vector. The same day the owner made SVG words only ("make svg keyword
only"): 651,707 passages, 10% of what waited, labels and attribute values.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

NO_PACING = ResourceLimits(pause_on_battery=False, cpu_percent=0)

REPORT = ("Pump station commissioning report. The burner at Leeds was replaced.\n\n"
          "Licence 12400 is current and the next inspection is due in March.")
DRAWING = ('<svg xmlns="http://www.w3.org/2000/svg"><text x="10" y="20">'
           'Tank T-101 level transmitter</text></svg>')


class Vectors:
    """Keeps each chunk's vector, so a reused one can be read back and compared."""

    def __init__(self) -> None:
        self.rows: dict[int, tuple[int, list[float]]] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        gone = {int(one) for one in file_ids}
        self.rows = {cid: row for cid, row in self.rows.items() if row[0] not in gone}

    def add(self, *, chunk_ids, file_ids, vectors, **_kw) -> int:
        for cid, fid, vector in zip(chunk_ids, file_ids, vectors, strict=True):
            self.rows[int(cid)] = (int(fid), [float(v) for v in vector])
        return len(list(chunk_ids))

    def vectors_for(self, chunk_ids) -> dict[int, list[float]]:
        return {int(c): self.rows[int(c)][1] for c in chunk_ids if int(c) in self.rows}

    def count(self) -> int:
        return len(self.rows)

    def files(self) -> set[int]:
        return {fid for fid, _v in self.rows.values()}

    def __getattr__(self, name):
        return lambda *args, **kwargs: False


def _run(db: Path, roots: list[Path], vectors: Vectors, sent: list[str], **config):
    def encode(texts):
        sent.extend(texts)
        return [[float(len(text)), 1.0, 0.0, 0.0] for text in texts]

    with SqliteStore(db) as store:
        settings = PipelineConfig(walk=WalkConfig(roots=roots), workers=1,
                                  limits=NO_PACING, **config)
        return Pipeline(store, vectors, Embedder(dim=4, encoder=encode), settings).run()


def _file_id(db: Path, path: Path) -> int:
    with SqliteStore(db) as store:
        return store.get_file(str(path)).id


def _flags(db: Path, path: Path) -> list[int]:
    with SqliteStore(db) as store:
        file_id = store.get_file(str(path)).id
        return [int(r[0]) for r in store.conn.execute(
            "SELECT embedded FROM chunks WHERE file_id = ?", (file_id,))]


def test_copies_of_an_embedded_file_take_its_vectors_and_never_reach_the_model(tmp_path):
    first = tmp_path / "docs" / "2024"
    first.mkdir(parents=True)
    (first / "report.txt").write_text(REPORT, encoding="utf-8")
    db, vectors, sent = tmp_path / "i.db", Vectors(), []
    _run(db, [tmp_path / "docs"], vectors, sent)
    assert sent, "the original was never embedded"

    for folder in ("backup", "old copy"):
        (tmp_path / "docs" / folder).mkdir()
        shutil.copy2(first / "report.txt", tmp_path / "docs" / folder / "report.txt")
    sent.clear()
    stats = _run(db, [tmp_path / "docs"], vectors, sent)

    assert sent == [], f"a copy's passages were sent to the model: {sent}"
    assert stats.vectors_reused >= 2
    original = _file_id(db, first / "report.txt")
    by_file: dict[int, list[list[float]]] = {}
    for fid, vector in vectors.rows.values():
        by_file.setdefault(fid, []).append(vector)
    for folder in ("backup", "old copy"):
        copy = _file_id(db, tmp_path / "docs" / folder / "report.txt")
        assert sorted(by_file[copy]) == sorted(by_file[original])
        assert set(_flags(db, tmp_path / "docs" / folder / "report.txt")) == {1}


def test_a_file_that_differs_is_embedded_as_before(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "report.txt").write_text(REPORT, encoding="utf-8")
    db, vectors, sent = tmp_path / "i.db", Vectors(), []
    _run(db, [root], vectors, sent)

    (root / "edited.txt").write_text(REPORT + " Signed off by the site manager.",
                                     encoding="utf-8")
    sent.clear()
    stats = _run(db, [root], vectors, sent)

    assert sent, "a file with different words took another file's vectors"
    assert stats.vectors_reused == 0


def test_off_with_embed_repeated_passages_once(tmp_path):
    root = tmp_path / "docs"
    (root / "a").mkdir(parents=True)
    (root / "a" / "report.txt").write_text(REPORT, encoding="utf-8")
    db, vectors, sent = tmp_path / "i.db", Vectors(), []
    _run(db, [root], vectors, sent, dedup_chunks=False)

    (root / "b").mkdir()
    shutil.copy2(root / "a" / "report.txt", root / "b" / "report.txt")
    sent.clear()
    stats = _run(db, [root], vectors, sent, dedup_chunks=False)

    assert sent, "reuse ran with EMBED_DEDUP off"
    assert stats.vectors_reused == 0


def test_a_drawing_is_found_by_its_words_even_with_spreadsheets_by_meaning_on(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "tank.svg").write_text(DRAWING, encoding="utf-8")
    (root / "report.txt").write_text(REPORT, encoding="utf-8")
    db, vectors, sent = tmp_path / "i.db", Vectors(), []
    _run(db, [root], vectors, sent, spreadsheet_meaning=True)

    flags = _flags(db, root / "tank.svg")
    assert flags and set(flags) == {SqliteStore.KEYWORD_ONLY}, flags
    assert _file_id(db, root / "tank.svg") not in vectors.files()
    assert not any("T-101" in text for text in sent), "the drawing reached the model"
    with SqliteStore(db) as store:
        hits = store.conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'transmitter'").fetchone()[0]
    assert hits >= 1, "the drawing is not findable by its words"


def test_drawing_passages_already_waiting_leave_the_queue_at_the_next_run(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "tank.svg").write_text(DRAWING, encoding="utf-8")
    db, vectors, sent = tmp_path / "i.db", Vectors(), []
    _run(db, [root], vectors, sent)
    # As the owner's index stood: the drawing's passages stored and waiting.
    with SqliteStore(db) as store:
        file_id = store.get_file(str(root / "tank.svg")).id
        with store.write() as conn:
            conn.execute("UPDATE chunks SET embedded = 0 WHERE file_id = ?", (file_id,))

    sent.clear()
    _run(db, [root], vectors, sent)

    assert set(_flags(db, root / "tank.svg")) == {SqliteStore.KEYWORD_ONLY}
    assert not any("T-101" in text for text in sent)


def test_switching_spreadsheets_to_meaning_does_not_release_the_drawings(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "tank.svg").write_text(DRAWING, encoding="utf-8")
    (root / "levels.csv").write_text("tank,level\nT-101,4.2\n", encoding="utf-8")
    db, vectors, sent = tmp_path / "i.db", Vectors(), []
    _run(db, [root], vectors, sent)

    _run(db, [root], vectors, sent, spreadsheet_meaning=True)

    assert set(_flags(db, root / "tank.svg")) == {SqliteStore.KEYWORD_ONLY}
    assert set(_flags(db, root / "levels.csv")) == {1}
