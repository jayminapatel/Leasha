"""Order 0z lane C: a `.pst` read through libpff keeps going, says what each item became,
and leaves attached pictures for the pictures pass.

The owner's report: *"the pst scanning is way slow ... and is not reliable"* - it
stalls (progress stops, a count never moves) and stops (the run ends early, or
messages are failed or skipped). Each failure below was reproduced on damaged
copies of a real archive before it was fixed (`HANDOFF.md`, the 0z lane C note);
these tests hold the fixes on a fake `pypff`, so they run anywhere.

* **An attachment's reader raising anything** - not only `AppErrorException` -
  cost the rest of the archive. `xlrd` raised `struct.error` on a damaged `.xls`.
* **A damaged folder tree** could loop (a folder listing its own ancestor) or
  claim billions of messages, each failing in microseconds.
* **The status words** - Indexed, Skipped, Failed, Duplicate, Held - on the
  progress frame, per archive, and in the log when the archive ends.
* **Pictures attached to mail were read by OCR on the text-first pass**, and
  that was most of the time the read took.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from app.extract import base, email_pst, progress, pst_libpff, reading
from app.extract.base import Document
from app.index.activity import KIND_ARCHIVE_COUNTS, decode_counts, encode_counts
from app.index.held_archives import HELD_ARCHIVES_STATE_KEY
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.activity import activity_text, status_counts_text
from app.ui.presenter.live_progress import worker_lines
from tests.unit.test_index_freshness import NullVectors, fake_embedder, write_aged
from tests.unit.test_pst_libpff import FakeAttachment, FakeFolder, FakeMessage, install_fake


def _msg(n: int, **kwargs) -> FakeMessage:
    return FakeMessage(n, subject=f"Survey {n}", plain=f"Valve {n} needs replacing.",
                       headers=f"Message-ID: <m{n}@example.com>\nFrom: a@example.com\n",
                       **kwargs)


class _IdFolder(FakeFolder):
    """A fake folder with a libpff identifier, as the real one has."""

    def __init__(self, identifier, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._id = identifier

    def get_identifier(self):
        return self._id


class _Picture:
    """A stand-in OCR reader for `.fakepic`, counting what it was handed.

    Registered under the name `ocr`, which is what `reads_by_ocr` asks for -
    so the reader treats it exactly as a real picture, without loading OCR.
    """

    name = "ocr"
    extensions = (".fakepic",)
    calls: list[str] = []

    def extract(self, path: Path):
        type(self).calls.append(path.name)
        yield Document(path=path, text=f"Words read from the picture {path.name}.",
                       source_kind="file")


class _Explodes:
    """A reader that fails the way `xlrd` did on a damaged `.xls`: not an AppError."""

    name = "explodes"
    extensions = (".boom",)

    def extract(self, path: Path):
        raise struct.error("unpack requires a buffer of 4 bytes")
        yield  # pragma: no cover


@pytest.fixture(autouse=True)
def _readers(monkeypatch):
    before = dict(base.REGISTRY)
    _Picture.calls = []
    base.register(_Picture())
    base.register(_Explodes())
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


def _read(root, monkeypatch, *, images=reading.IMAGES_READ):
    install_fake(monkeypatch, root)
    with reading.reading(images=images) as policy:
        documents = list(pst_libpff.read_archive(Path("Archive2019.pst")))
    assert progress.frames() == [], "the archive's frame closed"
    return documents, policy


# --- one bad item costs one item ----------------------------------------------

def test_an_attachment_reader_raising_anything_costs_only_that_attachment(monkeypatch) -> None:
    """Fails on the code as it was: `struct.error` escaped and ended the archive
    after message 1 - reproduced on 29 of 150 damaged copies of a real archive."""
    inbox = FakeFolder("Inbox", messages=[
        _msg(1, attachments=[FakeAttachment("sheet.boom", b"damaged")]),
        _msg(2), _msg(3),
    ])
    documents, policy = _read(FakeFolder("", children=[inbox]), monkeypatch)
    assert [d.meta.get("subject") for d in documents] == ["Survey 1", "Survey 2", "Survey 3"]
    assert policy.counts == {"Indexed": 3, "Failed": 1}


def test_the_outlook_path_has_the_same_guard() -> None:
    """`email_pst._attachment_documents` caught only `AppErrorException` too."""
    from tests.unit.test_email_pst import FakeAttachment as MapiAttachment

    item = email_pst.MailItem(entry_id="E1", subject="Survey", body="Valves.", attachments=[
        MapiAttachment("sheet.boom", b"damaged"),
        MapiAttachment("notes.txt", b"pump station notes"),
    ])
    warnings: list = []
    documents = list(email_pst._attachment_documents(item, "pst://store/E1", set(),
                                                     warnings=warnings))
    assert [d.meta["attachment_name"] for d in documents] == ["notes.txt"]
    assert [w.code for w in warnings] == ["ERR_FILE_CORRUPT"]


def test_a_type_nothing_reads_is_skipped_without_being_written(monkeypatch, tmp_path) -> None:
    """Measured: a `.url` was written to disk, handed to `extract` and turned down,
    re-reading `extractors.toml` on the way. Now it is refused by name."""
    written = []
    real = pst_libpff._Scratch.write

    def spy(self, name, data):
        written.append(name)
        return real(self, name, data)

    monkeypatch.setattr(pst_libpff._Scratch, "write", spy)
    inbox = FakeFolder("Inbox", messages=[
        _msg(1, attachments=[FakeAttachment("link.url", b"[InternetShortcut]"),
                             FakeAttachment("notes.txt", b"pump station notes")]),
    ])
    documents, policy = _read(FakeFolder("", children=[inbox]), monkeypatch)
    assert written == ["notes.txt"]
    assert len(documents) == 2
    assert policy.counts == {"Indexed": 2, "Skipped": 1}


def test_each_attachment_is_fetched_once(monkeypatch) -> None:
    """`get_attachment` is the costliest libpff call without OCR (about 2ms each,
    measured) and was made twice per attachment: once for the name, once for
    the bytes."""
    fetched = []

    class Counting(FakeMessage):
        def get_attachment(self, index):
            fetched.append(index)
            return super().get_attachment(index)

    message = Counting(1, attachments=[FakeAttachment("a.txt", b"alpha text"),
                                       FakeAttachment("b.txt", b"beta text")])
    documents, _ = _read(FakeFolder("", children=[FakeFolder("Inbox", messages=[message])]),
                         monkeypatch)
    assert fetched == [0, 1]
    assert documents[0].meta["attachment_names"] == ["a.txt", "b.txt"]


def test_a_duplicate_attachment_is_counted_and_never_written(monkeypatch) -> None:
    inbox = FakeFolder("Inbox", messages=[
        _msg(1, attachments=[FakeAttachment("brief.txt", b"the same brief")]),
        _msg(2, attachments=[FakeAttachment("brief-again.txt", b"the same brief")]),
    ])
    documents, policy = _read(FakeFolder("", children=[inbox]), monkeypatch)
    assert len(documents) == 3
    assert policy.counts == {"Indexed": 3, "Duplicate": 1}


# --- a damaged folder tree ----------------------------------------------------

def test_a_folder_that_lists_its_own_ancestor_is_walked_once(monkeypatch) -> None:
    """A descriptor pointing back up the tree made the walk infinitely deep; the
    recursion error ended the archive. Now the loop is recorded and left."""
    top = _IdFolder(10, "Top of Personal Folders")
    inbox = _IdFolder(11, "Inbox", messages=[_msg(1)], children=[top])
    top._children = [inbox]
    root = _IdFolder(1, "", children=[top])

    documents, _policy = _read(root, monkeypatch)
    assert len(documents) == 1
    warning = documents[-1].warnings[0]
    assert warning.code == "ERR_PST_PARTIAL"
    assert "1 folder could not be read" in warning.context["reason"]


def test_nesting_without_end_stops_at_the_depth_limit(monkeypatch) -> None:
    """No identifiers to spot the loop by: the depth limit still ends it."""

    class Bottomless(FakeFolder):
        def get_number_of_sub_folders(self):
            return 1

        def get_sub_folder(self, index):
            return Bottomless("Again", messages=[])

    documents, _ = _read(FakeFolder("", children=[
        FakeFolder("Inbox", messages=[_msg(1)]), Bottomless("Deep")]), monkeypatch)
    assert len(documents) == 1
    assert documents[-1].warnings[0].code == "ERR_PST_PARTIAL"


def test_a_folder_claiming_a_billion_messages_is_left_after_a_run_of_failures(monkeypatch) -> None:
    """A lying count, every read failing fast: the loop spun for hours with the
    position rising and nothing read. The guard ends it."""

    class Lying(FakeFolder):
        def get_number_of_sub_messages(self):
            return 1_000_000_000

        def get_sub_message(self, index):
            if index < 2:
                return _msg(100 + index)
            raise OSError("unable to retrieve descriptor")

    documents, policy = _read(FakeFolder("", children=[
        Lying("Inbox"), FakeFolder("Sent Items", messages=[_msg(7)])]), monkeypatch)
    assert [d.meta.get("subject") for d in documents] == ["Survey 100", "Survey 101", "Survey 7"]
    assert policy.counts["Failed"] == pst_libpff.MAX_CONSECUTIVE_FAILURES
    assert "1 folder" in documents[-1].warnings[0].context["reason"]


def test_the_same_message_twice_is_one_document_and_a_duplicate(monkeypatch) -> None:
    same = _msg(5)
    documents, policy = _read(FakeFolder("", children=[
        FakeFolder("Inbox", messages=[same, same, _msg(6)])]), monkeypatch)
    assert [d.meta.get("subject") for d in documents] == ["Survey 5", "Survey 6"]
    assert policy.counts == {"Indexed": 2, "Duplicate": 1}
    assert not documents[-1].warnings, "a repeat is not damage worth a partial warning"


# --- the status words on the frame ------------------------------------------

def test_every_item_moves_the_frame_and_ends_as_one_word(monkeypatch) -> None:
    """`n` moves for every message, failed ones included, and `beat` for every
    message and attachment - so a per-file limit never mistakes a slow archive
    for a stalled one."""
    seen = []

    class Watched(FakeFolder):
        def get_sub_message(self, index):
            frame = progress.frames()[-1]
            seen.append((frame.n, frame.beat))
            if index == 1:
                raise OSError("bad block")
            return super().get_sub_message(index)

    inbox = Watched("Inbox", messages=[
        _msg(1, attachments=[FakeAttachment("a.txt", b"alpha"), FakeAttachment("b.url", b"x")]),
        None, FakeMessage(3, subject="", plain="", headers="", sender=""),
    ])
    install_fake(monkeypatch, FakeFolder("", children=[inbox]))
    list(pst_libpff.read_archive(Path("Archive2019.pst")))
    # message 1, then its two attachments, then the failure, then the empty one
    assert seen == [(1, 0), (2, 3), (3, 4)]
    assert reading.current().counts == {}, "outside a pipeline nothing is left behind"


def test_the_frame_carries_the_counts_as_plain_json(monkeypatch) -> None:
    frame = progress.Frame("pst", "Archive2019.pst", unit="message")
    assert "counts" not in frame.as_dict() and "beat" not in frame.as_dict()
    frame.count(progress.STATUS_INDEXED)
    frame.count(progress.STATUS_FAILED)
    record = json.loads(json.dumps(frame.as_dict()))
    assert record["counts"] == {"Indexed": 1, "Failed": 1}
    assert record["beat"] == 2


# --- pictures held on the text pass ---------------------------------------------

def _mail_with_a_picture() -> FakeFolder:
    return FakeFolder("", children=[FakeFolder("Inbox", messages=[
        _msg(1, attachments=[FakeAttachment("site-photo.fakepic", b"PICTURE"),
                             FakeAttachment("notes.txt", b"pump station notes")]),
    ])])


def test_the_text_pass_holds_an_attached_picture_and_keeps_its_name(monkeypatch) -> None:
    documents, policy = _read(_mail_with_a_picture(), monkeypatch, images=reading.IMAGES_HOLD)
    assert _Picture.calls == []
    assert policy.held == 1
    assert policy.counts == {"Indexed": 2, "Held": 1}
    assert documents[0].meta["attachment_names"] == ["site-photo.fakepic", "notes.txt"]


def test_without_a_pass_the_picture_is_read_as_before(monkeypatch) -> None:
    _read(_mail_with_a_picture(), monkeypatch)
    assert _Picture.calls == ["site-photo.fakepic"]


def test_the_pictures_pass_reads_only_the_pictures(monkeypatch) -> None:
    documents, _policy = _read(_mail_with_a_picture(), monkeypatch, images=reading.IMAGES_ONLY)
    assert _Picture.calls == ["site-photo.fakepic"]
    assert [d.virtual_path for d in documents] == [
        "pst://Archive2019/1/attachments/site-photo.fakepic"]
    assert pst_libpff.FOLDER_META_KEY not in documents[0].meta, (
        "the pictures pass must never move the text pass's resume cursor")


# --- through the pipeline -------------------------------------------------------

@pytest.fixture()
def mail_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(email_pst.PstExtractor, "backend", email_pst.PstBackend.AUTO)
    monkeypatch.setattr(email_pst.PstExtractor, "session_factory", None)
    root = tmp_path / "mail"
    root.mkdir()
    write_aged(root / "2019.pst", b"!BDN" + b"\0" * 4092)
    return root


def _run(db: Path, root: Path, mode: str):
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, ocr_mode=mode)
        stats = Pipeline(store, NullVectors(), fake_embedder(), config).run()
        held = store.get_state(HELD_ARCHIVES_STATE_KEY)
        rows = {row["path"]: row["status"] for row in store.conn.execute(
            "SELECT path, status FROM files").fetchall()}
    return stats, (json.loads(held) if held else []), rows


def test_held_pictures_are_read_by_the_pictures_pass_and_only_once(
        tmp_path, mail_root, monkeypatch) -> None:
    install_fake(monkeypatch, _mail_with_a_picture())
    db = tmp_path / "index.db"

    text, held, rows = _run(db, mail_root, "text")
    assert _Picture.calls == []
    assert held == [str(mail_root / "2019.pst")]
    picture_key = "pst://2019/1/attachments/site-photo.fakepic"
    assert picture_key not in rows
    [line] = [e for e in text.activity.entries() if e.kind == KIND_ARCHIVE_COUNTS]
    assert line.text == "2019.pst"
    assert decode_counts(line.detail) == {"Indexed": 2, "Held": 1}
    marker = rows[str(mail_root / "2019.pst")]

    install_fake(monkeypatch, _mail_with_a_picture())
    _images, held, rows = _run(db, mail_root, "images")
    assert _Picture.calls == ["site-photo.fakepic"]
    assert held == []
    assert picture_key in rows
    assert rows[str(mail_root / "2019.pst")] == marker, "the text pass's marker is untouched"

    install_fake(monkeypatch, _mail_with_a_picture())
    _run(db, mail_root, "images")
    assert _Picture.calls == ["site-photo.fakepic"], "read once, not every pictures pass"


# --- the words -------------------------------------------------------------------

def test_the_counts_read_as_single_words_in_the_owner_s_order() -> None:
    assert status_counts_text({"Failed": 3, "Indexed": 12400, "TimedOut": 1}) == (
        "12,400 Indexed · 3 Failed · 1 TimedOut")
    assert status_counts_text({"Indexed": 0}) == ""
    assert status_counts_text(None) == ""


def test_the_log_line_at_the_end_of_an_archive() -> None:
    class Entry:
        kind = KIND_ARCHIVE_COUNTS
        text = "Archive2019.pst"
        detail = encode_counts({"Indexed": 12400, "Failed": 3, "Duplicate": 12})

    assert activity_text(Entry()) == "Archive2019.pst: 12,400 Indexed · 3 Failed · 12 Duplicate"


def test_the_reader_line_shows_the_counts() -> None:
    stats = {"workers": {"1": {
        "file": "Archive2019.pst", "started_at": 100.0, "inner": [{
            "kind": "pst", "name": "Archive2019.pst", "unit": "message", "n": 12,
            "total": 40, "where": "Inbox", "stage": "messages", "detail": "",
            "counts": {"Indexed": 12400, "Failed": 3}, "beat": 12403}]}}}
    [line] = worker_lines(stats, now=300.0)
    assert line.startswith("Reader 1: Archive2019.pst")
    assert line.endswith("· 12,400 Indexed · 3 Failed · 3 min 20 s")


def test_files_inside_a_zipped_attachment_keep_their_own_keys(tmp_path) -> None:
    """2026-09-30, the owner's `2009.pst`: every file inside `marathon_AMS.zip`
    got the attachment's key, so the pipeline warned "duplicate key" and made
    them unique with `#10`, `#11`... Each member now keeps its path after the
    attachment's; a plain attachment keeps the plain key."""
    import zipfile

    from app.extract.archive import attachment_key
    from app.extract.base import extract

    saved = tmp_path / "marathon_AMS.zip"
    with zipfile.ZipFile(saved, "w") as archive:
        archive.writestr("docs/a.txt", "first member")
        archive.writestr("docs/b.txt", "second member")
    keys = [attachment_key("pst://2009/42", saved.name, d.virtual_path, saved)
            for d in extract(saved)]
    assert len(keys) == len(set(keys)) == 2
    assert all(k.startswith("pst://2009/42/attachments/marathon_AMS.zip/") for k in keys)
    plain = tmp_path / "report.txt"
    plain.write_text("x", encoding="utf-8")
    (doc,) = list(extract(plain))
    assert attachment_key("pst://2009/42", "report.txt", doc.virtual_path, plain) \
        == "pst://2009/42/attachments/report.txt"
