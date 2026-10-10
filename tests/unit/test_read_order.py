r"""Scan, then sort, then read: the "newest first (mixed)" order.

Layer: L3 (and the L5 controls that set it)

What these pin, from the owner's decision of 2026-09-29 ("mixed by date"):

* **The order is honoured.** The folders marked "Index this folder first" are
  read first, in the order marked; then everything else newest first, mail and
  files mixed; within one month, small before large. The same on disk as in
  memory.
* **Resume.** A run stopped part-way leaves its finished files settled; the
  next run reads only the rest, in the same order an uninterrupted run would
  have, and nothing twice.
* **An unchanged corpus reads nothing.** A rerun with nothing changed queues no
  file, and the scan never hashes a new file (the hash is taken on its turn).
* **The setting is reachable.** `INDEX_ORDER` has a control, a registry entry,
  a config value and a pipeline field; "Index this folder first" is on the
  folder list and reaches the in-process run, the command line and the child.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.index import read_order
from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.read_order import (
    BUCKET_NS,
    FIRST_FOLDERS_STATE_KEY,
    WorkList,
    candidate_fields,
    dump_first_folders,
    load_first_folders,
    normalise_order,
)
from app.index.resources import ResourceLimits
from app.index.walker import Candidate, WalkConfig, _priority_for
from app.storage.sqlite_store import SqliteStore

DAY_NS = 24 * 3600 * 1_000_000_000
#: 2026-06-01, far enough back that nothing counts as "edited just now".
BASE_NS = 1_780_272_000 * 1_000_000_000


class _Vectors:
    """A vector store that counts, not writes. As `test_phase_progress.py`."""

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


def _candidate(name: str, *, days_ago: float, size: int, priority: int = 100) -> Candidate:
    return Candidate(path=Path("/corpus") / name, size_bytes=size,
                     mtime_ns=int(BASE_NS - days_ago * DAY_NS), priority=priority)


# ---------------------------------------------------------------------------
# The sort itself
# ---------------------------------------------------------------------------

def _mixed() -> list[Candidate]:
    return [
        _candidate("old-small.txt", days_ago=400, size=10),
        _candidate("new-big.pst", days_ago=1, size=9_000_000),
        _candidate("new-small.txt", days_ago=2, size=50),
        _candidate("chosen-old.txt", days_ago=900, size=5, priority=0),
        _candidate("second-choice.txt", days_ago=0, size=5, priority=1),
        _candidate("last-month.eml", days_ago=45, size=20),
        _candidate("new-mid.eml", days_ago=3, size=4_000),
    ]


EXPECTED = ["chosen-old.txt", "second-choice.txt", "new-small.txt",
            "new-mid.eml", "new-big.pst", "last-month.eml", "old-small.txt"]


def test_first_folders_then_newest_then_small_before_large() -> None:
    """Mail (`.eml`, `.pst`) and files are one list, each by its own date."""
    with WorkList() as work:
        for candidate in _mixed():
            work.add(candidate, None)
        got = [candidate.path.name for candidate, _d in work.sorted()]
    assert got == EXPECTED


def test_the_same_order_when_the_list_is_on_disk(tmp_path: Path) -> None:
    with WorkList(tmp_path, spill_at=2) as work:
        for index, candidate in enumerate(_mixed()):
            work.add(candidate, None if index % 2 else f"hash{index}")
        assert work.spilled and work.spill_path is not None
        spill = work.spill_path
        assert spill.parent == tmp_path, "the spill goes beside the index"
        got = list(work.sorted())
    assert [c.path.name for c, _d in got] == EXPECTED
    # Every field survives the trip, and None stays distinct from "".
    by_name = {c.path.name: (c, d) for c, d in got}
    assert by_name["chosen-old.txt"][0] == _mixed()[3]
    assert by_name["old-small.txt"][1] == "hash0"
    assert by_name["new-big.pst"][1] is None
    assert not spill.exists(), "the spill file is removed on close"


def test_an_empty_decision_is_not_turned_into_none(tmp_path: Path) -> None:
    """"" means "could not tell, read it"; None means "changed, no hash"."""
    with WorkList(tmp_path, spill_at=1) as work:
        work.add(_candidate("a.txt", days_ago=1, size=1), "")
        work.add(_candidate("b.txt", days_ago=1, size=2), None)
        got = [d for _c, d in work.sorted()]
    assert got == ["", None]


def test_the_spill_file_keeps_every_candidate_field() -> None:
    """A field added to `Candidate` without a column would come back as its
    default after a large scan - silently, and only on big corpora."""
    assert set(read_order._CANDIDATE_COLUMNS) == set(candidate_fields())


def test_within_a_month_small_goes_first_across_months_new_goes_first() -> None:
    small_old = _candidate("a", days_ago=31 + 1, size=1)
    big_new = _candidate("b", days_ago=1, size=10**9)
    assert read_order.sort_key(big_new, 1) < read_order.sort_key(small_old, 0)
    same_month_small = _candidate("c", days_ago=2, size=1)
    same_bucket = (big_new.mtime_ns // BUCKET_NS) == (same_month_small.mtime_ns // BUCKET_NS)
    if same_bucket:
        assert read_order.sort_key(same_month_small, 2) < read_order.sort_key(big_new, 1)


def test_the_order_setting_is_forgiving() -> None:
    assert normalise_order("newest") == "newest"
    assert normalise_order(" FOUND ") == "found"
    assert normalise_order("") == "newest"
    assert normalise_order("sideways") == "newest"


def test_first_folders_round_trip_in_order() -> None:
    raw = dump_first_folders([r"D:\Work", r"C:\Home", r"D:\Work", ""])
    assert load_first_folders(raw) == [r"D:\Work", r"C:\Home"]
    assert load_first_folders("") == []
    assert load_first_folders("not json") == []
    assert load_first_folders('{"a": 1}') == []


def test_a_first_folder_matches_at_a_folder_boundary_only() -> None:
    roots = [Path("C:/Docs"), Path("C:/Mail")]
    assert _priority_for(Path("C:/Docs/a.txt"), roots) == 0
    assert _priority_for(Path("C:/Mail/x/b.eml"), roots) == 1
    assert _priority_for(Path("C:/Docs2/a.txt"), roots) == 100


# ---------------------------------------------------------------------------
# Through the real pipeline
# ---------------------------------------------------------------------------

def _corpus(root: Path) -> dict[str, int]:
    """Twelve files whose walk order has nothing to do with their dates.

    `chosen/` is marked first. Returns `{name: days ago}`.
    """
    ages = {
        "a-old.txt": 700, "b-new.txt": 1, "c-mid.txt": 90, "d-newest.txt": 0.5,
        "e-older.txt": 1200, "f-recent.txt": 10, "g-ancient.txt": 3000,
        "h-week.txt": 7, "i-year.txt": 365,
    }
    chosen_ages = {"chosen/x-old.txt": 2000, "chosen/y-new.txt": 3}
    other = {"zz/late.txt": 40}
    everything = {**ages, **chosen_ages, **other}
    for index, (name, days) in enumerate(everything.items()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # A distinct size each, walk order unrelated to size or date.
        body = f"pump station report {name} ".ljust(100 + ((index * 7) % 12) * 13, "x")
        path.write_text(body, encoding="utf-8")
        stamp = int(BASE_NS - days * DAY_NS)
        os.utime(path, ns=(stamp, stamp))
    return everything


def _expected_order(root: Path, ages: dict[str, float]) -> list[str]:
    """What the order promises, worked out independently of `sort_key`:
    `chosen/` first; then by month, newest first; within a month, smallest
    first. The corpus is built so that no two files tie on all three."""
    def key(name: str) -> tuple:
        stat = (root / name).stat()
        return (0 if name.startswith("chosen/") else 1,
                -(stat.st_mtime_ns // BUCKET_NS), stat.st_size)

    keys = {name: key(name) for name in ages}
    assert len(set(keys.values())) == len(keys), "the test corpus has a tie"
    return sorted(ages, key=lambda name: keys[name])


def _pipeline(store, root: Path, **config) -> Pipeline:
    embedder = Embedder(dim=4, encoder=lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts])
    walk = WalkConfig(roots=[root], priority_roots=[root / "chosen"])
    return Pipeline(store, _Vectors(), embedder, PipelineConfig(
        walk=walk, workers=1, min_free_gb=0, required_free_gb=0,
        limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                              min_free_gb=0, low_priority=False),
        **config))


def _record_reads(pipeline: Pipeline, root: Path, reads: list[str], *,
                  stop_after: int = 0) -> None:
    """Note each file as it is written, in order; optionally stop the run."""
    original = pipeline._write_one

    def write_one(item):
        out = original(item)
        if item.first_of_file:
            reads.append(item.candidate.path.relative_to(root).as_posix())
            if stop_after and len(reads) >= stop_after:
                pipeline.request_stop()
        return out

    pipeline._write_one = write_one


def test_a_run_reads_the_chosen_folder_first_then_newest(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    ages = _corpus(root)
    reads: list[str] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root)
        _record_reads(pipeline, root, reads)
        stats = pipeline.run()
    assert stats.indexed == len(ages)
    assert reads == _expected_order(root, ages)
    assert stats.walk_complete


def test_as_found_is_still_the_walk_order(tmp_path: Path) -> None:
    """The old behaviour, kept behind the setting: no scan phase, and the
    chosen folder is not guaranteed first across the corpus (only within the
    queue's window - which, for twelve files, is all of it)."""
    root = tmp_path / "corpus"
    ages = _corpus(root)
    reads: list[str] = []
    phases: list[str] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root, read_order="found")
        _record_reads(pipeline, root, reads)
        stats = pipeline.run(on_progress=lambda s: phases.append(s.phase))
    assert stats.indexed == len(ages)
    assert "scanning" not in phases
    assert sorted(reads) == sorted(ages)


def test_an_interrupted_run_resumes_in_the_same_order(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    ages = _corpus(root)
    expected = _expected_order(root, ages)
    first: list[str] = []
    second: list[str] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root)
        _record_reads(pipeline, root, first, stop_after=5)
        pipeline.run()
        # A stop lands between documents; whatever was read is a prefix.
        assert 5 <= len(first) < len(ages)
        assert first == expected[:len(first)]

        again = _pipeline(store, root)
        _record_reads(again, root, second)
        stats = again.run()
    assert second == expected[len(first):], "the rest, in the same order"
    assert not set(first) & set(second), "nothing is read twice"
    assert stats.unchanged == len(first)


def test_a_rerun_with_nothing_changed_reads_nothing(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    ages = _corpus(root)
    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, root).run()

        reads: list[str] = []
        hashed: list[str] = []
        from app.index import walker

        real_hash = walker.content_hash
        monkeypatch.setattr(walker, "content_hash",
                            lambda path: hashed.append(str(path)) or real_hash(path))
        again = _pipeline(store, root)
        _record_reads(again, root, reads)
        stats = again.run()
    assert reads == []
    assert stats.indexed == 0
    assert stats.unchanged == len(ages)
    assert hashed == [], "a settled file is decided from its row and stat alone"


def test_the_scan_does_not_hash_new_files(tmp_path: Path, monkeypatch) -> None:
    """Every new file is hashed exactly once - on its turn, not in the scan
    as well - and only after the whole walk has finished.

    *2026-10-10 (W1):* except the folders marked first, which are read while
    the rest is walked - so only their files may be hashed before the walk
    ends, and still each exactly once, by its reader."""
    root = tmp_path / "corpus"
    ages = _corpus(root)
    hashed: list[tuple[str, bool]] = []
    from app.index import walker

    real_hash = walker.content_hash
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root)

        def spy(path):
            hashed.append((Path(path).relative_to(root).as_posix(),
                           bool(pipeline._stats_ref.walk_complete)))
            return real_hash(path)

        monkeypatch.setattr(walker, "content_hash", spy)
        stats = pipeline.run()
    assert stats.indexed == len(ages)
    assert sorted(name for name, _done in hashed) == sorted(ages)
    early = [name for name, done in hashed if not done]
    assert all(name.startswith("chosen/") for name in early), early


def test_a_large_scan_goes_to_disk_and_is_tidied_away(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(read_order, "SPILL_AT", 3)
    root = tmp_path / "corpus"
    ages = _corpus(root)
    reads: list[str] = []
    data = tmp_path / "data"
    data.mkdir()
    with SqliteStore(data / "index.db") as store:
        pipeline = _pipeline(store, root)
        _record_reads(pipeline, root, reads)
        pipeline.run()
    assert reads == _expected_order(root, ages)
    assert not list(data.glob("leasha-worklist-*")), "the spill file was removed"


def test_the_machine_is_read_a_few_times_a_second_not_once_a_file(tmp_path: Path) -> None:
    """Order 0z E4, measured on Windows 2026-09-30. The walker asked the
    resource governor before every file, and one ask reads the process table:
    26-30ms on the owner's laptop against 1.76ms in the Linux sandbox. That
    held a whole run to about 35 files a second, and a rerun with nothing
    changed took 207s for 9,002 files in the "as found" order.

    Fails on the code as it was: 500 asks, 500 reads of the machine."""
    import time

    from app.index.pipeline import SCAN_GOVERNOR_S, IndexStats
    from app.index.resources import ResourceGovernor, Snapshot

    reads: list[int] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, tmp_path)
        pipeline.governor = ResourceGovernor(
            pipeline.config.resolved_limits(),
            probe=lambda: reads.append(1) or Snapshot())
        stats = IndexStats()

        started = time.monotonic()
        assert all(pipeline._governor_allows(stats) for _ in range(500))
        took = time.monotonic() - started
        assert 1 <= len(reads) <= 2 + took / SCAN_GOVERNOR_S, (
            f"{len(reads)} reads of the machine for 500 files in {took:.2f}s")

        # The interval is respected, not skipped for good.
        before = len(reads)
        time.sleep(SCAN_GOVERNOR_S + 0.05)
        assert pipeline._governor_allows(stats)
        assert len(reads) == before + 1

        # **The person's pause is never made to wait for the interval.** It is
        # a flag, not a measurement: the very next file notices it. (A stop is
        # asked for first - `request_stop` lets go of a pause - so the wait
        # the pause starts ends at once instead of holding this test.)
        assert pipeline._governor_allows(stats) is True
        pipeline.request_stop()
        pipeline.governor.pause_manually()
        assert pipeline._governor_allows(stats) is False


# ---------------------------------------------------------------------------
# The setting, and "Index this folder first", reach the run
# ---------------------------------------------------------------------------

def test_index_order_is_a_registered_choice_with_newest_the_default(tmp_path: Path) -> None:
    from app.core import settings_registry as reg
    from app.core.config import load_settings

    entry = reg.by_key("INDEX_ORDER")
    assert entry.default == "newest" and set(entry.choices) == {"newest", "found"}
    env = tmp_path / ".env"
    env.write_text(f"DATA_PATH={tmp_path / 'data'}\nINDEX_ORDER=found\n", encoding="utf-8")
    assert load_settings(env).index_order == "found"


def test_the_command_line_uses_the_setting_and_the_saved_first_folders(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from app.cli.index import _saved_first_folders, build_pipeline_config
    from app.core.config import load_settings

    env = tmp_path / ".env"
    env.write_text(f"DATA_PATH={tmp_path / 'data'}\nINDEX_ORDER=found\n", encoding="utf-8")
    settings = load_settings(env)
    tuned = SimpleNamespace(workers=1, embed_batch=8, gpu_regression_notice="")
    config = build_pipeline_config(settings, [tmp_path], tuned=tuned)
    assert config.read_order == "found"
    config = build_pipeline_config(settings, [tmp_path], tuned=tuned,
                                   read_order="newest", first=("D:/Work",))
    assert config.read_order == "newest"
    assert [p.as_posix() for p in config.walk.priority_roots] == ["D:/Work"]

    with SqliteStore(tmp_path / "index.db") as store:
        assert _saved_first_folders(store) == []
        store.set_state(FIRST_FOLDERS_STATE_KEY, dump_first_folders(["D:/B", "D:/A"]))
        assert _saved_first_folders(store) == ["D:/B", "D:/A"]


def test_the_separate_process_is_told_the_first_folders_in_order() -> None:
    from app.index.child_run import child_command

    argv = child_command(["D:/All"], first=["D:/B", "D:/A"])
    pairs = [argv[i + 1] for i, word in enumerate(argv) if word == "--first"]
    assert [Path(p).as_posix() for p in pairs] == ["D:/B", "D:/A"]
    assert argv.index("--first") < argv.index("--"), "options before the folders"
    assert "--first" not in child_command(["D:/All"])


def test_the_command_line_accepts_order() -> None:
    import argparse

    from app.cli.index import add_index_parser

    parser = argparse.ArgumentParser()
    add_index_parser(parser.add_subparsers(), argparse.ArgumentParser(add_help=False))
    assert parser.parse_args(["index", "--order", "found"]).order == "found"
    assert parser.parse_args(["index"]).order is None
    with pytest.raises(SystemExit):
        parser.parse_args(["index", "--order", "sideways"])


def test_every_found_file_is_listed_by_name_before_the_first_is_read(tmp_path: Path) -> None:
    """2026-10-04, the owner: "it should get the names first and then scan faces
    or text ... file list comes up first". When the first file is read, every
    file the scan found is already a row - Queued (`PENDING`) and findable by
    name - and the run then reads each one, as before.

    *2026-10-10 (W1):* the folders marked first are read while the rest is
    walked, so "the first file" here is the first one *outside* them - by
    then every name is listed - and a marked folder's names are all listed
    before the first of its files is read (the next test)."""
    root = tmp_path / "corpus"
    ages = _corpus(root)
    listed: list[tuple] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root)
        original = pipeline._write_one

        def write_one(item):
            outside = not item.candidate.path.relative_to(root).as_posix().startswith("chosen/")
            if outside and not listed:
                listed.extend(store.conn.execute(
                    "SELECT path, status FROM files ORDER BY path").fetchall())
            return original(item)

        pipeline._write_one = write_one
        stats = pipeline.run()
        assert len(listed) == len(ages)
        outside = {path: status for path, status in listed
                   if not Path(path).relative_to(root).as_posix().startswith("chosen/")}
        assert set(outside.values()) == {"PENDING"}
        assert store.search_files_by_name("y-new", limit=5), "found by name"
        assert stats.indexed == len(ages)
        statuses = {row[0] for row in store.conn.execute("SELECT status FROM files")}
    assert statuses == {"INDEXED"}, "every queued name was read"


def test_listing_names_never_touches_a_row_already_there(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "index.db") as store:
        kept = store.upsert_file("C:/a/report.txt", size_bytes=5, mtime_ns=1,
                                 status="INDEXED", content_hash="h")
        added = store.add_waiting_files([
            {"path": "C:/a/report.txt", "parent_dir": "C:/a", "ext": "txt",
             "size_bytes": 9, "mtime_ns": 2},
            {"path": "C:/a/new.txt", "parent_dir": "C:/a", "ext": "txt",
             "size_bytes": 3, "mtime_ns": 2}])
        assert added == 1
        record = store.get_file("C:/a/report.txt")
        assert (record.id, record.status, record.size_bytes) == (kept, "INDEXED", 5)
        assert store.get_file("C:/a/new.txt").status == "PENDING"


# ---------------------------------------------------------------------------
# 2026-10-10, review item W2: the scan's decision is carried to the file's turn
# ---------------------------------------------------------------------------

def test_a_deferred_hash_survives_the_spill_file(tmp_path: Path) -> None:
    """Written as a bare string it would come back as a digest - the old hash,
    stored on the row after the contents moved."""
    from app.index.walker import HashDeferred

    with WorkList(tmp_path, spill_at=1) as work:
        work.add(_candidate("a.txt", days_ago=1, size=1), HashDeferred("abc"))
        work.add(_candidate("b.txt", days_ago=1, size=2), "abc")
        assert work.spilled
        got = [d for _c, d in work.sorted()]
    assert got == [HashDeferred("abc"), "abc"]
    assert not isinstance(got[0], str)


def _count_row_lookups(store) -> list[str]:
    asked: list[str] = []
    real = store.get_file

    def get_file(key):
        asked.append(str(key))
        return real(key)

    store.get_file = get_file
    return asked


def _move_dates(root: Path, names: list[str], *, by_ns: int = 10 * DAY_NS) -> None:
    for name in names:
        stat = (root / name).stat()
        os.utime(root / name, ns=(stat.st_atime_ns, stat.st_mtime_ns + by_ns))


def test_each_file_s_row_is_looked_up_once_not_twice(tmp_path: Path) -> None:
    """The scan asked `_classify` with the hash held back, and the file's turn
    asked it all again - a second `get_file`, and a `.pst` header read, for
    every file to be read. Now once each, on the first run and on a rerun
    where dates moved."""
    root = tmp_path / "corpus"
    ages = _corpus(root)
    with SqliteStore(tmp_path / "index.db") as store:
        asked = _count_row_lookups(store)
        stats = _pipeline(store, root).run()
        assert stats.indexed == len(ages)
        file_keys = [key for key in asked if key.endswith(".txt")]
        assert sorted(file_keys) == sorted(str(root / name) for name in ages), (
            "one row lookup per file")

        moved = ["b-new.txt", "chosen/x-old.txt", "i-year.txt"]
        _move_dates(root, moved)
        asked.clear()
        again = _pipeline(store, root).run()
        file_keys = [key for key in asked if key.endswith(".txt")]
        assert len(file_keys) == len(set(file_keys)) == len(ages)
    assert again.indexed == 0
    assert again.unchanged == len(ages)


def test_a_moved_date_with_new_contents_is_read_again_with_its_new_hash(
        tmp_path: Path) -> None:
    """The deferred hash decides both ways: same bytes are unchanged, other
    bytes of the same size are read - and the row gets the *new* hash."""
    from app.index.walker import content_hash

    root = tmp_path / "corpus"
    ages = _corpus(root)
    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, root).run()
        target = root / "c-mid.txt"
        body = target.read_text(encoding="utf-8")
        stamp = target.stat().st_mtime_ns
        target.write_text(body.replace("pump", "pipe"), encoding="utf-8")
        os.utime(target, ns=(stamp, stamp + DAY_NS))
        _move_dates(root, ["a-old.txt"])
        reads: list[str] = []
        again = _pipeline(store, root)
        _record_reads(again, root, reads)
        stats = again.run()
        record = store.get_file(str(target))
    assert reads == ["c-mid.txt"]
    assert stats.unchanged == len(ages) - 1
    assert record.content_hash == content_hash(target)


# ---------------------------------------------------------------------------
# 2026-10-10, review item W4: a moved date is hashed by the readers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ["newest", "found"])
def test_a_moved_date_is_hashed_by_a_reader_not_the_walker(tmp_path: Path,
                                                           monkeypatch, order) -> None:
    """A `robocopy` restore moves every date and changes no byte. The hash
    that proves it was taken on the one walker thread, file after file; now
    each reader hashes the file it was given, and a match is counted
    unchanged and never read."""
    import threading

    from app.index import walker

    root = tmp_path / "corpus"
    ages = _corpus(root)
    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, root, read_order=order).run()
        moved = sorted(ages)[:6]
        _move_dates(root, moved)

        hashed_on: list[tuple[str, str]] = []
        real_hash = walker.content_hash

        def spy(path):
            hashed_on.append((Path(path).relative_to(root).as_posix(),
                              threading.current_thread().name))
            return real_hash(path)

        monkeypatch.setattr(walker, "content_hash", spy)
        reads: list[str] = []
        again = _pipeline(store, root, read_order=order)
        _record_reads(again, root, reads)
        stats = again.run()
    assert sorted(name for name, _thread in hashed_on) == sorted(moved), (
        "each moved file hashed once")
    assert all(thread != "walker" for _name, thread in hashed_on), hashed_on
    assert reads == [], "a matching hash is not read"
    assert stats.unchanged == len(ages)
    assert stats.indexed == 0


def test_a_reader_that_cannot_hash_the_file_reports_it_as_before(tmp_path: Path,
                                                                 monkeypatch) -> None:
    """A deferred hash that fails (the file is locked) is "changed, no hash",
    as `has_changed` answered: the reader then records the lock."""
    from app.index import walker
    from app.index.pipeline import UNCHANGED, HashDeferred

    root = tmp_path / "corpus"
    _corpus(root)
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root)
        candidate = Candidate(path=root / "a-old.txt", size_bytes=1, mtime_ns=1)
        real_hash = walker.content_hash
        assert pipeline._check_deferred_hash(
            candidate, HashDeferred(real_hash(candidate.path))) is UNCHANGED

        def locked(path):
            raise PermissionError(13, "in use", str(path))

        monkeypatch.setattr(walker, "content_hash", locked)
        assert pipeline._check_deferred_hash(candidate, HashDeferred("x")) is None


# ---------------------------------------------------------------------------
# 2026-10-10, review item W1: the folders marked first are read during the walk
# ---------------------------------------------------------------------------

def test_the_chosen_folder_is_read_while_the_rest_is_walked(tmp_path: Path,
                                                            monkeypatch) -> None:
    """The rest of the walk is held until a file from the marked folder has
    been written. On the code before W1 nothing was read until the walk had
    ended, so the hold ran out with nothing written. And the order a run
    reads in is still exactly the one sorted list's."""
    import threading

    from app.index import pipeline as pipeline_module

    root = tmp_path / "corpus"
    ages = _corpus(root)
    written = threading.Event()
    held: list[bool] = []
    real_walk = pipeline_module.walk

    def held_walk(*args, **kwargs):
        held.append(written.wait(timeout=30))
        yield from real_walk(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "walk", held_walk)
    reads: list[str] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root)
        _record_reads(pipeline, root, reads)
        original = pipeline._write_one

        def write_one(item):
            out = original(item)
            written.set()
            return out

        pipeline._write_one = write_one
        stats = pipeline.run()
    assert held == [True], "a marked file was read before the rest of the walk"
    assert reads == _expected_order(root, ages)
    assert stats.indexed == len(ages)


def test_a_marked_folder_s_names_are_listed_before_its_first_file_is_read(
        tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _corpus(root)
    listed: list[str] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root)
        original = pipeline._write_one

        def write_one(item):
            if not listed:
                listed.extend(row[0] for row in store.conn.execute("SELECT path FROM files"))
            return original(item)

        pipeline._write_one = write_one
        pipeline.run()
    chosen = {str(root / "chosen" / name) for name in ("x-old.txt", "y-new.txt")}
    assert chosen <= set(listed)


def test_an_interrupted_run_with_a_marked_folder_resumes_in_order(tmp_path: Path) -> None:
    """Stopped inside the marked folder: the next run reads the rest of it
    first, then everything else newest first."""
    root = tmp_path / "corpus"
    ages = _corpus(root)
    expected = _expected_order(root, ages)
    first: list[str] = []
    second: list[str] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root)
        _record_reads(pipeline, root, first, stop_after=1)
        pipeline.run()
        assert first and first == expected[:len(first)]
        again = _pipeline(store, root)
        _record_reads(again, root, second)
        again.run()
    assert first + second == expected
