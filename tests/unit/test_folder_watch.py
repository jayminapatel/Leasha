r"""The folder watch: gathering changes, the walker's rules for one path, the
two sources, and what happens to a batch that cannot be applied.

Layer: L3

Work order 0z, item F1 (`app/index/folder_watch.py`). Everything here runs
without a model and without waiting: time is a number the test moves, Windows'
change notifications are a fake object, and "put the batch in the index" is a
function the test supplies. The real thing - a file saved in a real folder and
found through the real pipeline - is `tests/integration/
test_folder_watch_acceptance.py`.
"""

from __future__ import annotations

import inspect
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from app.core.errors import AppErrorException, make_error
from app.core.osbridge import dirwatch
from app.core.run_lock import IndexRunLock
from app.index import folder_watch as fw
from app.index import walker
from app.index.folder_watch import (
    Batch, BatchIndexer, BatchResult, Change, ChangeBuffer, FolderWatcher,
    IndexBusy, NativeSource, PollingSource,
)
from app.index.walker import PathRules, WalkConfig, walk
from app.storage.sqlite_store import SqliteStore


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def rules_for(root: Path, **overrides) -> PathRules:
    return PathRules(WalkConfig(roots=[root], **overrides))


# ---------------------------------------------------------------------------
# ChangeBuffer: bursts are gathered, and each path waits to go quiet
# ---------------------------------------------------------------------------

def test_an_editors_burst_on_one_file_is_one_change(clock, tmp_path):
    """A save is several events. They come out as one path, flags merged."""
    buffer = ChangeBuffer(clock=clock)
    doc = tmp_path / "report.docx"
    buffer.add(tmp_path, doc, renamed_from=True)     # renamed away to a backup
    clock.tick(0.05)
    buffer.add(tmp_path, doc, new=True)              # the temporary file renamed in
    clock.tick(0.05)
    buffer.add(tmp_path, doc)                        # and written to

    assert buffer.take() is None, "still inside the quiet time"
    clock.tick(fw.QUIET_S)
    batch = buffer.take()

    assert [change.path for change in batch.changes] == [doc]
    assert batch.changes[0].new and batch.changes[0].renamed_from
    assert buffer.take() is None and len(buffer) == 0


def test_each_path_waits_for_its_own_quiet(clock, tmp_path):
    """A file still being written does not hold back one that is finished."""
    buffer = ChangeBuffer(clock=clock)
    done, busy = tmp_path / "done.txt", tmp_path / "copying.bin"
    buffer.add(tmp_path, done)
    buffer.add(tmp_path, busy)
    for _ in range(4):
        clock.tick(fw.QUIET_S / 2)
        buffer.add(tmp_path, busy)                   # never quiet

    batch = buffer.take()

    assert [change.path for change in batch.changes] == [done]
    assert len(buffer) == 1


def test_a_path_that_never_goes_quiet_is_taken_anyway(clock, tmp_path):
    buffer = ChangeBuffer(clock=clock)
    log_file = tmp_path / "running.log"
    buffer.add(tmp_path, log_file)
    waited = 0.0
    while waited < fw.MAX_WAIT_S:
        clock.tick(1.0)
        waited += 1.0
        buffer.add(tmp_path, log_file)
        if waited < fw.MAX_WAIT_S:
            assert buffer.take() is None

    assert [change.path for change in buffer.take().changes] == [log_file]


def test_too_many_paths_under_one_folder_become_one_rescan(clock, tmp_path):
    """A large copy: the list is dropped for "look at the whole folder" -
    never silence - and another folder's paths are untouched."""
    big, other = tmp_path / "big", tmp_path / "other"
    buffer = ChangeBuffer(clock=clock, max_paths=50)
    buffer.add(other, other / "kept.txt")
    for number in range(60):
        buffer.add(big, big / f"file{number}.txt")

    assert buffer.overflows == 1
    # Later changes under it only say the folder is still busy.
    clock.tick(fw.QUIET_S - 0.5)
    buffer.add(big, big / "late.txt")
    clock.tick(0.5)
    first = buffer.take()
    assert first.rescan == [] and [c.path for c in first.changes] == [other / "kept.txt"]

    clock.tick(fw.QUIET_S)
    second = buffer.take()
    assert second.rescan == [big] and second.changes == []


def test_an_overflow_from_the_system_is_a_rescan(clock, tmp_path):
    buffer = ChangeBuffer(clock=clock)
    buffer.add(tmp_path, tmp_path / "a.txt")
    buffer.overflow(tmp_path)
    clock.tick(fw.QUIET_S)

    batch = buffer.take()

    assert batch.rescan == [tmp_path] and batch.changes == []


def test_a_batch_put_back_is_kept_and_waits(clock, tmp_path):
    """Held: nothing at all is handed over until the delay has passed."""
    buffer = ChangeBuffer(clock=clock)
    buffer.add(tmp_path, tmp_path / "a.txt")
    clock.tick(fw.QUIET_S)
    batch = buffer.take()

    buffer.restore(batch, delay_s=20.0, hold=True)
    buffer.add(tmp_path, tmp_path / "b.txt")
    clock.tick(fw.QUIET_S)
    assert buffer.take() is None, "an index run has the lock: everything waits"
    clock.tick(20.0)

    assert {c.path.name for c in buffer.take().changes} == {"a.txt", "b.txt"}


def test_a_locked_file_waiting_does_not_hold_back_other_files(clock, tmp_path):
    """Not held: only the paths put back wait."""
    buffer = ChangeBuffer(clock=clock)
    buffer.restore(Batch(changes=[Change(tmp_path, tmp_path / "locked.docx", attempts=1)]),
                   delay_s=30.0)
    buffer.add(tmp_path, tmp_path / "other.txt")
    clock.tick(fw.QUIET_S)

    assert [c.path.name for c in buffer.take().changes] == ["other.txt"]
    clock.tick(30.0)
    again = buffer.take()
    assert [c.path.name for c in again.changes] == ["locked.docx"]
    assert again.changes[0].attempts == 1


def test_a_rescan_put_back_comes_out_as_a_rescan(clock, tmp_path):
    buffer = ChangeBuffer(clock=clock)
    buffer.restore(Batch(rescan=[tmp_path]), delay_s=5.0)
    assert buffer.take() is None
    clock.tick(5.0)
    assert buffer.take().rescan == [tmp_path]


# ---------------------------------------------------------------------------
# PathRules: the walker's decisions, one path at a time
# ---------------------------------------------------------------------------

def _tree(root: Path) -> None:
    files = {
        "a.txt": "alpha", "notes.md": "beta", "empty.txt": "",
        "film.mp4": "not really a film", "~$lock.docx": "x", "scratch.tmp": "x",
        "Makefile": "all:\n", "build": "a file, not a folder",
        "sub/deep/b.txt": "gamma", "sub/Thumbs.db": "x",
        "node_modules/pkg/index.txt": "x", ".git/config": "x",
        "sub/AppData/hidden.txt": "x", "sub/~$folder/inside.txt": "x",
        "forced/node_modules/kept.txt": "kept",
        "logs/own.txt": "the application's own folder",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("name_only", [True, False])
def test_one_path_at_a_time_agrees_with_the_walk(tmp_path, name_only):
    """Every file under a tree, asked about singly, gets the answer `walk`
    gives - which is what stops the two copies of these rules drifting."""
    root = tmp_path / "root"
    _tree(root)
    config = WalkConfig(
        roots=[root], name_only=name_only,
        force_include=frozenset({str(root / "forced" / "node_modules")}),
        exclude_paths=frozenset({str(root / "logs")}))
    walked = {str(c.path): (c.readable, c.size_bytes, c.mtime_ns) for c in walk(config)}

    rules = PathRules(config)
    singly = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            candidate = rules.candidate(root, Path(folder) / name)
            if candidate is not None:
                singly[str(candidate.path)] = (
                    candidate.readable, candidate.size_bytes, candidate.mtime_ns)

    assert singly == walked
    assert str(root / "forced" / "node_modules" / "kept.txt") in walked
    assert str(root / "build") in walked or not name_only


def test_the_exclusions_are_answered_for_a_path_that_has_gone(tmp_path):
    """A deleted path cannot be looked at, so the answer comes from its name."""
    rules = rules_for(tmp_path)
    assert rules.excluded(tmp_path, tmp_path / "node_modules" / "x" / "gone.txt")
    assert rules.excluded(tmp_path, tmp_path / "docs" / "~$gone.docx")
    assert rules.excluded(tmp_path, tmp_path / "docs" / "gone.tmp")
    assert not rules.excluded(tmp_path, tmp_path / "docs" / "gone.txt")
    # A *file* called `build` is indexed; only a folder of that name is not.
    assert not rules.excluded(tmp_path, tmp_path / "build")
    assert rules.excluded(tmp_path, tmp_path / "build", is_dir=True)
    assert rules.excluded(tmp_path, tmp_path.parent / "elsewhere.txt")


def test_a_cloud_placeholder_is_never_read_even_in_an_opted_in_folder(tmp_path, monkeypatch):
    """`walk` may download inside a budget somebody set for a run; a watch
    that runs all day never does."""
    (tmp_path / "online.txt").write_text("would be downloaded", encoding="utf-8")
    monkeypatch.setattr(walker, "attributes_say_placeholder", lambda _bits: True)
    config = WalkConfig(
        roots=[tmp_path],
        cloud_content_roots=frozenset({str(tmp_path).rstrip("\\/").lower()}))

    assert [c.readable for c in walk(config)] == [True], "the walk may read it"
    candidate = PathRules(config).candidate(tmp_path, tmp_path / "online.txt")

    assert candidate is not None and candidate.readable is False


def test_the_mailbox_list_is_the_one_the_walk_names():
    source = inspect.getsource(walker.walk)
    for extension in walker.STREAMED_MAILBOXES:
        assert f'"{extension}"' in source


# ---------------------------------------------------------------------------
# NativeSource, with Windows' part replaced by a script
# ---------------------------------------------------------------------------

class ScriptedWatch:
    """Stands in for `dirwatch.DirectoryWatch`: each `read` returns the next
    item of the script (a list of records, None, or an OSError to raise)."""

    def __init__(self, script, stop: threading.Event) -> None:
        self.script = list(script)
        self.stop = stop
        self.closed = False

    def read(self, _timeout_s):
        if not self.script:
            self.stop.set()
            return None
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def run_native(root: Path, scripts, clock, **buffer_options):
    """Run a `NativeSource` over one `ScriptedWatch` per opening, to the end."""
    stop = threading.Event()
    buffer = ChangeBuffer(clock=clock, **buffer_options)
    problems = []
    pending = list(scripts)

    def open_watch(_root):
        if not pending:
            stop.set()
            raise OSError("no more")
        script = pending.pop(0)
        if isinstance(script, BaseException):
            raise script
        return ScriptedWatch(script, stop if not pending else threading.Event())

    source = NativeSource(
        root, buffer, rules=lambda: rules_for(root), stop=stop,
        on_problem=lambda folder, error: problems.append(error),
        open_watch=open_watch, reopen_s=0.0)
    source.run()
    return buffer, problems, source


def test_what_windows_reports_reaches_the_buffer_with_its_meaning(tmp_path, clock):
    records = [
        (dirwatch.ADDED, "new.txt"),
        (dirwatch.MODIFIED, "new.txt"),
        (dirwatch.RENAMED_FROM, "old name.docx"),
        (dirwatch.RENAMED_TO, "new name.docx"),
        (dirwatch.REMOVED, os.path.join("sub", "gone.txt")),
        (dirwatch.ADDED, "~$new name.docx"),             # Word's lock file
        (dirwatch.MODIFIED, os.path.join("node_modules", "pkg", "x.txt")),
    ]
    buffer, problems, source = run_native(tmp_path, [[records]], clock)
    clock.tick(fw.QUIET_S)

    found = {change.path.name: change for change in buffer.take().changes}

    assert set(found) == {"new.txt", "old name.docx", "new name.docx", "gone.txt"}
    assert found["new.txt"].new
    assert found["old name.docx"].renamed_from and not found["old name.docx"].new
    assert found["new name.docx"].new
    assert not found["gone.txt"].new
    assert source.ignored == 2 and problems == []


def test_a_lost_buffer_becomes_a_rescan_of_that_folder(tmp_path, clock):
    records = [(dirwatch.ADDED, "a.txt"), (dirwatch.OVERFLOW, "")]
    buffer, _problems, _source = run_native(tmp_path, [[records]], clock)
    clock.tick(fw.QUIET_S)

    batch = buffer.take()

    assert batch.rescan == [tmp_path] and batch.changes == []


def test_a_folder_that_cannot_be_watched_is_reported_and_tried_again(tmp_path, clock):
    """The error is an AppError; when the folder answers again the whole of it
    is looked at, because whatever happened meanwhile was not seen."""
    scripts = [
        [[(dirwatch.ADDED, "before.txt")], OSError(5, "Access is denied")],
        PermissionError(5, "Access is denied"),
        [[(dirwatch.ADDED, "after.txt")]],
    ]
    buffer, problems, _source = run_native(tmp_path, scripts, clock)

    codes = [error.code if error is not None else None for error in problems]
    assert codes[0] == "ERR_WATCH_FOLDER" and None in codes
    assert str(tmp_path) in problems[0].message and problems[0].suggestion
    clock.tick(fw.QUIET_S)
    assert buffer.take().rescan == [tmp_path]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own notifications")
def test_windows_really_reports_a_saved_file(tmp_path):
    """The real `ReadDirectoryChangesW`, in a temporary folder."""
    assert dirwatch.native_available()
    with dirwatch.DirectoryWatch(tmp_path) as watch:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "saved.txt").write_text("hello", encoding="utf-8")
        seen: list[tuple[str, str]] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            seen.extend(watch.read(0.5) or [])
            if any(name.endswith("saved.txt") for _action, name in seen):
                break
    assert (dirwatch.ADDED, "sub") in seen
    assert (dirwatch.ADDED, os.path.join("sub", "saved.txt")) in seen


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own notifications")
def test_closing_from_another_thread_ends_a_waiting_read(tmp_path):
    watch = dirwatch.DirectoryWatch(tmp_path)
    threading.Timer(0.2, watch.close).start()
    started = time.monotonic()

    assert watch.read(30.0) is None
    assert time.monotonic() - started < 5
    with pytest.raises(OSError):
        watch.read(0.1)


# ---------------------------------------------------------------------------
# PollingSource: every other system, and the fallback
# ---------------------------------------------------------------------------

def poller(root: Path, clock) -> tuple[PollingSource, ChangeBuffer, list]:
    buffer = ChangeBuffer(clock=clock)
    problems: list = []
    source = PollingSource(
        root, buffer, rules=lambda: rules_for(root), stop=threading.Event(),
        on_problem=lambda folder, error: problems.append(error))
    return source, buffer, problems


def test_comparing_finds_what_was_added_changed_and_removed(tmp_path, clock):
    (tmp_path / "stays.txt").write_text("same", encoding="utf-8")
    (tmp_path / "edited.txt").write_text("before", encoding="utf-8")
    (tmp_path / "deleted.txt").write_text("going", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    source, buffer, _problems = poller(tmp_path, clock)

    assert source.poll() == 0, "the first listing is only what later ones compare with"
    (tmp_path / "edited.txt").write_text("after, and longer", encoding="utf-8")
    (tmp_path / "deleted.txt").unlink()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "added.txt").write_text("new", encoding="utf-8")
    (tmp_path / "node_modules" / "noise.txt").write_text("x", encoding="utf-8")
    (tmp_path / "scratch.tmp").write_text("x", encoding="utf-8")

    assert source.poll() == 3
    clock.tick(fw.QUIET_S)
    found = {change.path.name: change for change in buffer.take().changes}

    assert set(found) == {"edited.txt", "deleted.txt", "added.txt"}
    assert found["added.txt"].new and not found["edited.txt"].new
    assert source.poll() == 0


def test_a_folder_that_vanishes_is_a_problem_not_a_thousand_deletions(tmp_path, clock):
    root = tmp_path / "usb"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    source, buffer, problems = poller(root, clock)
    source.poll()
    hidden = tmp_path / "unplugged"
    root.rename(hidden)

    assert source.poll() == 0
    assert problems[-1].code == "ERR_WATCH_FOLDER" and len(buffer) == 0

    hidden.rename(root)
    (root / "b.txt").write_text("b", encoding="utf-8")
    assert source.poll() == 1 and problems[-1] is None


def test_a_slow_listing_makes_the_next_one_wait_longer(tmp_path, clock):
    """At most a tenth of the time is spent listing, whatever the tree's size."""
    stop = threading.Event()
    source = PollingSource(tmp_path, ChangeBuffer(clock=clock),
                           rules=lambda: rules_for(tmp_path), stop=stop,
                           interval_s=30.0, clock=clock)
    waits: list[float] = []

    def fake_wait(seconds):
        waits.append(seconds)
        stop.set()
        return True

    stop.wait = fake_wait                       # type: ignore[method-assign]
    source.poll = lambda: setattr(source, "last_scan_s", 12.0) or 0  # type: ignore[method-assign]
    source.run()

    assert waits == [pytest.approx(120.0)]


# ---------------------------------------------------------------------------
# FolderWatcher: a batch that cannot be applied is kept
# ---------------------------------------------------------------------------

def watcher_with(apply, clock, tmp_path, events=None) -> FolderWatcher:
    return FolderWatcher(
        [tmp_path], apply=apply, rules=lambda: rules_for(tmp_path),
        buffer=ChangeBuffer(clock=clock),
        on_event=(lambda kind, data: events.append((kind, data)))
        if events is not None else None)


def test_a_batch_waits_while_an_index_run_has_the_lock(clock, tmp_path):
    calls: list[Batch] = []
    busy = [True]

    def apply(batch):
        calls.append(batch)
        if busy[0]:
            raise IndexBusy("the window, since 14:02")
        return BatchResult(indexed=1)

    events: list = []
    watcher = watcher_with(apply, clock, tmp_path, events)
    watcher.buffer.add(tmp_path, tmp_path / "a.txt")
    clock.tick(fw.QUIET_S)

    assert watcher.step() is None
    assert ("busy", {"reason": "the window, since 14:02", "count": 1}) in events
    assert len(watcher.buffer) == 1, "kept"
    clock.tick(fw.BUSY_RETRY_S - 1)
    assert watcher.step() is None and len(calls) == 1, "not asked again too soon"

    busy[0] = False
    clock.tick(2)
    result = watcher.step()

    assert result.indexed == 1 and len(watcher.buffer) == 0
    assert [c.path.name for c in calls[1].changes] == ["a.txt"]


def test_a_failed_batch_is_reported_kept_and_retried_more_slowly(clock, tmp_path):
    attempts: list[float] = []

    def apply(_batch):
        attempts.append(clock.now)
        raise RuntimeError("the disk said no")

    events: list = []
    watcher = watcher_with(apply, clock, tmp_path, events)
    watcher.buffer.add(tmp_path, tmp_path / "a.txt")
    clock.tick(fw.QUIET_S)
    watcher.step()

    kind, data = events[-1]
    assert kind == "error" and data["error"].code == "ERR_WATCH_UPDATE"
    assert data["error"].suggestion and "RuntimeError" in data["error"].details
    assert len(watcher.buffer) == 1

    for _ in range(3):
        clock.tick(fw.ERROR_RETRY_MAX_S)
        watcher.step()
    assert len(attempts) == 4 and len(watcher.buffer) == 1
    assert watcher._error_wait == min(fw.ERROR_RETRY_S * 16, fw.ERROR_RETRY_MAX_S)


def test_one_folder_failing_does_not_stop_the_others(clock, tmp_path):
    """A source that raises something unexpected ends alone, with an AppError."""
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    opened: list[str] = []
    ready = threading.Event()

    class Good:
        def read(self, timeout_s):
            ready.set()
            time.sleep(min(timeout_s, 0.05))
            return [(dirwatch.ADDED, "a.txt")]

        def close(self):
            pass

    def open_watch(root):
        opened.append(root)
        if root == str(bad):
            raise RuntimeError("something nobody planned for")
        return Good()

    events: list = []
    watcher = FolderWatcher(
        [bad, good], apply=lambda batch: BatchResult(), rules=lambda: rules_for(tmp_path),
        buffer=ChangeBuffer(clock=clock), open_watch=open_watch,
        on_event=lambda kind, data: events.append((kind, data)), tick_s=60.0)
    watcher.start()
    try:
        assert ready.wait(5)
        deadline = time.monotonic() + 5
        while len(watcher.buffer) == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(watcher.buffer) == 1
        assert str(bad) in watcher.problems()
    finally:
        watcher.stop(timeout_s=5)
    assert not watcher.running


def test_locked_files_are_offered_again_later(clock, tmp_path):
    locked = Change(tmp_path, tmp_path / "open in word.docx", attempts=1)
    results = [BatchResult(skipped=1, retry=[locked]), BatchResult(indexed=1)]
    seen: list[Batch] = []

    def apply(batch):
        seen.append(batch)
        return results.pop(0)

    watcher = watcher_with(apply, clock, tmp_path)
    watcher.buffer.add(tmp_path, locked.path)
    clock.tick(fw.QUIET_S)
    watcher.step()
    assert watcher.step() is None
    clock.tick(fw.LOCKED_RETRY_S)
    watcher.step()

    assert seen[1].changes[0].attempts == 1 and len(watcher.buffer) == 0


# ---------------------------------------------------------------------------
# BatchIndexer: what is decided before the run lock is taken
# ---------------------------------------------------------------------------

@pytest.fixture()
def lock_name() -> str:
    """A run-lock name of this test's own, so it cannot meet a real index run
    on the machine the suite is running on (the lock is machine-wide)."""
    return f"Leasha.Test.FolderWatch.{uuid.uuid4().hex}"


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index" / "index.db") as found:
        yield found


def indexer_for(store, root: Path, lock_name: str, tmp_path: Path, **options) -> BatchIndexer:
    from app.index.pipeline import PipelineConfig

    def never_called():
        raise AssertionError("nothing here should need the model")

    return BatchIndexer(
        store, vectors=None, embedder=options.pop("embedder", never_called),
        config=lambda roots: PipelineConfig(walk=WalkConfig(roots=list(roots))),
        lock_name=lock_name, lock_dir=tmp_path / "locks", **options)


def test_a_batch_that_needs_nothing_never_takes_the_lock(store, tmp_path, lock_name):
    """A folder whose contents changed, an excluded file, a path that was
    never indexed and has gone: none of them is work, and the lock - held here
    by "an index run" - is never asked for."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "scratch.tmp").write_text("x", encoding="utf-8")
    apply = indexer_for(store, root, lock_name, tmp_path)
    batch = Batch(changes=[
        Change(root, root / "sub"),                      # modified, not new
        Change(root, root / "scratch.tmp", new=True),
        Change(root, root / "never indexed.txt"),
        Change(tmp_path / "unplugged", tmp_path / "unplugged" / "a.txt"),
    ])

    with IndexRunLock(None, name=lock_name, lock_dir=tmp_path / "locks"):
        result = apply(batch)

    assert result.ignored == 4 and result.indexed == 0 and apply.batches == 0


def test_a_batch_with_work_is_refused_while_the_lock_is_held(store, tmp_path, lock_name):
    root = tmp_path / "root"
    root.mkdir()
    (root / "new.txt").write_text("something to index", encoding="utf-8")
    apply = indexer_for(store, root, lock_name, tmp_path)

    with IndexRunLock(None, name=lock_name, lock_dir=tmp_path / "locks"):
        with pytest.raises(IndexBusy) as refused:
            apply(Batch(changes=[Change(root, root / "new.txt", new=True)]))

    assert "already in progress" in refused.value.reason


def test_on_battery_the_batch_waits_without_holding_the_lock(store, tmp_path, lock_name):
    """The owner's "pause on battery" is honoured by waiting in the buffer, so
    a Start pressed meanwhile is not refused by a watch that is only waiting."""
    from app.index.resources import Snapshot

    root = tmp_path / "root"
    root.mkdir()
    (root / "new.txt").write_text("something to index", encoding="utf-8")
    apply = indexer_for(store, root, lock_name, tmp_path,
                        probe=lambda: Snapshot(on_battery=True))

    with pytest.raises(IndexBusy) as waiting:
        apply(Batch(changes=[Change(root, root / "new.txt", new=True)]))

    assert "battery" in waiting.value.reason.lower()
    IndexRunLock(None, name=lock_name, lock_dir=tmp_path / "locks").acquire().release()


def test_nothing_is_forgotten_when_the_indexed_folder_itself_is_missing(
        store, tmp_path, lock_name):
    """An unplugged drive makes every path under it "missing". That is not
    evidence that anything was deleted."""
    root = tmp_path / "usb"
    store.upsert_file(str(root / "a.txt"), size_bytes=1, mtime_ns=1)
    apply = indexer_for(store, root, lock_name, tmp_path)

    result = apply(Batch(changes=[Change(root, root / "a.txt")], rescan=[root]))

    assert result.removed == 0 and result.rescan_later == [root]
    assert store.get_file(str(root / "a.txt")) is not None


# ---------------------------------------------------------------------------
# The store accessor
# ---------------------------------------------------------------------------

def test_rows_at_or_under_a_path_are_found_by_the_index(store):
    r"""A folder's files, a zip's members and a mailbox's messages - and not a
    neighbour whose name merely starts the same, or has `%` and `_` in it."""
    rows = {
        r"D:\Docs\Old": None,
        r"D:\Docs\Old\a.txt": "in",
        r"D:\Docs\Old\sub\b.txt": "in",
        r"D:\Docs\Old.txt": "out",
        r"D:\Docs\Older\c.txt": "out",
        r"D:\Docs\Q1_2024%.pst": "exact",
        r"D:\Docs\Q1_2024%.pst#0001": "message",
        r"D:\Docs\Q1X2024Y.pst#0001": "out",
        r"D:\Docs\pack.zip\inner\d.txt": "member",
        "/home/me/docs/old/e.txt": "posix",
    }
    ids = {path: store.upsert_file(path, size_bytes=1, mtime_ns=1)
           for path, kind in rows.items() if kind is not None}

    under_old = set(store.file_ids_at_or_under("D:\\Docs\\Old\\"))
    assert under_old == {ids[r"D:\Docs\Old\a.txt"], ids[r"D:\Docs\Old\sub\b.txt"]}
    assert set(store.file_ids_at_or_under(r"D:\Docs\Q1_2024%.pst")) == {
        ids[r"D:\Docs\Q1_2024%.pst"], ids[r"D:\Docs\Q1_2024%.pst#0001"]}
    assert store.file_ids_at_or_under(r"D:\Docs\pack.zip") == [
        ids[r"D:\Docs\pack.zip\inner\d.txt"]]
    assert store.file_ids_at_or_under("/home/me/docs/old") == [ids["/home/me/docs/old/e.txt"]]
    assert store.file_ids_at_or_under("") == []

    plan = " ".join(str(tuple(row)) for row in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM files WHERE path >= ? AND path < ?",
        ("D:\\Docs\\Old\\", "D:\\Docs\\Old]")))
    assert "SEARCH" in plan and "SCAN" not in plan, plan


def test_the_errors_say_what_happened_and_what_to_do():
    for code, context in (
            ("ERR_WATCH_FOLDER", {"path": r"D:\Docs", "reason": "it has gone"}),
            ("ERR_WATCH_UPDATE", {"count": 3, "reason": "no space"})):
        error = make_error(code, "index.folder_watch", **context)
        assert error.code == code and "{" not in error.message
        assert error.suggestion and error.action_type
    with pytest.raises(AppErrorException):
        raise AppErrorException(make_error("ERR_WATCH_FOLDER", "t", path="p", reason="r"))
