"""Work order `pst-resilience`: an archive that is held open or slightly damaged.

Two questions, and the code used to answer both wrongly:

* *Held open by another program* - was reported as `ERR_FILE_CORRUPT`, which is
  settled and never retried, and never tried the one reader that can open it
  (Outlook, which is the program holding it).
* *A few bad messages* - one that broke conversion ended the archive; the ones
  that were skipped were only logged, so a 60%-indexed archive looked complete.

The fakes come from `test_pst_libpff`; the last two tests use the real `pypff`
and a real Windows file lock, because a fake cannot prove what the library says.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.errors import AppErrorException, make_error
from app.extract import email_pst, pst_libpff
from app.extract.base import Document, looks_locked, with_closing_warning
from tests.unit.test_pst_libpff import FakeFolder, FakeMessage, install_fake

_LOCKED_TEXT = (
    "pypff_file_open: unable to open file. libcfile_file_open_wide_with_error_code: "
    "unable to open file with error: The process cannot access the file because it "
    "is being used by another process."
)


# --- telling a lock from damage ---------------------------------------------

@pytest.mark.parametrize("exc", [
    PermissionError(13, "Permission denied"),
    OSError(_LOCKED_TEXT),
    OSError("sharing violation"),
])
def test_a_held_file_is_recognised_as_locked(exc) -> None:
    assert looks_locked(exc)


def test_the_windows_sharing_codes_count_as_locked() -> None:
    exc = OSError()
    exc.winerror = 32                        # type: ignore[attr-defined]
    assert looks_locked(exc)


@pytest.mark.parametrize("exc", [
    OSError("libpff_file_header_read_data: invalid file signature."),
    RuntimeError("not a PFF file"),
    ValueError("bad"),
])
def test_a_damaged_file_is_not_called_locked(exc) -> None:
    assert not looks_locked(exc)


def test_a_locked_open_is_a_lock_not_corruption(monkeypatch, tmp_path: Path) -> None:
    archive = install_fake(monkeypatch, FakeFolder("Top"))

    def refuse(_path):
        raise OSError(_LOCKED_TEXT)

    archive.open = refuse
    with pytest.raises(AppErrorException) as caught:
        list(pst_libpff.read_archive(tmp_path / "held.pst"))
    assert caught.value.error.code == "ERR_FILE_LOCKED"


def test_a_damaged_open_is_still_corruption(monkeypatch, tmp_path: Path) -> None:
    archive = install_fake(monkeypatch, FakeFolder("Top"))

    def refuse(_path):
        raise OSError("libpff_file_header_read_data: invalid file signature.")

    archive.open = refuse
    with pytest.raises(AppErrorException) as caught:
        list(pst_libpff.read_archive(tmp_path / "broken.pst"))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


# --- a few bad messages ------------------------------------------------------

def _bad(identifier: int) -> FakeMessage:
    """Fetches fine, then breaks the document builder - the one step in
    `_to_document` that no accessor guard covers."""
    return FakeMessage(identifier, subject="bad")


@pytest.fixture(autouse=True)
def _builder_breaks_on_bad_messages(monkeypatch):
    real = pst_libpff.build_email_document

    def build(*args, **kwargs):
        if kwargs.get("subject") == "bad":
            raise ValueError("corrupt property table")
        return real(*args, **kwargs)

    monkeypatch.setattr(pst_libpff, "build_email_document", build)


def _read(monkeypatch, *folders) -> list[Document]:
    install_fake(monkeypatch, FakeFolder("Top", children=list(folders)))
    return list(pst_libpff.read_archive(Path("a.pst")))


def _codes(document: Document) -> list[str]:
    return [w.code for w in document.warnings]


def test_a_message_that_breaks_conversion_does_not_end_the_archive(monkeypatch) -> None:
    documents = _read(monkeypatch, FakeFolder("Inbox", messages=[
        FakeMessage(1, subject="first"),
        _bad(2),
        FakeMessage(3, subject="third"),
        FakeMessage(4, subject="fourth"),
    ]))
    assert [d.meta["subject"] for d in documents] == ["first", "third", "fourth"]


def test_skipped_messages_are_reported_on_the_last_document(monkeypatch) -> None:
    documents = _read(monkeypatch, FakeFolder("Inbox", messages=[
        FakeMessage(1, subject="first"),
        _bad(2),
        FakeMessage(3, subject="third"),
    ]))
    assert [_codes(d) for d in documents] == [[], ["ERR_PST_PARTIAL"]]
    warning = documents[-1].warnings[0]
    assert "1 message could not be read" in warning.message
    assert "(2 were)" in warning.message


def test_a_clean_archive_carries_no_warning(monkeypatch) -> None:
    documents = _read(monkeypatch, FakeFolder("Inbox", messages=[
        FakeMessage(1, subject="a"), FakeMessage(2, subject="b"),
    ]))
    assert len(documents) == 2
    assert all(not d.warnings for d in documents)


def test_a_folder_whose_count_fails_is_reported_not_swallowed(monkeypatch) -> None:
    class NoCount(FakeFolder):
        def get_number_of_sub_messages(self):
            raise OSError("index corrupt")

    documents = _read(
        monkeypatch,
        NoCount("Broken", messages=[FakeMessage(9, subject="lost")]),
        FakeFolder("Inbox", messages=[FakeMessage(1, subject="kept")]),
    )
    assert [d.meta["subject"] for d in documents] == ["kept"]
    assert _codes(documents[-1]) == ["ERR_PST_PARTIAL"]
    assert "1 folder could not be read" in documents[-1].warnings[0].message


def test_a_subtree_that_cannot_be_listed_is_reported(monkeypatch) -> None:
    class NoChildren(FakeFolder):
        def get_number_of_sub_folders(self):
            raise OSError("folder table corrupt")

    documents = _read(
        monkeypatch,
        NoChildren("Archive", messages=[FakeMessage(1, subject="top of it")]),
    )
    assert [d.meta["subject"] for d in documents] == ["top of it"]
    assert _codes(documents[-1]) == ["ERR_PST_PARTIAL"]


def test_an_archive_where_nothing_reads_is_corrupt_not_empty(monkeypatch) -> None:
    """Otherwise the pipeline says "no text", which points nowhere useful."""
    install_fake(monkeypatch, FakeFolder("Top", children=[
        FakeFolder("Inbox", messages=[_bad(1), _bad(2)]),
    ]))
    with pytest.raises(AppErrorException) as caught:
        list(pst_libpff.read_archive(Path("a.pst")))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"
    assert "2 messages could not be read" in caught.value.error.details


def test_the_archive_is_still_closed_after_a_partial_read(monkeypatch) -> None:
    archive = install_fake(monkeypatch, FakeFolder("Top", children=[
        FakeFolder("Inbox", messages=[FakeMessage(1), _bad(2)]),
    ]))
    list(pst_libpff.read_archive(Path("a.pst")))
    assert archive.closed


def test_the_closing_warning_helper_tolerates_an_empty_source() -> None:
    assert list(with_closing_warning(iter(()), lambda: None)) == []


# --- an archive held open: fall back to Outlook -------------------------------

def _locked() -> AppErrorException:
    return AppErrorException(make_error("ERR_FILE_LOCKED", "extract.pst", path="held.pst"))


class _FakeSession:
    attached: list[Path] = []
    closed = False

    def attach(self, path: Path) -> None:
        self.attached.append(path)

    def close(self) -> None:
        self.closed = True


def _doc(name: str) -> Document:
    return Document(path=Path(name), text=name, meta={"subject": name})


@pytest.fixture()
def libpff_present(monkeypatch):
    monkeypatch.setattr(pst_libpff, "available", lambda: True)


def _extractor(backend: str) -> email_pst.PstExtractor:
    extractor = email_pst.PstExtractor()
    extractor.backend = backend
    return extractor


def test_a_locked_archive_is_read_through_outlook(monkeypatch, libpff_present) -> None:
    def refuse(_path, **_kw):
        raise _locked()
        yield                                    # pragma: no cover - makes it a generator

    session = _FakeSession()
    monkeypatch.setattr(pst_libpff, "read_archive", refuse)
    monkeypatch.setattr(email_pst, "Win32ComSession", lambda: session)
    monkeypatch.setattr(email_pst, "walk_session", lambda *_a, **_k: iter([_doc("via outlook")]))

    documents = list(_extractor(email_pst.PstBackend.AUTO).extract(Path("held.pst")))
    assert [d.meta["subject"] for d in documents] == ["via outlook"]
    assert session.attached == [Path("held.pst")]
    assert session.closed


def test_a_locked_archive_with_no_outlook_is_reported_as_locked(monkeypatch, libpff_present) -> None:
    def refuse(_path, **_kw):
        raise _locked()
        yield                                    # pragma: no cover

    def no_outlook():
        raise AppErrorException(make_error("ERR_OUTLOOK_MISSING", "extract.pst"))

    monkeypatch.setattr(pst_libpff, "read_archive", refuse)
    monkeypatch.setattr(email_pst, "Win32ComSession", no_outlook)

    with pytest.raises(AppErrorException) as caught:
        list(_extractor(email_pst.PstBackend.AUTO).extract(Path("held.pst")))
    assert caught.value.error.code == "ERR_FILE_LOCKED"       # retried next pass


def test_outlook_refusing_the_archive_still_reports_the_lock(monkeypatch, libpff_present) -> None:
    def refuse(_path, **_kw):
        raise _locked()
        yield                                    # pragma: no cover

    class Refusing(_FakeSession):
        def attach(self, path):
            raise AppErrorException(make_error("ERR_FILE_CORRUPT", "extract.pst"))

    monkeypatch.setattr(pst_libpff, "read_archive", refuse)
    monkeypatch.setattr(email_pst, "Win32ComSession", Refusing)

    with pytest.raises(AppErrorException) as caught:
        list(_extractor(email_pst.PstBackend.AUTO).extract(Path("held.pst")))
    assert caught.value.error.code == "ERR_FILE_LOCKED"


def test_a_forced_backend_is_not_quietly_overridden(monkeypatch, libpff_present) -> None:
    def refuse(_path, **_kw):
        raise _locked()
        yield                                    # pragma: no cover

    def unexpected():
        raise AssertionError("Outlook must not be tried when libpff was chosen")

    monkeypatch.setattr(pst_libpff, "read_archive", refuse)
    monkeypatch.setattr(email_pst, "Win32ComSession", unexpected)

    with pytest.raises(AppErrorException) as caught:
        list(_extractor(email_pst.PstBackend.LIBPFF).extract(Path("held.pst")))
    assert caught.value.error.code == "ERR_FILE_LOCKED"


def test_a_lock_after_messages_were_read_never_reads_the_archive_twice(
    monkeypatch, libpff_present,
) -> None:
    def partway(_path, **_kw):
        yield _doc("already indexed")
        raise _locked()

    def unexpected():
        raise AssertionError("falling back now would index the archive twice")

    monkeypatch.setattr(pst_libpff, "read_archive", partway)
    monkeypatch.setattr(email_pst, "Win32ComSession", unexpected)

    seen: list[str] = []
    with pytest.raises(AppErrorException):
        for document in _extractor(email_pst.PstBackend.AUTO).extract(Path("held.pst")):
            seen.append(document.meta["subject"])
    assert seen == ["already indexed"]


def test_an_outlook_attach_failure_from_a_lock_is_a_lock(monkeypatch) -> None:
    class Namespace:
        Stores: list = []

        def AddStore(self, _path):
            raise OSError("The process cannot access the file because it is being used by another process.")

    session = object.__new__(email_pst.Win32ComSession)
    session._namespace = Namespace()
    session._attached = []

    with pytest.raises(AppErrorException) as caught:
        session.attach(Path("held.pst"))
    assert caught.value.error.code == "ERR_FILE_LOCKED"


# --- Outlook skipped folders are no longer lost --------------------------------

def test_folders_outlook_could_not_read_reach_the_archive_warning(monkeypatch) -> None:
    email_pst.drain_busy_folders()

    class Store:
        display_name = "a"
        file_path = "a.pst"

    class Folder:
        path = "Inbox"

    def walk(*_a, **_k):
        yield _doc("one")
        email_pst._record_busy(Store(), Folder(), OSError("closed"))     # keyed by store
        yield _doc("two")

    session = _FakeSession()
    monkeypatch.setattr(email_pst, "Win32ComSession", lambda: session)
    monkeypatch.setattr(email_pst, "walk_session", walk)
    monkeypatch.setattr(pst_libpff, "available", lambda: False)

    documents = list(_extractor(email_pst.PstBackend.OUTLOOK).extract(Path("a.pst")))
    assert [_codes(d) for d in documents] == [[], ["ERR_PST_PARTIAL"]]
    assert "1 folder could not be read" in documents[-1].warnings[0].message
    assert email_pst.drain_busy_folders() == []          # consumed, not left to pile up


# --- the real library and a real Windows lock ---------------------------------

needs_real_lock = pytest.mark.skipif(
    sys.platform != "win32" or not pst_libpff.available(),
    reason="needs Windows and the real pypff",
)


def _hold_exclusively(path: Path):
    """Open `path` with no sharing at all, as a program that owns the file does."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    handle = kernel.CreateFileW(str(path), 0xC0000000, 0, None, 3, 0x80, None)
    assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    return lambda: kernel.CloseHandle(handle)


@needs_real_lock
def test_the_real_library_reports_an_exclusively_held_file_as_locked(tmp_path: Path) -> None:
    """Measured, not assumed: this is the message `looks_locked` was written from."""
    held = tmp_path / "held.pst"
    held.write_bytes(b"!BDN" + bytes(200))
    release = _hold_exclusively(held)
    try:
        with pytest.raises(AppErrorException) as caught:
            list(pst_libpff.read_archive(held))
    finally:
        release()
    assert caught.value.error.code == "ERR_FILE_LOCKED"


@needs_real_lock
def test_the_real_library_reports_garbage_as_corrupt(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pst"
    broken.write_bytes(b"this was never a pst" * 50)
    with pytest.raises(AppErrorException) as caught:
        list(pst_libpff.read_archive(broken))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


# --- a lock held by ANOTHER PROCESS, through the whole extractor ---------------

_HOLDER = r"""
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.restype = wintypes.HANDLE
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
h = k.CreateFileW(sys.argv[1], 0xC0000000, 0, None, 3, 0x80, None)   # share mode 0
if h in (None, wintypes.HANDLE(-1).value):
    print("FAILED", ctypes.get_last_error(), flush=True); sys.exit(1)
print("HELD", flush=True)
sys.stdin.read()                       # hold until the parent closes stdin
"""


@pytest.fixture()
def held_by_another_process(tmp_path: Path):
    """A file another *process* holds exclusively, as Outlook would hold a .pst."""
    import subprocess

    held = tmp_path / "held.pst"
    held.write_bytes(b"!BDN" + bytes(4096))
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(held)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "HELD"
        yield held
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


@needs_real_lock
def test_a_file_another_process_holds_falls_back_to_outlook(
    held_by_another_process, monkeypatch,
) -> None:
    """Order 1e's *logic*, proved without Outlook: real libpff, a real lock held by
    a separate process, the real `PstExtractor`. Only Outlook itself is faked - and
    must be, because building the real session **starts Outlook**, which attaches
    every archive it can see and moves their modified times."""
    session = _FakeSession()
    session.attached = []
    monkeypatch.setattr(email_pst, "Win32ComSession", lambda: session)
    monkeypatch.setattr(email_pst, "walk_session",
                        lambda *_a, **_k: iter([_doc("read through outlook")]))

    documents = list(_extractor(email_pst.PstBackend.AUTO).extract(held_by_another_process))

    assert [d.meta["subject"] for d in documents] == ["read through outlook"]
    assert session.attached == [held_by_another_process], "libpff was refused, Outlook was not asked"


@needs_real_lock
def test_a_file_another_process_holds_is_reported_locked_when_outlook_is_absent(
    held_by_another_process, monkeypatch,
) -> None:
    def no_outlook():
        raise AppErrorException(make_error("ERR_OUTLOOK_MISSING", "extract.pst"))

    monkeypatch.setattr(email_pst, "Win32ComSession", no_outlook)
    with pytest.raises(AppErrorException) as caught:
        list(_extractor(email_pst.PstBackend.AUTO).extract(held_by_another_process))
    assert caught.value.error.code == "ERR_FILE_LOCKED"        # retried on the next pass
