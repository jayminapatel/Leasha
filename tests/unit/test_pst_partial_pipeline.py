"""Work order `pst-resilience` 3e, 4a and 5a: a partial archive, seen by the pipeline.

Three things the extractor-level tests cannot show, because they live between an
archive's last message and the index:

* **3e** - the warning rides on the archive's last message, and on an incremental
  run that message is usually unchanged, so it took the `_already_current` skip
  and its warning was never counted. The summary said "no partial archive" over
  an archive that was short.
* **4a** - a partial read retries next pass when (and only when) the cause was
  transient. Damage is settled: re-reading it every run only skips the same
  messages again.
* **5a** - two archives read through Outlook at once must not take each other's
  skipped folders.

Fakes come from `test_index_freshness`: one archive file on disk, a few messages
inside, a real `Pipeline` over a scratch SQLite store, a fake embedder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import make_error
from app.extract import base, email_pst
from app.extract.base import Document
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_index_freshness import NullVectors, aged, fake_embedder, write_aged

MESSAGES = 4


class PartialArchive:
    """A `.pst` stand-in whose last message carries a partial-read warning."""

    name = "partial-archive"
    extensions = (".partialpst",)
    reads_externally = True

    #: None = a clean read; otherwise the `transient` flag of the warning.
    warn_transient: "bool | None" = None
    #: Counts opens, to prove an archive was (or was not) read again.
    opened = 0

    def extract(self, path: Path):
        type(self).opened += 1
        for n in range(MESSAGES):
            warnings = ()
            if n == MESSAGES - 1 and self.warn_transient is not None:
                extra = {"transient": True} if self.warn_transient else {}
                warnings = (make_error(
                    "ERR_PST_PARTIAL", "extract.pst", path=str(path),
                    reason="1 message could not be read", **extra),)
            yield Document(
                path=path,
                text=f"Message {n} about the Barnsley Dairy HACCP review.",
                source_kind="pst_message",
                virtual_path=f"pst://{path.name}/E{n}",
                meta={"entry_id": f"E{n}", "subject": f"Message {n}"},
                warnings=warnings,
            )


@pytest.fixture(autouse=True)
def _register(monkeypatch):
    before = dict(base.REGISTRY)
    base.REGISTRY.pop(".partialpst", None)
    PartialArchive.warn_transient = None
    PartialArchive.opened = 0
    base.register(PartialArchive())
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)
    PartialArchive.warn_transient = None


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    write_aged(root / "mail.partialpst", b"x" * 4096)
    return root


def run(db: Path, root: Path):
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1)
        return Pipeline(store, NullVectors(), fake_embedder(), config).run()


def touch_archive(root: Path, size: int) -> None:
    """A changed archive: a new size, so mtime granularity is irrelevant."""
    aged(write_aged(root / "mail.partialpst", b"y" * size))


# --- 3e: the warning on an unchanged last message -----------------------------

def test_a_partial_warning_is_counted_on_the_first_read(tmp_path, corpus) -> None:
    PartialArchive.warn_transient = False
    stats = run(tmp_path / "index.db", corpus)
    assert stats.warned_by_code == {"ERR_PST_PARTIAL": 1}


def test_a_partial_warning_on_an_unchanged_last_message_is_still_counted(
    tmp_path, corpus,
) -> None:
    """Fails on the code as it was: the second run counted nothing."""
    db = tmp_path / "index.db"
    PartialArchive.warn_transient = False
    run(db, corpus)

    touch_archive(corpus, 8192)                  # re-read; every message unchanged
    second = run(db, corpus)

    assert second.unchanged_documents == MESSAGES
    assert second.indexed == 0
    assert second.warned_by_code == {"ERR_PST_PARTIAL": 1}


def test_a_warning_is_never_counted_twice(tmp_path, corpus) -> None:
    """Counting before the `_already_current` check would double a written one."""
    PartialArchive.warn_transient = False
    stats = run(tmp_path / "index.db", corpus)
    assert stats.indexed == MESSAGES
    assert stats.warned_by_code["ERR_PST_PARTIAL"] == 1


# --- 4a: retry only what will pass -------------------------------------------

def test_a_transient_partial_read_is_retried_next_pass(tmp_path, corpus) -> None:
    db = tmp_path / "index.db"
    PartialArchive.warn_transient = True
    run(db, corpus)
    assert PartialArchive.opened == 1

    second = run(db, corpus)                     # nothing on disk changed
    assert PartialArchive.opened == 2, "Outlook was busy; the archive should be tried again"
    assert second.indexed == 0, "messages already indexed are not embedded a second time"
    assert second.unchanged_documents == MESSAGES


def test_a_transient_partial_read_settles_once_outlook_is_not_busy(tmp_path, corpus) -> None:
    db = tmp_path / "index.db"
    PartialArchive.warn_transient = True
    run(db, corpus)

    PartialArchive.warn_transient = None         # the retry reads everything
    run(db, corpus)
    assert PartialArchive.opened == 2

    run(db, corpus)                              # now it has its marker
    assert PartialArchive.opened == 2, "a clean read must settle the archive"


def test_a_damaged_archive_is_not_re_read_every_pass(tmp_path, corpus) -> None:
    """Real damage skips the same messages every time; retrying is pure cost."""
    db = tmp_path / "index.db"
    PartialArchive.warn_transient = False
    run(db, corpus)
    run(db, corpus)
    run(db, corpus)
    assert PartialArchive.opened == 1


# --- 5a: skipped folders belong to their own archive --------------------------

class _Store:
    def __init__(self, path: str) -> None:
        self.display_name = Path(path).stem
        self.file_path = path


class _Folder:
    path = "Inbox"


@pytest.fixture()
def _clean_busy():
    email_pst.drain_busy_folders()
    yield
    email_pst.drain_busy_folders()


def test_each_archive_drains_only_its_own_busy_folders(_clean_busy) -> None:
    email_pst._record_busy(_Store(r"D:\a\one.pst"), _Folder(), OSError("closed"))
    email_pst._record_busy(_Store(r"D:\a\two.pst"), _Folder(), OSError("closed"))
    email_pst._record_busy(_Store(r"D:\a\two.pst"), _Folder(), OSError("closed again"))

    assert len(email_pst.drain_busy_folders(Path(r"D:\a\one.pst"))) == 1
    assert len(email_pst.drain_busy_folders(Path(r"D:\a\one.pst"))) == 0, "drained means gone"
    assert len(email_pst.drain_busy_folders(Path(r"D:\a\two.pst"))) == 2


def test_the_archive_warning_names_only_its_own_folders(_clean_busy) -> None:
    email_pst._record_busy(_Store(r"D:\a\one.pst"), _Folder(), OSError("closed"))
    email_pst._record_busy(_Store(r"D:\a\two.pst"), _Folder(), OSError("closed"))

    assert email_pst._busy_warning(Path(r"D:\a\three.pst")) is None
    warning = email_pst._busy_warning(Path(r"D:\a\one.pst"))
    assert warning is not None and "1 folder could not be read" in warning.message
    assert warning.context["transient"] is True


def test_paths_that_differ_only_in_case_are_the_same_archive(_clean_busy) -> None:
    email_pst._record_busy(_Store(r"D:\Mail\Old.PST"), _Folder(), OSError("closed"))
    assert len(email_pst.drain_busy_folders(Path(r"d:\mail\old.pst"))) == 1


def test_draining_with_no_path_takes_everything(_clean_busy) -> None:
    """The CLI's `extract --mailbox` walks every store in one process."""
    email_pst._record_busy(_Store(r"D:\a\one.pst"), _Folder(), OSError("x"))
    email_pst._record_busy(_Store(r"D:\a\two.pst"), _Folder(), OSError("y"))
    assert len(email_pst.drain_busy_folders()) == 2
    assert email_pst.drain_busy_folders() == []
