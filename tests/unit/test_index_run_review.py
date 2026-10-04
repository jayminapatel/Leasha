r"""The index run, after the code review of 4 October 2026.

Layer: L3

The owner: "comprehensive code review for performance consistency and
reliability ... Do the recommended push and finish all". What these pin, one
finding each:

* **A run over one folder prunes only under it** - it pruned the whole index,
  so a folder moved away for a day lost its rows to a run over another folder;
  a run over the saved folder set still prunes everywhere.
* **The held-pictures list and the image book are kept however a run ends**,
  and with each resume-cursor write.
* **A new file is not hashed on the walker thread** - a reader hashes it, in
  parallel - and a file whose date moved but whose bytes did not is still
  found unchanged.
* **The re-queues read only the skip codes they want** (`skip_codes`).
* **Attachment scratch folders** go under the cache and are swept.
* **A zip inside a zip** is not read into memory past `max_bytes`.
* **One key for a drive's mount point** on both sides of the walk.
* **The What gets read page** follows the pass rule, images due included.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest

from app.core.errors import make_error
from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore

#: 2026-06-01 - old enough that nothing counts as "edited just now".
OLD_NS = 1_780_272_000 * 1_000_000_000


class _Vectors:
    def __init__(self) -> None:
        self.rows: dict[int, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = {int(one) for one in file_ids}
        for cid, fid in list(self.rows.items()):
            if fid in wanted:
                del self.rows[cid]

    def add(self, *, chunk_ids, file_ids, vectors) -> int:
        for cid, fid in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(cid)] = int(fid)
        return len(list(chunk_ids))

    def maybe_compact(self, **_k) -> bool:
        return False

    def maybe_create_index(self, **_k) -> bool:
        return False

    def count(self) -> int:
        return len(self.rows)


def _pipeline(store, roots, **config) -> Pipeline:
    embedder = Embedder(dim=4, encoder=lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts])
    return Pipeline(store, _Vectors(), embedder, PipelineConfig(
        walk=WalkConfig(roots=list(roots)), workers=1, min_free_gb=0, required_free_gb=0,
        limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                              min_free_gb=0, low_priority=False),
        **config))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(OLD_NS, OLD_NS))
    return path


# ---------------------------------------------------------------------------
# 1. A run over one folder prunes only under it
# ---------------------------------------------------------------------------

def _cli(capsys, *argv) -> int:
    from app.cli import main

    code = main(list(argv))
    capsys.readouterr()
    return code


@pytest.fixture()
def env(temp_env: Path) -> Path:
    with temp_env.open("a", encoding="utf-8") as handle:
        handle.write("\nINDEX_CPU_PERCENT=0\nMIN_FREE_GB=0\nREQUIRED_FREE_GB=0\n")
    return temp_env


def test_a_run_over_one_folder_does_not_prune_the_others(capsys, env, tmp_path) -> None:
    """The reviewer's reproduction: index A and B, take B's file away, run
    over A alone - B's row was deleted. A run over the saved folders still
    prunes everywhere, as before."""
    from app.cli import ROOTS_STATE_KEY
    from app.core.config import load_settings

    first = _write(tmp_path / "A" / "a.txt", "pump station north")
    gone = _write(tmp_path / "B" / "b.txt", "pump station south")
    db = Path(load_settings(env).fts_db)
    db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteStore(db) as store:
        store.set_state(ROOTS_STATE_KEY, f"{first.parent}|{gone.parent}")
    common = ("--env", str(env), "index", "--quiet", "--workers", "1",
              "--fake-embedder-for-bench")
    assert _cli(capsys, *common) == 0
    with SqliteStore(db) as store:
        assert store.get_file(str(gone)) is not None

    moved = gone.with_name("moved-away.txt.bak")
    gone.rename(moved)
    assert _cli(capsys, *common, str(first.parent)) == 0
    with SqliteStore(db) as store:
        assert store.get_file(str(gone)) is not None, "another folder's row survives"

    assert _cli(capsys, *common) == 0
    with SqliteStore(db) as store:
        assert store.get_file(str(gone)) is None, "the saved set's run still prunes"


def test_only_the_saved_set_or_no_prune_at_all_reaches_the_whole_index(tmp_path) -> None:
    from types import SimpleNamespace

    from app.index.resolve import Resolved
    from app.index.run_setup import build_pipeline_config, covers_saved_folders

    settings = SimpleNamespace(data_path=tmp_path / "data", log_path=tmp_path / "logs",
                               cache_path=tmp_path / "cache")
    tuned = Resolved(workers=1, embed_batch=8)
    one = build_pipeline_config(settings, [tmp_path / "A"], tuned=tuned)
    assert one.prune_missing and one.prune_under == (str(tmp_path / "A"),)
    whole = build_pipeline_config(settings, [tmp_path / "A"], tuned=tuned, whole=True)
    assert whole.prune_missing and whole.prune_under is None
    assert build_pipeline_config(settings, [tmp_path], tuned=tuned,
                                 prune=False).prune_under is None
    assert one.scratch_dir == tmp_path / "cache"
    saved = [str(tmp_path / "A"), str(tmp_path / "B") + os.sep]
    assert covers_saved_folders([tmp_path / "B", tmp_path / "A"], saved)
    assert not covers_saved_folders([tmp_path / "A"], saved)
    assert not covers_saved_folders([tmp_path / "A"], [])


# ---------------------------------------------------------------------------
# 3. The held-pictures list and the image book are kept however a run ends
# ---------------------------------------------------------------------------

def test_a_run_that_raises_still_keeps_the_held_pictures_list(tmp_path) -> None:
    from app.index.held_archives import HeldArchives

    root = tmp_path / "docs"
    _write(root / "a.txt", "pump station")
    archive = root / "mail.pst"
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [root])

        def consume(*_a, **_k):
            pipeline._held_archives().note(archive, held=3, finished=False)
            raise RuntimeError("the disk went away mid-run")

        pipeline._consume = consume
        with pytest.raises(RuntimeError):
            pipeline.run()
        assert HeldArchives(store).paths() == [archive]


def test_the_held_pictures_list_is_written_with_the_resume_cursors(tmp_path) -> None:
    from app.index.held_archives import HeldArchives

    archive = tmp_path / "mail.pst"
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [tmp_path])
        pipeline._last_resume_persist = float("-inf")
        pipeline._flush_pending_images = lambda: None
        pipeline._feed_sync = lambda pending: None
        pipeline._embed_abandoned = False
        pipeline._persist_resume_progress = lambda: None
        pipeline._held_archives().note(archive, held=1, finished=False)
        pipeline._persist_at_folder_boundary([])
        assert HeldArchives(store).paths() == [archive]


# ---------------------------------------------------------------------------
# 4. A new file is hashed by a reader, not on the walker thread
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ["found", "newest"])
def test_a_new_file_is_never_hashed_on_the_walker_thread(tmp_path, monkeypatch, order) -> None:
    from app.index import walker

    root = tmp_path / "docs"
    for index in range(4):
        _write(root / f"n{index}.txt", f"pump station {index}")
    threads: list[str] = []
    real = walker.content_hash

    def spy(path, **kwargs):
        threads.append(threading.current_thread().name)
        return real(path, **kwargs)

    monkeypatch.setattr(walker, "content_hash", spy)
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, [root], read_order=order).run()
        hashes = {record.path: record.content_hash for record in store.iter_files()}
    assert stats.indexed == 4
    assert len(threads) == 4 and "walker" not in threads, threads
    assert all(hashes.values()), "every row still has its hash"


def test_a_restored_copy_with_a_new_date_is_still_found_unchanged(tmp_path) -> None:
    """The `robocopy` case: the date moved, the bytes did not, the row has a
    hash - still decided on the walker thread, and not read again."""
    root = tmp_path / "docs"
    path = _write(root / "a.txt", "pump station")
    with SqliteStore(tmp_path / "index.db") as store:
        assert _pipeline(store, [root]).run().indexed == 1
        later = OLD_NS + 86_400 * 1_000_000_000
        os.utime(path, ns=(later, later))
        again = _pipeline(store, [root]).run()
    assert again.indexed == 0 and again.unchanged == 1


# ---------------------------------------------------------------------------
# 5. The re-queues read only the codes they want
# ---------------------------------------------------------------------------

def test_iter_files_narrows_by_skip_code_in_the_query(tmp_path) -> None:
    with SqliteStore(tmp_path / "index.db") as store:
        for name, code in (("a.pdf", "ERR_NO_TEXT_LAYER"), ("b.jpg", "ERR_OCR_HELD"),
                           ("c.docx", "ERR_FILE_LOCKED")):
            path = str(tmp_path / name)
            file_id = store.upsert_file(path, size_bytes=1, mtime_ns=1)
            store.mark_skipped(file_id, make_error(code, "t", path=path))
        locked = [r.path for r in store.iter_files(status=FileStatus.SKIPPED,
                                                   skip_codes=("ERR_FILE_LOCKED",))]
        none = list(store.iter_files(status=FileStatus.SKIPPED, skip_codes=()))
        every = list(store.iter_files(status=FileStatus.SKIPPED))
    assert locked == [str(tmp_path / "c.docx")]
    assert none == [] and len(every) == 3


# ---------------------------------------------------------------------------
# 7. Attachment scratch folders
# ---------------------------------------------------------------------------

def test_attachment_scratch_goes_under_the_cache_and_leftovers_are_swept(tmp_path) -> None:
    from app.extract import pst_libpff

    cache = tmp_path / "cache"
    try:
        pst_libpff.configure_scratch(cache)
        scratch = pst_libpff._Scratch()
        written = scratch.write("report.pdf", b"%PDF")
        assert written.parent.parent == cache / pst_libpff.SCRATCH_FOLDER
        scratch.close()
        assert not written.parent.exists()

        place = cache / pst_libpff.SCRATCH_FOLDER
        stale = place / f"{pst_libpff.SCRATCH_PREFIX}old"
        fresh = place / f"{pst_libpff.SCRATCH_PREFIX}new"
        for folder in (stale, fresh):
            folder.mkdir(parents=True)
            (folder / "x.bin").write_bytes(b"x")
        old = time.time() - pst_libpff.SCRATCH_STALE_S - 60
        os.utime(stale, (old, old))
        assert pst_libpff.sweep_scratch(cache) >= 1
        assert not stale.exists() and fresh.exists(), "a reader at work keeps its own"
    finally:
        pst_libpff.configure_scratch(None)


def test_every_run_points_the_scratch_at_its_cache(tmp_path) -> None:
    from app.extract import pst_libpff

    root = tmp_path / "docs"
    _write(root / "a.txt", "pump station")
    try:
        with SqliteStore(tmp_path / "index.db") as store:
            _pipeline(store, [root], scratch_dir=tmp_path / "cache").run()
        assert pst_libpff._scratch_root == tmp_path / "cache" / pst_libpff.SCRATCH_FOLDER
    finally:
        pst_libpff.configure_scratch(None)


# ---------------------------------------------------------------------------
# 8. A zip inside a zip
# ---------------------------------------------------------------------------

def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zipped:
        for name, data in members.items():
            zipped.writestr(name, data)
    return buffer.getvalue()


def test_a_nested_zip_over_the_limit_is_not_read_into_memory() -> None:
    from app.extract.pst_attachment import member_of

    inner = _zip({"x.txt": b"small", "padding.bin": b"0" * 5_000})
    outer = _zip({"pack/inner.zip": inner})
    assert member_of(outer, "pack/inner.zip/x.txt", max_bytes=1_000) is None
    assert member_of(outer, "pack/inner.zip/x.txt", max_bytes=100_000) == b"small"
    assert member_of(outer, "pack/inner.zip/x.txt") == b"small"


# ---------------------------------------------------------------------------
# 10. One key for a drive's mount point
# ---------------------------------------------------------------------------

def test_the_mount_point_key_is_path_key_on_both_sides() -> None:
    from app.core.osbridge.pathnames import path_key
    from app.index.walker import volume_root_key

    assert volume_root_key("E:\\") == volume_root_key("e:") == path_key("E:")
    assert volume_root_key(r"D:\Media\\") == path_key(r"D:\Media")


# ---------------------------------------------------------------------------
# 9. What gets read follows the pass rule
# ---------------------------------------------------------------------------

def test_the_pictures_sentence_follows_the_pass_rule_images_due_included() -> None:
    from app.ui.presenter.coverage import PICTURES, place_sentence

    after = {"index_ocr_pass": " After-Run ", "index_ocr_mode": "both"}
    assert place_sentence(PICTURES, after).startswith("Text in photos and scanned pages "
                                                      "is read after each run")
    assert place_sentence(PICTURES, {**after, "images_due": True}).startswith(
        "This run reads only photos and scanned pages.")
    assert place_sentence(PICTURES, {"index_ocr_mode": "images"}).startswith(
        "This run reads only photos and scanned pages.")
    assert place_sentence(PICTURES, {"index_ocr_mode": "nonsense"}).startswith(
        "Text in photos and scanned pages is read during each run.")
    assert json.dumps(place_sentence(PICTURES, {}))     # never raises, never empty
