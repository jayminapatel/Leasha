"""Work order `dates-live-log-and-interrupted-runs` 3b and 3d: an archive carries on
at the folder it was in.

    3b  a PST resumes inside itself at folder granularity: the folder cursor is
        persisted before it is needed (non-negotiable #4) through the existing
        `resume:` mechanism, and the next run starts at the first unfinished
        folder. Build it for the libpff backend first.
    3d  an index stopped mid-archive and then resumed ends with exactly the
        message set of an uninterrupted run, with no duplicates and no gaps.

Two halves. The first drives the **real** `pst_libpff.read_archive`, the real
`PstExtractor` and a real `Pipeline` over a fake `pypff` (the shape
`test_pst_libpff.py` already mirrors), because a fake can hold exactly the
awkward cases - an attachment mailed twice in two folders, a message that will
not read, a folder the settings skip - and can count which folders were opened.
The second runs the same comparison against a **real archive** through the real
library, when one is available: `LEASHA_TEST_PST`, or any `.pst` in
`tests/fixtures/email/`. None is committed (see the note on that test).

"The message set" is every row the archive produced - messages and the
attachments read out of them - by key and by text hash. "No duplicates" is that
set having as many members as there are rows; "no gaps" is it equalling the
uninterrupted run's.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from app.extract import email_pst, pst_libpff
from app.index import pipeline as pipeline_module
from app.index.pipeline import ARCHIVE_RESUME_PREFIX, Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_index_freshness import NullVectors, fake_embedder, write_aged
from tests.unit.test_pst_libpff import FakeAttachment, FakeFolder, FakeMessage, install_fake

PROJECT = Path(__file__).resolve().parents[2]


# --- a fake archive with every awkward case in it ----------------------------

class CountingFolder(FakeFolder):
    """Counts message fetches, to prove a resumed read skipped a folder."""

    def __init__(self, *args, broken=(), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fetched = 0
        self._broken = set(broken)

    def get_sub_message(self, index):
        self.fetched += 1
        if index in self._broken:
            raise OSError("unable to read item")
        return super().get_sub_message(index)


def _message(n: int, *, attachments=()) -> FakeMessage:
    return FakeMessage(
        identifier=1000 + n, subject=f"Message {n}",
        plain=f"Barnsley Dairy note {n}: the HACCP review, item {n}.",
        headers=f"Message-ID: <m{n}@example.com>\nFrom: sam@example.com\n",
        attachments=attachments)


BRIEF = b"Commissioning brief for the northern pump station."
MINUTES = b"Minutes of the autumn shutdown meeting."


def build_tree(*, broken_in_barnsley=()) -> dict[str, CountingFolder]:
    """Depth-first numbering: root 0, Inbox 1, Projects 2, Barnsley 3,
    Deleted Items 4 (skipped by default), Sent Items 5."""
    inbox = CountingFolder("Inbox", messages=[
        _message(1), _message(2, attachments=[FakeAttachment("brief.txt", BRIEF)]),
        _message(3)])
    barnsley = CountingFolder("Barnsley", messages=[_message(4), _message(5)],
                              broken=broken_in_barnsley)
    projects = CountingFolder("Projects", messages=[_message(6)], children=[barnsley])
    deleted = CountingFolder("Deleted Items", messages=[_message(7)])
    sent = CountingFolder("Sent Items", messages=[
        # The same brief again: an uninterrupted read reads it once, in Inbox.
        _message(8, attachments=[FakeAttachment("brief-again.txt", BRIEF)]),
        _message(9, attachments=[FakeAttachment("minutes.txt", MINUTES)])])
    root = CountingFolder("Top of Personal Folders",
                          children=[inbox, projects, deleted, sent])
    return {"root": root, "inbox": inbox, "projects": projects, "barnsley": barnsley,
            "deleted": deleted, "sent": sent}


@pytest.fixture()
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "mail"
    root.mkdir()
    write_aged(root / "2007.pst", b"!BDN" + b"\0" * 4092)
    return root


@pytest.fixture(autouse=True)
def _libpff_backend(monkeypatch):
    """The registered extractor, on `auto` - which picks libpff when it imports."""
    monkeypatch.setattr(email_pst.PstExtractor, "backend", email_pst.PstBackend.AUTO)
    monkeypatch.setattr(email_pst.PstExtractor, "session_factory", None)
    # Every boundary is a write here; the interval is measured in the real run.
    monkeypatch.setattr(pipeline_module, "RESUME_PERSIST_S", 0.0)


def index(db: Path, root: Path, *, stop_after: int = 0):
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                checkpoint_every=1)
        pipeline = Pipeline(store, NullVectors(), fake_embedder(), config)

        def maybe_stop(stats) -> None:
            if stop_after and stats.indexed >= stop_after:
                pipeline.request_stop()

        return pipeline.run(on_progress=maybe_stop if stop_after else None)


def message_set(db: Path) -> tuple[set[tuple[str, str]], int]:
    """`({(key, text hash)}, rows)` for everything the archive produced."""
    with SqliteStore(db) as store:
        rows = store.conn.execute(
            "SELECT path, content_hash FROM files WHERE source_kind = 'pst_message'"
        ).fetchall()
    return {(row["path"], row["content_hash"]) for row in rows}, len(rows)


def cursors(db: Path) -> dict[str, dict]:
    with SqliteStore(db) as store:
        return {key: json.loads(value) for key, value in store.all_state().items()
                if key.startswith(ARCHIVE_RESUME_PREFIX)}


def uninterrupted(tmp_path: Path, root: Path, monkeypatch) -> set[tuple[str, str]]:
    install_fake(monkeypatch, build_tree()["root"])
    db = tmp_path / "whole.db"
    index(db, root)
    found, rows = message_set(db)
    assert rows == len(found) == 10, "8 messages (Deleted Items is skipped), 2 attachments"
    return found


# --- stopped and resumed == uninterrupted ------------------------------------

@pytest.mark.parametrize("stop_after", [1, 3, 5, 6, 8, 9])
def test_stopped_mid_archive_then_resumed_is_exactly_the_uninterrupted_set(
        tmp_path, archive, monkeypatch, stop_after) -> None:
    whole = uninterrupted(tmp_path, archive, monkeypatch)

    db = tmp_path / "index.db"
    install_fake(monkeypatch, build_tree()["root"])
    first = index(db, archive, stop_after=stop_after)
    assert first.indexed == stop_after, "the stop did not land where the test put it"
    [cursor] = cursors(db).values()               # the stop left a cursor behind
    assert cursor["path"].endswith("2007.pst")

    tree = build_tree()
    install_fake(monkeypatch, tree["root"])
    index(db, archive)

    found, rows = message_set(db)
    assert rows == len(found), "a resumed read wrote a document twice"
    assert found == whole, (
        f"missing {sorted(whole - found)}, extra {sorted(found - whole)}")
    assert cursors(db) == {}, "a finished archive must not keep its cursor"
    if cursor["folder"] > 1:
        assert tree["inbox"].fetched == 0, "a resumed read re-read a finished folder"


def test_stopped_twice_forgets_nothing_the_first_stop_knew(
        tmp_path, archive, monkeypatch) -> None:
    """The brief is read in Inbox by the first run. The second run resumes
    past Inbox and stops again; the third must still know the brief was read,
    or Sent Items' copy of it becomes a document an uninterrupted run never had."""
    whole = uninterrupted(tmp_path, archive, monkeypatch)
    db = tmp_path / "index.db"
    install_fake(monkeypatch, build_tree()["root"])
    index(db, archive, stop_after=3)             # the brief has settled
    install_fake(monkeypatch, build_tree()["root"])
    index(db, archive, stop_after=3)             # m6, m4, m5: into Barnsley
    [cursor] = cursors(db).values()
    assert cursor["folder"] == 3 and len(cursor["seen"]) == 1

    install_fake(monkeypatch, build_tree()["root"])
    index(db, archive)
    found, rows = message_set(db)
    assert rows == len(found) and found == whole


def test_the_cursor_is_written_during_the_run_not_only_at_its_end(
        tmp_path, archive, monkeypatch) -> None:
    """A pulled plug runs no `finally`. The cursor has to be there already."""
    install_fake(monkeypatch, build_tree()["root"])
    seen_mid_run: list[dict] = []
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[archive]), workers=1,
                                checkpoint_every=1)
        pipeline = Pipeline(store, NullVectors(), fake_embedder(), config)

        def look(stats) -> None:
            if stats.indexed == 7:                # into Barnsley, folder 3
                seen_mid_run.extend(
                    json.loads(value) for key, value in store.all_state().items()
                    if key.startswith(ARCHIVE_RESUME_PREFIX))

        pipeline.run(on_progress=look)

    assert seen_mid_run, "no folder cursor existed while the archive was being read"
    assert seen_mid_run[0]["folder"] >= 3


def test_a_killed_run_resumes_from_the_cursor_it_wrote(tmp_path, archive, monkeypatch) -> None:
    """The same, with no clean end at all: the cursor from a boundary is all
    there is, and carrying on from it still gives exactly the whole set."""
    whole = uninterrupted(tmp_path, archive, monkeypatch)
    install_fake(monkeypatch, build_tree()["root"])
    db = tmp_path / "index.db"

    class Killed(BaseException):
        """Not an `Exception`: nothing in the pipeline may catch it, which is
        as near as one process gets to the power going."""

    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[archive]), workers=1,
                                checkpoint_every=1)
        pipeline = Pipeline(store, NullVectors(), fake_embedder(), config)

        def die(stats) -> None:
            if stats.indexed == 7:
                raise Killed

        with pytest.raises(Killed):
            pipeline.run(on_progress=die)
    assert cursors(db), "nothing was written before the kill"

    tree = build_tree()
    install_fake(monkeypatch, tree["root"])
    index(db, archive)
    found, rows = message_set(db)
    assert rows == len(found) and found == whole
    assert tree["inbox"].fetched == 0


def test_a_changed_archive_is_read_from_the_start(tmp_path, archive, monkeypatch) -> None:
    """Folder numbers describe one tree; after a write they may describe another."""
    install_fake(monkeypatch, build_tree()["root"])
    db = tmp_path / "index.db"
    index(db, archive, stop_after=6)
    assert cursors(db)

    write_aged(archive / "2007.pst", b"!BDN" + b"\0" * 8188)     # a new size
    tree = build_tree()
    install_fake(monkeypatch, tree["root"])
    index(db, archive)
    assert tree["inbox"].fetched == 3, "a changed archive resumed into a stale cursor"
    assert cursors(db) == {}


def test_an_unreadable_cursor_is_a_read_from_the_top(tmp_path, archive, monkeypatch) -> None:
    install_fake(monkeypatch, build_tree()["root"])
    db = tmp_path / "index.db"
    index(db, archive, stop_after=6)
    [key] = cursors(db)
    with SqliteStore(db) as store:
        store.set_state(key, "{torn")

    tree = build_tree()
    install_fake(monkeypatch, tree["root"])
    stats = index(db, archive)
    assert tree["inbox"].fetched == 3
    assert stats.skipped == 0


# --- the partial-read rules of order 0v stay true -----------------------------

def test_a_damaged_folder_is_never_resumed_past(tmp_path, archive, monkeypatch) -> None:
    """The cursor stops at the first failure, so the resumed read meets the
    damage again and `ERR_PST_PARTIAL` is still counted for the whole archive."""
    install_fake(monkeypatch, build_tree(broken_in_barnsley={0})["root"])
    whole_db = tmp_path / "whole.db"
    whole_stats = index(whole_db, archive)
    assert whole_stats.warned_by_code.get("ERR_PST_PARTIAL") == 1
    whole, _ = message_set(whole_db)

    db = tmp_path / "index.db"
    install_fake(monkeypatch, build_tree(broken_in_barnsley={0})["root"])
    index(db, archive, stop_after=8)               # well past the damage, in Sent Items
    [cursor] = cursors(db).values()
    assert cursor["folder"] <= 3, "the cursor moved past a folder with a failure in it"

    tree = build_tree(broken_in_barnsley={0})
    install_fake(monkeypatch, tree["root"])
    second = index(db, archive)
    assert second.warned_by_code.get("ERR_PST_PARTIAL") == 1
    assert tree["barnsley"].fetched == 2, "the damage was not walked over again"
    found, rows = message_set(db)
    assert rows == len(found) and found == whole


def test_the_partial_summary_counts_the_whole_archive_after_a_resume(monkeypatch) -> None:
    """ "(N were)" is about the archive, not about the part read last."""
    tree = build_tree(broken_in_barnsley={1})
    install_fake(monkeypatch, tree["root"])
    everything = list(pst_libpff.read_archive(Path("2007.pst")))
    whole_warning = everything[-1].warnings[-1].message

    # Where a cursor may stand: stamped documents only, and never past the failure.
    stamped = [d for d in everything if pst_libpff.FOLDER_META_KEY in d.meta]
    last = stamped[-1]
    assert last.meta[pst_libpff.FOLDER_META_KEY] == 3
    assert not [d for d in everything[len(stamped):]
                if pst_libpff.FOLDER_META_KEY in d.meta]

    install_fake(monkeypatch, build_tree(broken_in_barnsley={1})["root"])
    resumed = list(pst_libpff.read_archive(
        Path("2007.pst"), resume_from=3,
        read_before=last.meta[pst_libpff.READ_BEFORE_META_KEY],
        seen_attachments=[pst_libpff._hash_bytes(BRIEF)]))
    assert resumed[-1].warnings[-1].message == whole_warning
    assert "1 message could not be read (7 were)" in whole_warning


def test_an_attachment_first_seen_in_a_skipped_folder_is_still_read_once(monkeypatch) -> None:
    install_fake(monkeypatch, build_tree()["root"])
    resumed = list(pst_libpff.read_archive(
        Path("2007.pst"), resume_from=5,
        seen_attachments=[pst_libpff._hash_bytes(BRIEF)]))
    names = [d.meta.get("attachment_name") for d in resumed if d.meta.get("attachment_of")]
    assert names == ["minutes.txt"]


def test_the_outlook_path_takes_no_cursor(monkeypatch) -> None:
    """Its folder order is not provably the same between reads - see the
    note on `PstExtractor.supports_resume`. Handed one, it reads everything."""
    calls: list[dict] = []

    class Session:
        def stores(self):
            calls.append({})
            return iter(())

    extractor = email_pst.PstExtractor()
    extractor.session_factory = Session
    list(extractor.extract(Path("2007.pst"), resume_from=4, resume_extra={"read": 3}))
    assert calls == [{}]


# --- a real archive, when there is one -----------------------------------------

def _real_archives() -> list[Path]:
    found = [Path(p) for p in os.environ.get("LEASHA_TEST_PST", "").split(os.pathsep) if p]
    found += sorted((PROJECT / "tests" / "fixtures" / "email").glob("*.pst"))
    return [p for p in found if p.is_file()]


@pytest.mark.slow
@pytest.mark.parametrize("real", _real_archives() or [None],
                         ids=lambda p: p.name if p else "none")
def test_a_real_archive_stopped_and_resumed_is_the_uninterrupted_set(
        tmp_path, monkeypatch, real) -> None:
    """**No real archive is committed.** A `.pst` is either somebody's mail or a
    third party's file with its own licence, and the useful public samples are
    megabytes. Point `LEASHA_TEST_PST` at one (a copy - it is only read), or
    drop it in `tests/fixtures/email/`, and this runs against the real library.
    """
    if real is None:
        pytest.skip("no real .pst: set LEASHA_TEST_PST or add one to tests/fixtures/email/")
    pytest.importorskip("pypff")
    monkeypatch.delitem(sys.modules, "pypff", raising=False)
    root = tmp_path / "mail"
    root.mkdir()
    target = root / real.name
    shutil.copy2(real, target)
    write_aged(target, target.read_bytes())

    whole_db = tmp_path / "whole.db"
    index(whole_db, root)
    whole, rows = message_set(whole_db)
    assert rows == len(whole) and whole, "the real archive produced nothing"

    # Stop about half-way, by documents; the cursor must have got past folder 0.
    db = tmp_path / "index.db"
    index(db, root, stop_after=max(1, len(whole) // 2))
    [cursor] = cursors(db).values()
    index(db, root)

    found, rows = message_set(db)
    assert rows == len(found), "a resumed read wrote a document twice"
    assert found == whole, f"missing {len(whole - found)}, extra {len(found - whole)}"
    assert cursors(db) == {}
    print(f"{real.name}: {len(whole)} documents; stopped at folder {cursor['folder']}")
