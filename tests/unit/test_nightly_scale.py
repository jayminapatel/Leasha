r"""Order 0m section 5a - the nightly's scale fixture, kill-resume stage and
ladder probe, without running the (multi-minute) loop itself.

Layer: n/a (tooling)

`tools/nightly.py` was run for real three times on 2026-09-20 (see the order's
dated note and `PERF_FLOORS`); what is tested here is the logic around it that
does not need a subprocess: the corpus builder, the mid-flight test, the
INDEXED-count read, and the ladder probe's model-free half.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.fixtures.scale_corpus import (                        # noqa: E402
    DEFAULT_FILES,
    ensure_scale_corpus,
)
from tools import nightly, nightly_probe                          # noqa: E402

# ---------------------------------------------------------------------------
# the scale corpus
# ---------------------------------------------------------------------------


def test_the_corpus_has_exactly_the_files_asked_for_and_each_is_unique(tmp_path):
    made = ensure_scale_corpus(tmp_path / "scale", 40)
    files = sorted((tmp_path / "scale").glob("doc-*.txt"))
    assert made["files"] == 40 and len(files) == 40 and made["refreshed"] is True
    bodies = {f.read_bytes() for f in files}
    assert len(bodies) == 40                      # content-hash de-duplication cannot collapse them
    assert all(f.stat().st_size > 1000 for f in files)


def test_the_manifest_sits_beside_the_folder_so_it_is_never_indexed(tmp_path):
    ensure_scale_corpus(tmp_path / "scale", 3)
    assert (tmp_path / "scale.manifest.json").exists()
    assert not list((tmp_path / "scale").glob("*.json"))


def test_a_matching_corpus_is_left_alone_and_a_different_count_refreshes(tmp_path):
    root = tmp_path / "scale"
    ensure_scale_corpus(root, 5)
    stamp = (root / "doc-00000.txt").stat().st_mtime_ns
    again = ensure_scale_corpus(root, 5)
    assert again["refreshed"] is False
    assert (root / "doc-00000.txt").stat().st_mtime_ns == stamp
    bigger = ensure_scale_corpus(root, 8)
    assert bigger["refreshed"] is True and len(list(root.glob("doc-*.txt"))) == 8
    smaller = ensure_scale_corpus(root, 2)
    assert smaller["refreshed"] is True and len(list(root.glob("doc-*.txt"))) == 2


def test_refreshing_never_deletes_a_file_it_did_not_write(tmp_path):
    root = tmp_path / "scale"
    root.mkdir()
    (root / "keep-me.txt").write_text("not mine", encoding="utf-8")
    ensure_scale_corpus(root, 4)
    ensure_scale_corpus(root, 2)
    assert (root / "keep-me.txt").read_text(encoding="utf-8") == "not mine"


def test_the_corpus_is_deterministic(tmp_path):
    ensure_scale_corpus(tmp_path / "a", 6)
    ensure_scale_corpus(tmp_path / "b", 6)
    for name in ("doc-00000.txt", "doc-00005.txt"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_the_default_size_is_a_few_hundred():
    assert 100 <= DEFAULT_FILES <= 2000
    assert nightly._scale_files(None) == nightly.DEFAULT_SCALE_FILES
    assert nightly._scale_files(7) == 7


def test_the_scale_count_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LEASHA_NIGHTLY_SCALE_FILES", "12")
    assert nightly._scale_files(None) == 12
    monkeypatch.setenv("LEASHA_NIGHTLY_SCALE_FILES", "not a number")
    assert nightly._scale_files(None) == nightly.DEFAULT_SCALE_FILES


# ---------------------------------------------------------------------------
# reading the INDEXED count, and the mid-flight kill
# ---------------------------------------------------------------------------


def _env_pointing_at(tmp_path: Path, indexed: int, other: int = 2) -> Path:
    db = tmp_path / "fts" / "knowledge.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, status TEXT)")
    conn.executemany("INSERT INTO files (status) VALUES (?)",
                     [("INDEXED",)] * indexed + [("SKIPPED",)] * other)
    conn.commit()
    conn.close()
    env = tmp_path / ".env"
    env.write_text(f"FTS_DB={db.as_posix()}\n", encoding="utf-8")
    return env


def test_the_indexed_count_reads_only_indexed_rows(tmp_path):
    assert nightly._indexed_count(_env_pointing_at(tmp_path, 7)) == 7


def test_a_store_that_does_not_exist_yet_counts_zero(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"FTS_DB={(tmp_path / 'nope' / 'k.db').as_posix()}\n", encoding="utf-8")
    assert nightly._indexed_count(env) == 0


class _Proc:
    def __init__(self, *, finished: bool):
        self._finished = finished
        self.terminated = False

    def poll(self):
        return 0 if self._finished or self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class _Done:
    def __init__(self, code: int = 0):
        self.returncode = code


def _patch_stage(monkeypatch, *, counts, finished=False, resume_code=0):
    """`counts` is what `_indexed_count` returns on successive calls."""
    sequence = iter(counts)
    last = [counts[-1]]

    def fake_count(_env):
        try:
            last[0] = next(sequence)
        except StopIteration:
            pass
        return last[0]

    proc = _Proc(finished=finished)
    monkeypatch.setattr(nightly, "_indexed_count", fake_count)
    monkeypatch.setattr(nightly.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(nightly, "_run_cli", lambda *a, **k: _Done(resume_code))
    monkeypatch.setattr(nightly.time, "sleep", lambda _s: None)
    return proc


def test_a_kill_that_lands_mid_flight_then_resumes_to_the_reference_count_passes(
        monkeypatch, tmp_path):
    proc = _patch_stage(monkeypatch, counts=[0, 0, 13, 326])
    result = nightly.stage_kill_resume(tmp_path / ".env", [tmp_path], reference_indexed=326)
    assert result["ok"] is True
    assert proc.terminated is True and result["killed_at"] == 13
    assert result["indexed_after"] == 326


def test_a_resume_that_falls_short_of_the_reference_count_fails(monkeypatch, tmp_path):
    _patch_stage(monkeypatch, counts=[0, 9, 200])
    result = nightly.stage_kill_resume(tmp_path / ".env", [tmp_path], reference_indexed=326)
    assert result["ok"] is False
    assert "did not reach" in result["error"]


def test_a_failing_resume_command_fails_the_stage(monkeypatch, tmp_path):
    _patch_stage(monkeypatch, counts=[0, 9, 326], resume_code=1)
    result = nightly.stage_kill_resume(tmp_path / ".env", [tmp_path], reference_indexed=326)
    assert result["ok"] is False


def test_an_empty_reference_cannot_pass(monkeypatch, tmp_path):
    """A first run that indexed nothing must not make every later comparison vacuous."""
    _patch_stage(monkeypatch, counts=[0, 5, 5])
    result = nightly.stage_kill_resume(tmp_path / ".env", [tmp_path], reference_indexed=0)
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# the ladder probe: the model-free half really runs; the OCR half is honest
# ---------------------------------------------------------------------------


def test_the_generated_ladder_images_are_two_kinds_with_neutral_names(tmp_path):
    pytest.importorskip("PIL")
    pages, photos = nightly_probe._ladder_images(tmp_path)
    assert len(pages) == len(photos) == nightly_probe.LADDER_IMAGES // 2
    assert not any(p.name.lower().startswith(("img_", "dsc_", "screenshot")) for p in pages + photos)


def test_rung_zero_and_one_are_measured_without_any_model(monkeypatch):
    pytest.importorskip("PIL")
    from app.extract import ocr

    monkeypatch.setattr(ocr, "_load_engine", lambda: None)      # no OCR engine on this "machine"
    out = nightly_probe.measure_ladder()
    assert out["ladder_rung01_images_per_second"] > 0
    assert out["ladder_probe_ms"] is None                         # not measured, and it says so
    assert any("rung 2 not measured" in n for n in out["notes"])


def test_the_pages_route_to_ocr_at_rung_one_and_the_photos_do_not(tmp_path):
    pytest.importorskip("PIL")
    from app.extract.ocr_ladder import RouteDecision, route

    pages, photos = nightly_probe._ladder_images(tmp_path)
    assert route(pages[0], detect=None).decision == RouteDecision.FULL_OCR
    assert route(photos[0], detect=None).decision != RouteDecision.FULL_OCR


def test_every_floor_is_a_metric_the_probe_or_run_actually_produces():
    produced = {"index_files_per_second", "recall_at_10", "chunker_chunks_per_second",
                "embed_chunks_per_second", "ladder_rung01_images_per_second",
                "ladder_probe_ms", "search_p95_ms"}
    assert set(nightly.PERF_FLOORS) == produced
    assert all(v is not None for v in nightly.PERF_FLOORS.values()), \
        "every metric the order names now has a measured, pinned floor"
