r"""Progress you can read, inside one file too. Work order 0x section 3.

Layer: L2, L3 and L5.

**One 4GB mail archive is one file.** Until this existed the page could name
the file a reader had open and nothing more, so message 12 and message 90,000
looked the same, and four readers busy at once showed as one name flickering
between four files. What these pin:

* 3b - each reader (mbox, zip, `.olm`, `.pst` through libpff) reports "n of m"
  and the right names while it reads, nested readers stack outside-in, and a
  finished read leaves nothing behind;
* 3c - two readers busy at the same moment each get their own line, through a
  real `Pipeline.run`, and `current`/`current_item` still work;
* 3d - the heartbeat moves only when something moved, ticks arrive about once
  a second while a reader is busy, and the presenter's quiet warning starts
  exactly at `QUIET_AFTER_S` and never claims a hang;
* the words: headline, per-reader lines, the writer line;
* everything the snapshot carries is plain JSON.
"""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.extract import progress
from app.extract.archive import read_archive as read_zip
from app.extract.base import Document
from app.extract.email_mbox import MboxExtractor
from app.extract.email_olm import OlmExtractor
from app.index import live_progress
from app.index import pipeline as pipeline_module
from app.index.embedder import Embedder
from app.index.pipeline import IndexStats, Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.live_progress import (
    QUIET_AFTER_S,
    STAGE_WORDS,
    heartbeat_line,
    inner_trail,
    live_headline,
    live_view,
    position_text,
    since_text,
    worker_lines,
    writer_line,
)
from tests.unit.test_email_mbox import _write_generated_mbox
from tests.unit.test_email_olm import _olm
from tests.unit.test_pst_libpff import (
    FakeAttachment,
    FakeFolder,
    FakeMessage,
    install_fake,
)


@pytest.fixture(autouse=True)
def _fresh_stack():
    """Each test starts with this thread's frame stack empty, and leaves it so."""
    progress.frames().clear()
    yield
    progress.frames().clear()


def _innermost() -> dict:
    return progress.trail()[-1]


# ---------------------------------------------------------------------------
# 3b: readers
# ---------------------------------------------------------------------------

def test_mbox_reports_message_n_of_m(tmp_path: Path) -> None:
    mbox = tmp_path / "mail.mbox"
    _write_generated_mbox(mbox, 5)
    seen = []
    for _document in MboxExtractor().extract(mbox):
        frame = _innermost()
        seen.append((frame["n"], frame["total"]))
        assert frame["kind"] == "mbox" and frame["name"] == "mail.mbox"
        assert frame["unit"] == "message" and frame["stage"] == progress.STAGE_MESSAGES
    assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]
    assert progress.frames() == [], "a finished read must leave no frame behind"


def test_a_resumed_mbox_starts_counting_where_it_resumes(tmp_path: Path) -> None:
    mbox = tmp_path / "mail.mbox"
    _write_generated_mbox(mbox, 5)
    reader = MboxExtractor().extract(mbox, resume_from=3)
    next(iter(reader))
    assert (_innermost()["n"], _innermost()["total"]) == (4, 5)
    reader.close()
    assert progress.frames() == [], "an abandoned read must close its frame"


def test_zip_reports_member_n_of_m_and_its_name(tmp_path: Path) -> None:
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as out:
        for i in range(3):
            out.writestr(f"q3/note{i}.txt", f"pump station note number {i}")
    seen = []
    for _document in read_zip(archive):
        frame = _innermost()
        seen.append((frame["n"], frame["total"], frame["where"]))
        assert frame["kind"] == "zip" and frame["name"] == "backup.zip"
    assert seen == [(1, 3, "q3/note0.txt"), (2, 3, "q3/note1.txt"),
                    (3, 3, "q3/note2.txt")]
    assert progress.frames() == []


def test_an_mbox_inside_a_zip_stacks_outside_in(tmp_path: Path) -> None:
    """`backup.zip › mail.mbox › message 2 of 3` - the work order's own example
    shape. The zip reads its member through the registry on the same thread,
    so the mbox's frame lands on top of the zip's."""
    inner = tmp_path / "inner.mbox"
    _write_generated_mbox(inner, 3)
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.write(inner, "mail.mbox")
    trails = []
    for _document in read_zip(archive):
        stack = progress.trail()
        assert [f["kind"] for f in stack] == ["zip", "mbox"]
        trails.append(inner_trail(stack))
    assert trails == [
        "backup.zip › mail.mbox › message 1 of 3",
        "backup.zip › mail.mbox › message 2 of 3",
        "backup.zip › mail.mbox › message 3 of 3",
    ]
    assert progress.frames() == []


def test_olm_reports_message_n_of_m(tmp_path: Path, monkeypatch) -> None:
    """Observed at the moment each message is turned into a document.

    Not at the moment it is *yielded*: `with_closing_warning` reads one
    document ahead (so it can put a closing warning on the last one), so by
    the time the pipeline sees message k the reader is already on k+1 - which
    is the honest answer to "what is it reading now".
    """
    from app.extract import email_olm

    export = _olm(tmp_path / "Outlook for Mac.olm", 4)
    seen = []
    real = email_olm._document

    def spy(path, member, index, message):
        frame = _innermost()
        seen.append((frame["kind"], frame["unit"], frame["n"], frame["total"]))
        return real(path, member, index, message)

    monkeypatch.setattr(email_olm, "_document", spy)
    assert len(list(OlmExtractor().extract(export))) == 4
    assert seen == [("olm", "message", n, 4) for n in (1, 2, 3, 4)]
    assert progress.frames() == []


def _pst_tree() -> FakeFolder:
    projects = FakeFolder("Projects", messages=[
        FakeMessage(10 + i, subject=f"Survey {i}", plain=f"Valve {i} report.",
                    headers=f"Message-ID: <p{i}@example.com>\nFrom: a@example.com\n")
        for i in range(3)
    ])
    inbox = FakeFolder("Inbox", messages=[
        FakeMessage(1, attachments=[FakeAttachment("notes.txt", b"pump station notes")]),
    ], children=[projects])
    top = FakeFolder("Top of Personal Folders", children=[inbox])
    return FakeFolder("", children=[top])


def test_pst_reports_the_folder_and_message_n_of_m_within_it(monkeypatch) -> None:
    """Observed while each message is converted and each attachment read -
    see the `.olm` test for why not at the yield."""
    from app.extract import base, pst_libpff

    install_fake(monkeypatch, _pst_tree())
    seen = []

    def look(what: str) -> None:
        frame = progress.trail()[0]
        seen.append((what, frame["where"], frame["n"], frame["total"],
                     frame["stage"], frame["detail"]))

    real_convert = pst_libpff._to_document
    real_extract = base.extract

    def convert(*args, **kwargs):
        look("message")
        return real_convert(*args, **kwargs)

    def extract(path, *args, **kwargs):
        look("attachment")
        return real_extract(path, *args, **kwargs)

    monkeypatch.setattr(pst_libpff, "_to_document", convert)
    monkeypatch.setattr(base, "extract", extract)
    documents = list(pst_libpff.read_archive(Path("Archive2019.pst")))
    assert len(documents) == 5
    assert seen == [
        ("message", "Inbox", 1, 1, progress.STAGE_MESSAGES, ""),
        # Read while the message is still "1 of 1", and named.
        ("attachment", "Inbox", 1, 1, progress.STAGE_ATTACHMENTS, "notes.txt"),
        ("message", "Inbox/Projects", 1, 3, progress.STAGE_MESSAGES, ""),
        ("message", "Inbox/Projects", 2, 3, progress.STAGE_MESSAGES, ""),
        ("message", "Inbox/Projects", 3, 3, progress.STAGE_MESSAGES, ""),
    ]
    assert progress.frames() == []


def test_the_pst_folder_is_shown_as_a_person_would_say_it() -> None:
    from app.extract.pst_libpff import display_folder

    assert display_folder("(unnamed)/Top of Personal Folders/Inbox/Projects") == "Inbox/Projects"
    assert display_folder("(unnamed)/Top of Outlook data file/Sent Items") == "Sent Items"
    assert display_folder("Top of Personal Folders/Inbox") == "Inbox"
    assert display_folder("(unnamed)") == ""


def test_a_stage_marks_the_innermost_frame_and_is_put_back() -> None:
    with progress.enter("zip", "a.zip", unit="member", stage=progress.STAGE_ZIP):
        with progress.stage(progress.STAGE_OCR):
            assert _innermost()["stage"] == progress.STAGE_OCR
        assert _innermost()["stage"] == progress.STAGE_ZIP
    with progress.stage(progress.STAGE_OCR):     # outside any frame: harmless
        assert progress.frames() == []


# ---------------------------------------------------------------------------
# 3c: several readers at once, through a real run
# ---------------------------------------------------------------------------

class _Vectors:
    """The smallest vector store `Pipeline.run` accepts."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        for cid, fid in list(self.rows.items()):
            if fid in {int(one) for one in file_ids}:
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


def test_two_busy_readers_each_get_their_own_line(tmp_path: Path, monkeypatch) -> None:
    """Both readers are held inside their file at the same moment (a barrier),
    so the snapshot must show two lines with two different files - where the
    single `current` field could only ever show one of them.

    `checkpoint_seconds` is set far above a second on purpose: the ticks that
    see the two readers come from the heartbeat, not from the checkpoint.
    """
    corpus = tmp_path / "docs"
    corpus.mkdir()
    for name in ("alpha.txt", "beta.txt"):
        (corpus / name).write_text(f"pump station {name}", encoding="utf-8")

    both_inside = threading.Barrier(2, timeout=20)
    release = threading.Event()
    watchdog = threading.Timer(30, release.set)      # never hang the suite
    watchdog.start()

    def slow_extract(path, *, resume_from=0, resume_extra=None):
        with progress.enter("mbox", path.name, unit="message", total=3,
                            stage=progress.STAGE_MESSAGES) as frame:
            frame.n = 2
            both_inside.wait()
            release.wait(30)
            yield Document(path=path, text=f"pump station report {path.stem}")

    monkeypatch.setattr(pipeline_module, "extract", slow_extract)
    captured: list[IndexStats] = []
    tick_times: list[float] = []

    def on_progress(stats) -> None:
        if stats.phase != pipeline_module.PHASE_READING or release.is_set():
            return
        snap = stats.snapshot()
        tick_times.append(time.monotonic())
        busy = [w for w in snap.workers.values() if w["file"]]
        if len(busy) == 2 and len(tick_times) >= 3:
            captured.append(snap)
            release.set()

    embedder = Embedder(dim=4, encoder=lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts])
    try:
        with SqliteStore(tmp_path / "index.db") as store:
            stats = Pipeline(store, _Vectors(), embedder, PipelineConfig(
                walk=WalkConfig(roots=[corpus]), workers=2, min_free_gb=0,
                required_free_gb=0, checkpoint_seconds=60.0,
                limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                                      min_free_gb=0, low_priority=False),
            )).run(on_progress=on_progress)
    finally:
        watchdog.cancel()

    assert stats.indexed == 2
    assert captured, "never saw both readers busy at once"
    snap = captured[0]
    assert set(snap.workers) == {"1", "2"}
    assert {w["file"] for w in snap.workers.values()} == {"alpha.txt", "beta.txt"}
    for worker in snap.workers.values():
        assert worker["started_at"] > 0
        assert worker["inner"] == [{
            "kind": "mbox", "name": worker["file"], "unit": "message", "n": 2,
            "total": 3, "where": "", "stage": progress.STAGE_MESSAGES, "detail": "",
        }]
    # Backward compatibility: the one shared name is still filled, as before.
    assert snap.current in {"alpha.txt", "beta.txt"}
    # The heartbeat: ticks while both readers sat still came about once a
    # second, not once a minute (`checkpoint_seconds`).
    gaps = [b - a for a, b in zip(tick_times, tick_times[1:])]
    assert gaps and max(gaps) < 3.0, gaps
    assert snap.last_activity > 0
    # Plain JSON, as the child-process lane will stream it.
    assert json.loads(json.dumps(snap.workers)) == snap.workers
    # A finished run shows no readers.
    assert stats.workers == {}

    lines = worker_lines(snap, now=max(w["started_at"] for w in snap.workers.values()) + 3)
    assert len(lines) == 2
    assert any(line.startswith("Reader 1: ") for line in lines)
    assert all("› message 2 of 3 · " in line for line in lines), lines


def test_a_reader_on_its_own_thread_never_writes_into_another_readers_line() -> None:
    board = live_progress.WorkerBoard()
    ready = threading.Barrier(3, timeout=10)
    done = threading.Event()

    def reader(name: str, n: int) -> None:
        slot = board.open_slot()
        progress.attach(slot.frames)
        slot.begin(Path(name))
        with progress.enter("zip", name, unit="member", total=10) as frame:
            frame.n = n
            ready.wait()
            done.wait(10)
        slot.end()
        board.close_slot(slot)
        progress.detach()

    threads = [threading.Thread(target=reader, args=("one.zip", 3)),
               threading.Thread(target=reader, args=("two.zip", 7))]
    for thread in threads:
        thread.start()
    ready.wait()
    workers = board.workers()
    done.set()
    for thread in threads:
        thread.join(5)
    by_file = {w["file"]: w["inner"][0]["n"] for w in workers.values()}
    assert by_file == {"one.zip": 3, "two.zip": 7}
    assert board.workers() == {}, "closed slots must leave the board"
    assert progress.frames() == [], "the test thread's own stack was never touched"


def test_slot_numbers_are_reused_so_lines_stay_one_to_n() -> None:
    board = live_progress.WorkerBoard()
    first, second = board.open_slot(), board.open_slot()
    assert (first.id, second.id) == (1, 2)
    board.close_slot(first)
    assert board.open_slot().id == 1


# ---------------------------------------------------------------------------
# 3d: the heartbeat
# ---------------------------------------------------------------------------

def test_last_activity_moves_only_when_something_moved() -> None:
    stats = IndexStats()
    slot = stats.board.open_slot()
    slot.begin(Path("Archive2019.pst"))
    stats.refresh_live(now=100.0)
    assert stats.last_activity == 100.0
    stats.refresh_live(now=105.0)
    assert stats.last_activity == 100.0, "nothing moved, so the time must not"
    slot.frames.append(progress.Frame("pst", "Archive2019.pst", unit="message"))
    slot.frames[-1].n = 1
    stats.refresh_live(now=106.0)
    assert stats.last_activity == 106.0
    slot.frames[-1].n = 2
    stats.refresh_live(now=107.0)
    assert stats.last_activity == 107.0
    stats.indexed += 1
    stats.refresh_live(now=108.0)
    assert stats.last_activity == 108.0


def test_a_snapshot_is_plain_and_does_not_share_the_live_lists() -> None:
    stats = IndexStats()
    slot = stats.board.open_slot()
    slot.begin(Path("backup.zip"))
    slot.frames.append(progress.Frame("zip", "backup.zip", unit="member", total=4))
    snap = stats.snapshot()
    assert snap.board is None
    snap.workers["1"]["inner"][0]["n"] = 99
    assert stats.workers["1"]["inner"][0]["n"] == 0
    snap.refresh_live()                          # a snapshot never refreshes to empty
    assert set(snap.workers) == {"1"}
    record = json.loads(json.dumps(snap.as_dict()))
    assert record["workers"]["1"]["file"] == "backup.zip"
    assert record["last_activity"] == snap.last_activity


def test_every_new_field_is_plain_json() -> None:
    stats = IndexStats()
    stats.embed_batch, stats.embed_batches, stats.stage = 2, 5, "writing"
    slot = stats.board.open_slot()
    slot.begin(Path("mail.mbox"))
    progress.attach(slot.frames)                 # this thread writes the slot
    try:
        with progress.enter("mbox", "mail.mbox", unit="message", total=None):
            snap = stats.snapshot()
    finally:
        progress.detach()
    assert snap.workers["1"]["inner"][0]["total"] is None
    for name in ("workers", "last_activity", "stage", "embed_batch", "embed_batches",
                 "current", "current_item", "phase"):
        value = getattr(snap, name)
        assert json.loads(json.dumps(value)) == value, name


def test_the_stage_list_is_fixed_and_every_stage_has_words() -> None:
    assert pipeline_module.STAGES == live_progress.STAGES
    assert len(set(live_progress.STAGES)) == len(live_progress.STAGES)
    for code in live_progress.STAGES:
        assert STAGE_WORDS.get(code), f"no words for the {code!r} stage"
    assert set(STAGE_WORDS) == set(live_progress.STAGES)
    for code in progress.READER_STAGES:
        assert code in live_progress.STAGES


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------

def _pst_worker(n: int = 4512, total=18300, stage="messages", detail="") -> dict:
    return {"file": "Archive2019.pst", "path": "D:/Mail/Archive2019.pst",
            "started_at": 1000.0, "stage": "reading", "item": n,
            "inner": [{"kind": "pst", "name": "Archive2019.pst", "unit": "message",
                       "n": n, "total": total, "where": "Inbox/Projects",
                       "stage": stage, "detail": detail}]}


def test_the_headline_names_the_archive_the_folder_and_the_message() -> None:
    stats = SimpleNamespace(workers={"1": _pst_worker()})
    assert live_headline(stats) == (
        "Reading Archive2019.pst › Inbox/Projects — message 4,512 of 18,300")


def test_the_headline_without_a_total_says_n_alone() -> None:
    stats = {"workers": {"1": _pst_worker(total=None)}}
    assert live_headline(stats) == "Reading Archive2019.pst › Inbox/Projects — message 4,512"


def test_the_headline_names_the_attachment_being_read() -> None:
    stats = {"workers": {"1": _pst_worker(stage="attachments", detail="plans.pdf")}}
    assert live_headline(stats) == (
        "Reading Archive2019.pst › Inbox/Projects — message 4,512 of 18,300, "
        "attachment plans.pdf")


def test_the_headline_for_an_archive_still_opening_and_a_plain_file() -> None:
    opening = {"file": "Archive2019.pst", "started_at": 5.0, "inner": [
        {"kind": "pst", "name": "Archive2019.pst", "unit": "message", "n": 0,
         "total": None, "where": "", "stage": "opening", "detail": ""}]}
    assert live_headline({"workers": {"1": opening}}) == "Opening Archive2019.pst"
    plain = {"file": "report.docx", "started_at": 5.0, "inner": []}
    assert live_headline({"workers": {"1": plain}}) == "Reading report.docx"


def test_the_headline_prefers_the_reader_inside_a_container() -> None:
    plain = {"file": "report.docx", "started_at": 1.0, "inner": []}
    stats = {"workers": {"1": plain, "2": _pst_worker()}}
    assert live_headline(stats).startswith("Reading Archive2019.pst")


def test_the_headline_while_only_the_walker_is_moving() -> None:
    stats = {"workers": {}, "phase": "reading", "walk_complete": False}
    assert live_headline(stats) == "Finding files…"
    assert live_headline({"workers": {}, "phase": "reading", "walk_complete": True}) == ""


def test_one_line_per_reader_including_the_waiting_one() -> None:
    stats = {"workers": {
        "2": {"file": "report.docx", "started_at": 1198.0, "inner": []},
        "1": _pst_worker(),
        "3": {"file": "", "started_at": 0.0, "inner": []},
    }}
    assert worker_lines(stats, now=1200.0) == [
        "Reader 1: Archive2019.pst › Inbox/Projects › message 4,512 of 18,300 · 3 min 20 s",
        "Reader 2: report.docx · 2 s",
        "Reader 3: waiting for the next file",
    ]


def test_the_heartbeat_says_working_then_warns_at_exactly_the_threshold() -> None:
    stats = {"last_activity": 1000.0, "phase": "reading"}
    assert heartbeat_line(stats, now=1000.4) == ("Working · last activity just now", False)
    assert heartbeat_line(stats, now=1002.0) == ("Working · last activity 2 s ago", False)
    text, quiet = heartbeat_line(stats, now=1000.0 + QUIET_AFTER_S - 0.5)
    assert not quiet and text.startswith("Working")
    text, quiet = heartbeat_line(stats, now=1000.0 + QUIET_AFTER_S)
    assert quiet
    assert text.startswith("No progress for 1 min.")
    assert "OCR" in text and "attachment" in text
    for claim in ("hung", "hang", "stuck", "frozen", "crash"):
        assert claim not in text.lower(), "the warning must never claim a hang"


def test_the_heartbeat_in_a_step_with_no_count_explains_that_instead() -> None:
    stats = {"last_activity": 1000.0, "phase": "vector_index"}
    text, quiet = heartbeat_line(stats, now=1000.0 + 5 * 60)
    assert quiet and text.startswith("No progress for 5 min.")
    assert "This step can take several minutes" in text


def test_the_heartbeat_is_silent_before_the_first_sign_and_while_paused() -> None:
    assert heartbeat_line({"last_activity": 0.0}, now=50.0) == ("", False)
    assert heartbeat_line({"last_activity": 10.0, "paused": True}, now=500.0) == ("", False)


def test_the_writer_line() -> None:
    assert writer_line({"embed_batch": 3, "embed_batches": 12}) == (
        "Making text searchable by meaning, batch 3 of 12")
    # The repair pass knows no total: "batch 3", never "batch 3 of 0".
    assert writer_line({"embed_batch": 3, "embed_batches": 0}) == (
        "Making text searchable by meaning, batch 3")
    assert writer_line({"stage": "saving_resume", "embed_batch": 1}) == "Saving where to resume from"
    assert writer_line({}) == ""


def test_small_formatters() -> None:
    assert since_text(0) == "0 s"
    assert since_text(59.9) == "59 s"
    assert since_text(60) == "1 min"
    assert since_text(200) == "3 min 20 s"
    assert since_text(3600) == "1 h"
    assert since_text(3900) == "1 h 5 min"
    assert position_text({"unit": "member", "n": 12, "total": 40}) == "member 12 of 40"
    assert position_text({"unit": "message", "n": 0, "total": 40}) == ""


def test_live_view_puts_it_all_together_against_one_clock() -> None:
    stats = {"workers": {"1": _pst_worker()}, "last_activity": 1199.0,
             "embed_batch": 1, "embed_batches": 2, "phase": "reading"}
    view = live_view(stats, now=1200.0)
    assert view.headline.startswith("Reading Archive2019.pst › Inbox/Projects")
    assert view.workers[0].endswith("· 3 min 20 s")
    assert view.heartbeat == "Working · last activity 1 s ago" and not view.quiet
    assert view.writer == "Making text searchable by meaning, batch 1 of 2"


def test_ocr_marks_the_reader_it_runs_inside_as_ocr_and_puts_it_back() -> None:
    """Order 0x §3: OCR is seconds a page, so while it runs the reader's line
    must say "OCR" rather than look stuck on an item. The fake engine records
    the stage it sees mid-call; afterwards the frame's own stage is back."""
    from app.extract import ocr, progress

    seen: list = []

    def engine(_image):
        seen.append(progress.frames()[-1].stage)
        return ([], 0.0)

    with progress.enter("zip", "scans.zip", unit="member", total=1) as frame:
        before = frame.stage
        ocr.ocr_image("fake-source", engine=engine)
        assert frame.stage == before

    assert seen == [progress.STAGE_OCR]
    # Outside any reader it is a plain call - no frame, nothing to mark.
    ocr.ocr_image("fake-source", engine=lambda _i: ([], 0.0))
