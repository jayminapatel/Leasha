"""Layer 2: Outlook archives and the live mailbox.

These run anywhere. The COM calls live behind `Win32ComSession`; everything
tested here drives a `FakeSession` implementing the same duck types, because the
COM binding is a thin adapter while the walk, the conversation grouping, the
attachment dedup and the error handling are where the bugs actually are.

What is *not* covered: that `Win32ComSession` correctly drives real Outlook.
That needs Windows with Outlook running and is the residue of criteria 5 and 6.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract.email_pst import (
    DEFAULT_SKIP_FOLDERS,
    MailItem,
    PstExtractor,
    drain_busy_folders,
    walk_session,
)

# --- fakes ------------------------------------------------------------------

class FakeAttachment:
    def __init__(self, filename: str, payload: bytes) -> None:
        self.filename = filename
        self._payload = payload
        self.size_bytes = len(payload)
        self.saved = 0

    def save_to(self, path: Path) -> None:
        self.saved += 1
        path.write_bytes(self._payload)


class FakeFolder:
    def __init__(self, name: str, items=(), children=(), parent_path: str = "") -> None:
        self.name = name
        self._items = list(items)
        self._children = list(children)
        self.raises_on_items = False
        self.item_reads = 0
        self._reparent(parent_path)

    def _reparent(self, parent_path: str) -> None:
        """Re-path in place. Rebuilding children instead would drop any state a
        test set on them afterwards - which it did, and the resulting green test
        proved nothing."""
        self.path = f"{parent_path}/{self.name}".strip("/")
        for child in self._children:
            child._reparent(self.path)

    def folders(self):
        return list(self._children)

    def items(self):
        self.item_reads += 1
        if self.raises_on_items:
            raise OSError("Outlook was closed")
        return list(self._items)


class FakeStore:
    def __init__(self, name, root, file_path=None, is_live=False, cached_only=False) -> None:
        self.display_name = name
        self.file_path = file_path
        self.is_live = is_live
        self.cached_only = cached_only
        self._root = root

    def root(self):
        return self._root


class FakeSession:
    def __init__(self, *stores) -> None:
        self._stores = list(stores)

    def stores(self):
        return list(self._stores)


def message(entry_id: str, subject: str = "Subject", body: str = "Body text here", **kw):
    return MailItem(entry_id=entry_id, subject=subject, body=body, **kw)


@pytest.fixture(autouse=True)
def _readable_stand_ins(monkeypatch):
    """`.csv` and `.txt` stand in for a readable attachment here (1 October 2026)."""
    from tests.unit.test_pst_libpff import read_contents_of

    read_contents_of(monkeypatch, ".csv", ".txt")


@pytest.fixture(autouse=True)
def _clear_busy():
    drain_busy_folders()
    yield
    drain_busy_folders()


@pytest.fixture()
def archive() -> FakeSession:
    inbox = FakeFolder("Inbox", items=[
        message("id-1", "Site survey findings", "Two valves need replacing.",
                sender="priya@example.com", recipients=["sam@example.com"],
                conversation="thread-a", sent_at=1_772_000_000),
        message("id-2", "Re: Site survey findings", "Agreed, raising the PO.",
                sender="sam@example.com", conversation="thread-a"),
    ])
    projects = FakeFolder("Projects", items=[
        message("id-3", "Commissioning plan", "Handover in March."),
    ])
    deleted = FakeFolder("Deleted Items", items=[
        message("id-4", "Old rubbish", "Nobody wants this."),
    ])
    root = FakeFolder("Top of Outlook data file",
                      children=[inbox, projects, deleted])
    return FakeSession(FakeStore("2007 archive", root, file_path=r"D:\SearchData\2007.pst"))


# --- walking ----------------------------------------------------------------

def test_every_message_becomes_a_document(archive: FakeSession) -> None:
    documents = list(walk_session(archive))
    assert len(documents) == 3, "Deleted Items should not contribute"
    assert {d.meta["subject"] for d in documents} == {
        "Site survey findings", "Re: Site survey findings", "Commissioning plan",
    }


def test_deleted_items_is_skipped_by_default(archive: FakeSession) -> None:
    """On a fifteen-year archive Deleted Items is often a third of the messages,
    all of them things the owner decided they did not want."""
    assert "deleted items" in DEFAULT_SKIP_FOLDERS
    subjects = {d.meta["subject"] for d in walk_session(archive)}
    assert "Old rubbish" not in subjects


def test_skip_list_is_overridable(archive: FakeSession) -> None:
    subjects = {d.meta["subject"] for d in walk_session(archive, skip_folders=frozenset())}
    assert "Old rubbish" in subjects


def test_folder_path_is_recorded(archive: FakeSession) -> None:
    paths = {d.meta["folder_path"] for d in walk_session(archive)}
    assert paths == {"Top of Outlook data file/Inbox", "Top of Outlook data file/Projects"}


def test_empty_messages_are_dropped() -> None:
    root = FakeFolder("Root", items=[
        MailItem(entry_id="blank", subject="  ", body="", sender=""),
        message("real"),
    ])
    documents = list(walk_session(FakeSession(FakeStore("s", root))))
    assert len(documents) == 1


# --- identity and threading -------------------------------------------------

def test_the_key_is_the_entry_id_not_the_folder(archive: FakeSession) -> None:
    """Moving a message between folders must not make it look like a new one.

    On a mailbox people reorganise constantly, keying on a path is the
    difference between an index that settles and one that grows forever.
    """
    keys = {d.virtual_path for d in walk_session(archive)}
    assert "pst://2007 archive/id-1" in keys
    for key in keys:
        assert "Inbox" not in key


def test_a_reply_shares_its_conversation(archive: FakeSession) -> None:
    threads = {
        d.meta["subject"]: d.meta["conversation"]
        for d in walk_session(archive)
        if d.meta.get("conversation")
    }
    assert threads["Site survey findings"] == threads["Re: Site survey findings"] == "thread-a"


def test_store_metadata_reaches_the_document(archive: FakeSession) -> None:
    document = next(iter(walk_session(archive)))
    assert document.meta["store_path"] == r"D:\SearchData\2007.pst"
    assert document.meta["store_name"] == "2007 archive"
    assert document.source_kind == "pst_message"


def test_cached_only_is_recorded() -> None:
    """Cached Exchange Mode means coverage is whatever Outlook happened to hold.
    Recording it makes the gap visible rather than mysterious."""
    root = FakeFolder("Root", items=[message("m")])
    session = FakeSession(FakeStore("Mailbox", root, file_path=r"C:\x.ost",
                                    is_live=True, cached_only=True))
    document = next(iter(walk_session(session)))
    assert document.meta["store_cached_only"] is True


def test_headers_are_searchable_text(archive: FakeSession) -> None:
    document = next(d for d in walk_session(archive) if d.meta["entry_id"] == "id-1")
    assert "priya@example.com" in document.text
    assert "Site survey findings" in document.text
    assert "Two valves need replacing." in document.text


# --- store selection --------------------------------------------------------

def test_live_mailbox_can_be_excluded() -> None:
    live = FakeStore("Mailbox", FakeFolder("Root", items=[message("live")]),
                     file_path=r"C:\x.ost", is_live=True)
    pst = FakeStore("Archive", FakeFolder("Root", items=[message("archived")]),
                    file_path=r"D:\a.pst")
    session = FakeSession(live, pst)

    assert len(list(walk_session(session, include_live=True))) == 2
    assert len(list(walk_session(session, include_live=False))) == 1


def test_only_paths_selects_one_archive() -> None:
    first = FakeStore("A", FakeFolder("Root", items=[message("a")]), file_path=r"D:\a.pst")
    second = FakeStore("B", FakeFolder("Root", items=[message("b")]), file_path=r"D:\b.pst")
    session = FakeSession(first, second)

    documents = list(walk_session(session, only_paths=[r"d:\B.PST"]))
    assert len(documents) == 1
    assert documents[0].meta["store_name"] == "B"


# --- attachments ------------------------------------------------------------

DECK = b"item,quantity\npump,2\n"


def test_attachments_are_extracted_through_the_registry() -> None:
    item = message("with-attach")
    item.attachments = [FakeAttachment("costs.csv", DECK)]
    root = FakeFolder("Root", items=[item])

    documents = list(walk_session(FakeSession(FakeStore("s", root))))
    assert len(documents) == 2
    attached = documents[1]
    assert "pump" in attached.text
    assert attached.meta["attachment_name"] == "costs.csv"
    assert attached.virtual_path.endswith("/attachments/costs.csv")


def test_identical_attachments_are_indexed_once() -> None:
    """The same deck mailed round the team eight times is one document, not
    eight identical search results and eight times the embedding work."""
    first, second = message("m1"), message("m2")
    first.attachments = [FakeAttachment("proposal.csv", DECK)]
    second.attachments = [FakeAttachment("proposal_v2_FINAL.csv", DECK)]
    root = FakeFolder("Root", items=[first, second])

    documents = list(walk_session(FakeSession(FakeStore("s", root))))
    attachments = [d for d in documents if "attachment_name" in d.meta]
    assert len(attachments) == 1, "dedup is by content, not by filename"


def test_dedup_set_is_shared_with_the_caller() -> None:
    """Layer 3 persists this in files.content_hash, so dedup survives a restart
    instead of resetting on every run."""
    item = message("m")
    item.attachments = [FakeAttachment("costs.csv", DECK)]
    root = FakeFolder("Root", items=[item])

    seen: set[str] = set()
    list(walk_session(FakeSession(FakeStore("s", root)), seen_hashes=seen))
    assert len(seen) == 1

    again = list(walk_session(FakeSession(FakeStore("s", root)), seen_hashes=seen))
    assert not [d for d in again if "attachment_name" in d.meta]


def test_attachments_can_be_turned_off() -> None:
    item = message("m")
    item.attachments = [FakeAttachment("costs.csv", DECK)]
    root = FakeFolder("Root", items=[item])

    documents = list(walk_session(FakeSession(FakeStore("s", root)), with_attachments=False))
    assert len(documents) == 1
    assert "costs.csv" in documents[0].text, "the name is still searchable"


def test_oversized_attachment_is_warned_not_opened() -> None:
    # A deck, not a video, since 1 October 2026: a video attached to mail is now
    # recorded by name before its size is ever asked (`mail_attachments`), and
    # the ceiling still guards the types that are read.
    item = message("m")
    huge = FakeAttachment("enormous-deck.pptx", b"x")
    huge.size_bytes = 500 * 1_048_576
    item.attachments = [huge]
    root = FakeFolder("Root", items=[item])

    documents = list(walk_session(FakeSession(FakeStore("s", root))))
    assert len(documents) == 1
    assert huge.saved == 0, "never written to disk"
    assert any("larger than" in w.suggestion for w in documents[0].warnings)


def test_an_unreadable_attachment_does_not_lose_the_message() -> None:
    item = message("m", subject="Important decision")
    item.attachments = [FakeAttachment("broken.pdf", b"not a pdf at all")]
    root = FakeFolder("Root", items=[item])

    documents = list(walk_session(FakeSession(FakeStore("s", root))))
    assert len(documents) == 1
    assert "Important decision" in documents[0].text
    assert [w.code for w in documents[0].warnings] == ["ERR_FILE_CORRUPT"]


def test_unsupported_attachment_type_is_a_warning_not_a_failure() -> None:
    """1 October 2026: no longer a warning at all. A type that is not an Office
    document, a PDF or a zip is recorded by name and never opened."""
    item = message("m")
    exe = FakeAttachment("installer.exe", b"MZ binary")
    item.attachments = [exe]
    root = FakeFolder("Root", items=[item])

    documents = list(walk_session(FakeSession(FakeStore("s", root))))
    assert len(documents) == 2 and documents[0].warnings == ()
    assert exe.saved == 0, "never written to disk"
    assert documents[1].text == "installer.exe"
    assert documents[1].meta["contents_read"] is False


# --- Outlook closing mid-run ------------------------------------------------

def test_a_closed_folder_does_not_end_the_walk() -> None:
    """Acceptance criterion 6: closing Outlook mid-run costs the remaining
    folders, never the folders already done."""
    good = FakeFolder("Inbox", items=[message("a"), message("b")])
    bad = FakeFolder("Archive", items=[message("c")])
    bad.raises_on_items = True
    later = FakeFolder("Projects", items=[message("d")])
    root = FakeFolder("Root", children=[good, bad, later])

    documents = list(walk_session(FakeSession(FakeStore("s", root))))
    assert len(documents) == 3, "only the unreachable folder was lost"

    busy = drain_busy_folders()
    assert len(busy) == 1
    assert busy[0].code == "ERR_OUTLOOK_BUSY"
    assert "Archive" in busy[0].context["folder"]


def test_busy_errors_tell_the_user_to_leave_outlook_open() -> None:
    bad = FakeFolder("Inbox", items=[])
    bad.raises_on_items = True
    root = FakeFolder("Root", children=[bad])

    list(walk_session(FakeSession(FakeStore("s", root))))
    busy = drain_busy_folders()
    assert "Outlook open" in busy[0].suggestion


def test_an_unopenable_store_is_reported() -> None:
    class Refusing(FakeStore):
        def root(self):
            raise OSError("MAPI refused")

    session = FakeSession(Refusing("broken", None, file_path=r"D:\x.pst"))
    with pytest.raises(AppErrorException) as caught:
        list(walk_session(session))
    assert caught.value.error.code == "ERR_OUTLOOK_BUSY"


# --- checkpointing ----------------------------------------------------------

def test_folder_completion_is_reported_for_checkpointing(archive: FakeSession) -> None:
    """A 4GB archive must be resumable at folder granularity, so an interrupted
    run restarts at the folder it was in rather than at the beginning."""
    seen: list[tuple[str, int]] = []
    list(walk_session(archive, on_folder=lambda path, n: seen.append((path, n))))

    reported = dict(seen)
    assert reported["Top of Outlook data file/Inbox"] == 2
    assert reported["Top of Outlook data file/Projects"] == 1


def test_walk_is_lazy() -> None:
    """A 4GB archive must not be materialised to yield its first message."""
    first = FakeFolder("Inbox", items=[message("a")])
    second = FakeFolder("Later", items=[message("b")])
    root = FakeFolder("Root", children=[first, second])

    walker = walk_session(FakeSession(FakeStore("s", root)))
    next(walker)
    assert second.item_reads == 0, "the second folder was not touched yet"


# --- the registered extractor ----------------------------------------------

def test_pst_is_registered() -> None:
    from app.extract import extractor_for

    assert extractor_for(Path("mail.pst")) is not None
    assert extractor_for(Path("cache.ost")) is not None


def test_extractor_uses_an_injected_session(monkeypatch: pytest.MonkeyPatch) -> None:
    root = FakeFolder("Root", items=[message("m", subject="From the archive")])
    session = FakeSession(FakeStore("A", root, file_path=r"D:\a.pst"))

    extractor = PstExtractor()
    monkeypatch.setattr(extractor, "session_factory", lambda: session)

    documents = list(extractor.extract(Path(r"D:\a.pst")))
    assert len(documents) == 1
    assert "From the archive" in documents[0].text


def test_without_outlook_the_error_names_outlook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Windows, or with Outlook absent, this must be a clear AppError."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("win32com"):
            raise ImportError("No module named 'win32com'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    from app.extract.email_pst import Win32ComSession

    with pytest.raises(AppErrorException) as caught:
        Win32ComSession()
    assert caught.value.error.code == "ERR_OUTLOOK_MISSING"
    assert "Outlook" in caught.value.error.suggestion
