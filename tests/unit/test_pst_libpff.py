"""Reading `.pst` without Outlook.

The point of this backend is that it can be tested. The Outlook path needs
Windows, a live Outlook and COM on the right thread; this one needs a file. So
these drive a fake `pypff` with the same shape as the real library, and cover
the parts that decide what ends up in the index: header parsing, threading,
body preference, folder skipping, and the export.

`test_the_real_library_has_the_api_we_use` checks the assumptions against the
actual library when it is installed, so a version bump that moves an accessor
fails here rather than on someone's archive.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from app.extract import pst_libpff
from app.extract.email_pst import PstBackend, choose_backend

HEADERS = (
    "Message-ID: <reply-2@example.com>\n"
    "In-Reply-To: <root-1@example.com>\n"
    "References: <root-1@example.com>\n"
    "From: Priya Anand <priya@example.com>\n"
    "To: Sam Okoro <sam@example.com>, Dev Team <dev@example.com>\n"
    "Cc: archive@example.com\n"
    "Subject: Re: Site survey findings\n"
)


class FakeMessage:
    def __init__(self, identifier=1, subject="Site survey findings",
                 plain="Two valves need replacing.", html="", rtf="",
                 headers=HEADERS, sender="Priya Anand", attachments=()):
        self._values = {
            "get_identifier": identifier, "get_subject": subject,
            "get_plain_text_body": plain, "get_html_body": html, "get_rtf_body": rtf,
            "get_transport_headers": headers, "get_sender_name": sender,
            "get_conversation_topic": "Site survey findings",
        }
        self._attachments = list(attachments)

    def __getattr__(self, name):
        if name in self._values:
            return lambda: self._values[name]
        raise AttributeError(name)

    def get_number_of_attachments(self):
        return len(self._attachments)

    def get_attachment(self, index):
        return self._attachments[index]


class FakeAttachment:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class FakeFolder:
    def __init__(self, name, messages=(), children=()):
        self._name = name
        self._messages = list(messages)
        self._children = list(children)

    def get_name(self):
        return self._name

    def get_number_of_sub_folders(self):
        return len(self._children)

    def get_sub_folder(self, index):
        return self._children[index]

    def get_number_of_sub_messages(self):
        return len(self._messages)

    def get_sub_message(self, index):
        return self._messages[index]


class FakeArchive:
    def __init__(self, root):
        self._root = root
        self.closed = False

    def open(self, _path):
        return None

    def get_root_folder(self):
        return self._root

    def close(self):
        self.closed = True


def install_fake(monkeypatch, root) -> FakeArchive:
    archive = FakeArchive(root)
    module = types.ModuleType("pypff")
    module.file = lambda: archive          # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypff", module)
    return archive


@pytest.fixture()
def tree() -> FakeFolder:
    inbox = FakeFolder("Inbox", messages=[
        FakeMessage(1),
        FakeMessage(2, subject="Budget forecast", plain="Capital spend is ahead of plan.",
                    headers="Message-ID: <b@example.com>\nFrom: sam@example.com\n"),
    ])
    deleted = FakeFolder("Deleted Items", messages=[FakeMessage(3, subject="Rubbish")])
    return FakeFolder("Top of Personal Folders", children=[inbox, deleted])


# --- the walk ---------------------------------------------------------------

def test_every_message_becomes_a_document(monkeypatch, tree) -> None:
    install_fake(monkeypatch, tree)
    documents = list(pst_libpff.read_archive(Path("2007.pst")))
    assert {d.meta["subject"] for d in documents} == {
        "Site survey findings", "Budget forecast",
    }


def test_deleted_items_is_skipped(monkeypatch, tree) -> None:
    install_fake(monkeypatch, tree)
    subjects = {d.meta["subject"] for d in pst_libpff.read_archive(Path("2007.pst"))}
    assert "Rubbish" not in subjects


def test_the_archive_is_always_closed(monkeypatch, tree) -> None:
    """A 4GB file left open is a lock the next run trips over."""
    archive = install_fake(monkeypatch, tree)
    list(pst_libpff.read_archive(Path("2007.pst")))
    assert archive.closed


def test_folder_path_is_recorded(monkeypatch, tree) -> None:
    install_fake(monkeypatch, tree)
    paths = {d.meta["folder_path"] for d in pst_libpff.read_archive(Path("2007.pst"))}
    assert paths == {"Top of Personal Folders/Inbox"}


def test_output_matches_the_outlook_backend(monkeypatch, tree) -> None:
    """Both backends must be interchangeable - Layer 3 cannot tell which ran."""
    install_fake(monkeypatch, tree)
    document = next(iter(pst_libpff.read_archive(Path("2007.pst"))))

    for key in ("subject", "sender", "recipients", "sent_at", "conversation",
                "entry_id", "store_path", "folder_path", "store_name"):
        assert key in document.meta, f"{key} missing - the two backends have diverged"
    assert document.source_kind == "pst_message"
    assert document.virtual_path.startswith("pst://")
    assert document.meta["backend"] == "libpff"


# --- headers ----------------------------------------------------------------

def test_recipients_come_from_the_real_headers(monkeypatch, tree) -> None:
    """MAPI record sets hold Exchange X.500 addresses nobody would ever type;
    the delivered headers hold real ones."""
    install_fake(monkeypatch, tree)
    document = next(iter(pst_libpff.read_archive(Path("2007.pst"))))
    import json

    assert set(json.loads(document.meta["recipients"])) == {
        "sam@example.com", "dev@example.com", "archive@example.com",
    }


def test_the_sender_is_an_address_not_a_display_name(monkeypatch, tree) -> None:
    install_fake(monkeypatch, tree)
    document = next(iter(pst_libpff.read_archive(Path("2007.pst"))))
    assert document.meta["sender"] == "priya@example.com"


def test_a_reply_threads_to_its_root(monkeypatch, tree) -> None:
    """Same rule as the .eml path: References[0] is the thread."""
    install_fake(monkeypatch, tree)
    document = next(iter(pst_libpff.read_archive(Path("2007.pst"))))
    assert document.meta["conversation"] == "<root-1@example.com>"


def test_without_headers_it_falls_back_to_the_conversation_topic(monkeypatch) -> None:
    """Messages this mailbox *sent* often have no transport headers at all."""
    message = FakeMessage(9, subject="Sent item", headers="")
    root = FakeFolder("Top", children=[FakeFolder("Sent Items", messages=[message])])
    install_fake(monkeypatch, root)

    document = next(iter(pst_libpff.read_archive(Path("a.pst"))))
    assert document.meta["conversation"] == "Site survey findings"


# --- bodies -----------------------------------------------------------------

def test_plain_text_is_preferred(monkeypatch) -> None:
    message = FakeMessage(1, plain="the plain body", html="<p>the html body</p>")
    root = FakeFolder("Top", children=[FakeFolder("Inbox", messages=[message])])
    install_fake(monkeypatch, root)
    assert "the plain body" in next(iter(pst_libpff.read_archive(Path("a.pst")))).text


def test_html_is_used_when_there_is_no_plain_text(monkeypatch) -> None:
    message = FakeMessage(1, plain="", html="<p>throughput rose 4%</p>")
    root = FakeFolder("Top", children=[FakeFolder("Inbox", messages=[message])])
    install_fake(monkeypatch, root)

    text = next(iter(pst_libpff.read_archive(Path("a.pst")))).text
    assert "throughput rose 4%" in text
    assert "<p>" not in text


def test_a_message_with_nothing_in_it_is_dropped(monkeypatch) -> None:
    message = FakeMessage(1, subject="", plain="", headers="", sender="")
    root = FakeFolder("Top", children=[FakeFolder("Inbox", messages=[message])])
    install_fake(monkeypatch, root)
    assert list(pst_libpff.read_archive(Path("a.pst"))) == []


def test_attachment_names_are_searchable(monkeypatch) -> None:
    message = FakeMessage(1, attachments=[FakeAttachment("costs.xlsx")])
    root = FakeFolder("Top", children=[FakeFolder("Inbox", messages=[message])])
    install_fake(monkeypatch, root)

    document = next(iter(pst_libpff.read_archive(Path("a.pst"))))
    assert "costs.xlsx" in document.text


# --- resilience -------------------------------------------------------------

def test_one_unreadable_message_does_not_end_the_archive(monkeypatch) -> None:
    class Exploding(FakeFolder):
        def get_sub_message(self, index):
            if index == 1:
                raise RuntimeError("corrupt item")
            return super().get_sub_message(index)

    inbox = Exploding("Inbox", messages=[
        FakeMessage(1, subject="first"), FakeMessage(2, subject="bad"),
        FakeMessage(3, subject="third"),
    ])
    install_fake(monkeypatch, FakeFolder("Top", children=[inbox]))

    subjects = {d.meta["subject"] for d in pst_libpff.read_archive(Path("a.pst"))}
    assert subjects == {"first", "third"}


def test_an_unopenable_archive_is_a_clean_apperror(monkeypatch, tree) -> None:
    from app.core.errors import AppErrorException

    archive = install_fake(monkeypatch, tree)

    def refuse(_path):
        raise OSError("not a PFF file")

    archive.open = refuse
    with pytest.raises(AppErrorException) as caught:
        list(pst_libpff.read_archive(Path("broken.pst")))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"
    assert "scanpst" in caught.value.error.suggestion


def test_missing_libpff_is_reported_not_crashed(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "pypff":
            raise ImportError("No module named 'pypff'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert not pst_libpff.available()
    with pytest.raises(pst_libpff.LibpffUnavailable):
        list(pst_libpff.read_archive(Path("a.pst")))


# --- export -----------------------------------------------------------------

def test_export_writes_one_eml_per_message(monkeypatch, tree, tmp_path: Path) -> None:
    """The permanent escape hatch: afterwards the mail needs neither Outlook nor
    libpff, and any mail client can open it."""
    install_fake(monkeypatch, tree)
    written = pst_libpff.export_to_eml(Path("2007.pst"), tmp_path)

    assert written == 2
    files = sorted(tmp_path.rglob("*.eml"))
    assert len(files) == 2
    assert "Two valves need replacing." in files[0].read_text(encoding="utf-8") or \
           "Capital spend" in files[0].read_text(encoding="utf-8")


def test_export_preserves_the_folder_structure(monkeypatch, tree, tmp_path: Path) -> None:
    install_fake(monkeypatch, tree)
    pst_libpff.export_to_eml(Path("2007.pst"), tmp_path)
    assert (tmp_path / "Top of Personal Folders" / "Inbox").is_dir()


def test_exported_messages_are_re_indexable(monkeypatch, tree, tmp_path: Path) -> None:
    """The whole point - they must go straight back through the .eml extractor."""
    from app.extract import extract

    install_fake(monkeypatch, tree)
    pst_libpff.export_to_eml(Path("2007.pst"), tmp_path)

    for path in tmp_path.rglob("*.eml"):
        documents = list(extract(path))
        assert documents and documents[0].text.strip()


def test_two_messages_with_one_subject_do_not_collide(monkeypatch, tmp_path: Path) -> None:
    same = [FakeMessage(1, subject="Weekly update"), FakeMessage(2, subject="Weekly update")]
    install_fake(monkeypatch, FakeFolder("Top", children=[FakeFolder("Inbox", messages=same)]))

    assert pst_libpff.export_to_eml(Path("a.pst"), tmp_path) == 2
    assert len(list(tmp_path.rglob("*.eml"))) == 2


def test_a_hostile_subject_cannot_escape_the_folder(monkeypatch, tmp_path: Path) -> None:
    """A subject is attacker-controlled text that becomes a filename."""
    nasty = FakeMessage(1, subject=r"../../etc/passwd<>:|?*")
    install_fake(monkeypatch, FakeFolder("Top", children=[FakeFolder("Inbox", messages=[nasty])]))

    pst_libpff.export_to_eml(Path("a.pst"), tmp_path)
    written = list(tmp_path.rglob("*.eml"))
    assert len(written) == 1
    assert tmp_path in written[0].parents


# --- backend selection ------------------------------------------------------

def test_ost_always_goes_to_outlook() -> None:
    """The Cached Exchange Mode file is Outlook's own, and libpff reads it poorly."""
    assert choose_backend(Path("cache.ost"), PstBackend.LIBPFF) == PstBackend.OUTLOOK
    assert choose_backend(Path("cache.ost"), PstBackend.AUTO) == PstBackend.OUTLOOK


def test_auto_prefers_libpff_when_it_is_there(monkeypatch) -> None:
    monkeypatch.setattr(pst_libpff, "available", lambda: True)
    assert choose_backend(Path("a.pst"), PstBackend.AUTO) == PstBackend.LIBPFF


def test_auto_falls_back_to_outlook_without_libpff(monkeypatch) -> None:
    monkeypatch.setattr(pst_libpff, "available", lambda: False)
    assert choose_backend(Path("a.pst"), PstBackend.AUTO) == PstBackend.OUTLOOK


def test_the_preference_is_honoured() -> None:
    assert choose_backend(Path("a.pst"), PstBackend.OUTLOOK) == PstBackend.OUTLOOK


# --- the real library -------------------------------------------------------

@pytest.mark.skipif(not pst_libpff.available(), reason="libpff is not installed here")
def test_the_real_library_has_the_api_we_use() -> None:
    """Pins the assumptions against the actual library.

    A version bump that renames an accessor should fail here, in a second, and
    not in the middle of someone's 30GB archive.
    """
    import pypff

    for name in ("get_subject", "get_plain_text_body", "get_html_body",
                 "get_transport_headers", "get_sender_name", "get_identifier",
                 "get_number_of_attachments", "get_attachment",
                 "get_client_submit_time", "get_conversation_topic"):
        assert hasattr(pypff.message, name), f"pypff.message lost {name}"

    for name in ("get_name", "get_number_of_sub_folders", "get_sub_folder",
                 "get_number_of_sub_messages", "get_sub_message"):
        assert hasattr(pypff.folder, name), f"pypff.folder lost {name}"

    for name in ("open", "close", "get_root_folder"):
        assert hasattr(pypff.file, name), f"pypff.file lost {name}"
