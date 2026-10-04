"""One home for each "what kind of row is this" rule - 2026-10-04, code review.

The owner: *"the same code should run for functions so they are all
consistent and standard"*. Each test here puts one rule's copies, or the places
that used to keep a copy, against `app.core.row_facts` (or the one other home
named) and asserts they are now the same answer.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest

from app.core import row_facts
from app.core.row_facts import (
    ATTACHMENT_MARKER, MAIL_ARCHIVE_EXTS, MESSAGE_FILE_EXTS, OUTLOOK_ARCHIVE_EXTS,
    ZIP_FAMILY_EXTS, attachment_of, attachment_sql, container_of, is_message_key,
    is_synthetic_path, suffixes,
)

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The mail key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path, key, box", [
    ("pst://2024/212470", True, ""),
    ("pst://2024/212470/attachments/costs.docx", True, ""),
    ("D:\\Mail\\old.mbox/123", True, "D:\\Mail\\old.mbox"),
    ("D:/Mail/old.mbox/123", True, "D:/Mail/old.mbox"),
    ("D:\\Mail\\takeout.mbox.bak/7", True, "D:\\Mail\\takeout.mbox.bak"),
    ("D:\\Mail\\mac.olm/Accounts/x/message_00001.xml", True, "D:\\Mail\\mac.olm"),
    ("D:\\Mail\\OLD.MBOX/9", True, "D:\\Mail\\OLD.MBOX"),
    ("leasha-volume://3/Photos/a.jpg", False, ""),
    ("D:\\Mail\\note.eml", False, ""),
    ("D:\\Mail\\archive.pst", False, ""),
    ("D:\\a\\backup.zip/q3/report.docx", False, ""),
    ("D:\\Mail\\old.mbox", False, ""),
    ("", False, ""),
])
def test_a_mail_key_and_its_container_are_one_rule(path, key, box) -> None:
    assert is_message_key(path) is key
    assert container_of(path) == box


def test_a_synthetic_path_is_any_key_the_disk_cannot_answer_for() -> None:
    assert is_synthetic_path("leasha-volume://3/a.jpg")
    assert is_synthetic_path("pst://A/E1")
    assert is_synthetic_path("D:\\Mail\\old.mbox/123")
    assert not is_synthetic_path("D:\\docs\\report.docx")


def test_show_in_folder_and_drag_out_refuse_an_mbox_message() -> None:
    """Both said `"://" in path`, so an mbox message (`D:\\x.mbox/123`) was
    offered to Explorer as a file."""
    from types import SimpleNamespace

    from app.ui.drag_out import draggable
    from app.ui.presenter.rows import file_of_row

    mbox = SimpleNamespace(path="D:\\Mail\\old.mbox/123")
    assert file_of_row(mbox) == ""
    assert draggable(mbox) == ""
    plain = SimpleNamespace(path="D:\\docs\\report.docx")
    assert file_of_row(plain) == "D:\\docs\\report.docx"


def test_the_window_no_longer_decides_mail_by_its_own_prefix() -> None:
    """The copies the review found, outside the window agent's two files."""
    for name in ("app/ui/attachment_open.py", "app/ui/tasks.py", "app/ui/drag_out.py",
                 "app/ui/presenter/rows.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert 'startswith("pst://")' not in text, name
        assert '"://" in path' not in text, name


# ---------------------------------------------------------------------------
# The attachment key: one marker, one parser, SQL built from them
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "pst://A/E1/attachments/form.pdf",
    "pst://A/E1",
    "/attachments/x",
    "D:\\a.zip/Attachments/x.docx",
    "D:\\a.zip/ATTACHMENTS/x.docx",
    "pst://A/E1/attachments/pack.zip/q3/r.pdf",
    "",
])
def test_python_and_sql_agree_on_what_an_attachment_is(path) -> None:
    conn = sqlite3.connect(":memory:")
    in_sql = bool(conn.execute(f"SELECT {attachment_sql('')} FROM (SELECT ? AS path)",
                               (path,)).fetchone()[0])
    assert in_sql == bool(attachment_of(path)[0]), path


def test_there_is_one_marker_and_one_parser() -> None:
    from app.search import marks
    from app.storage import sqlite_store
    from app.storage.sqlite_store import LISTED_FILES, is_mail_attachment
    from app.ui.presenter import mail

    assert marks.ATTACHMENT_MARKER is mail.ATTACHMENT_MARKER is ATTACHMENT_MARKER
    assert mail.attachment_of is attachment_of
    assert is_mail_attachment is row_facts.is_mail_attachment
    assert LISTED_FILES == row_facts.listed_files_sql("f")
    assert "LIKE '%/attachments/%'" not in LISTED_FILES
    assert not hasattr(sqlite_store, "_ATTACHMENT_MARKER")
    from app.ui import tasks

    assert not hasattr(tasks, "_ATTACHMENT_MARKER")
    assert not hasattr(tasks, "_attachment_parent_path")


def test_an_attachment_key_is_written_with_the_same_marker() -> None:
    from app.extract.archive import attachment_key

    key = attachment_key("pst://A/E1", "form.pdf", None, None)
    assert attachment_of(key) == ("pst://A/E1", "form.pdf")


def test_a_zip_member_in_an_attachments_folder_is_not_listed_as_mail(tmp_path) -> None:
    """`LIKE` ignored case: `.../Attachments/...` counted for the store."""
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "x.db") as store:
        store.upsert_file("D:\\a.zip/Attachments/x.docx", parent_dir="D:\\a.zip/Attachments",
                          size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="eml")
        store.upsert_file("pst://A/E1/attachments/form.pdf", parent_dir="pst://A/E1",
                          size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="pst_message")
        assert store.count_listed_files() == 1


# ---------------------------------------------------------------------------
# The families
# ---------------------------------------------------------------------------

def test_mail_kinds_come_from_source_kind() -> None:
    from app.extract.base import SourceKind
    from app.storage.filters import MAIL_KINDS

    assert row_facts.MAIL_SOURCE_KINDS == (SourceKind.PST_MESSAGE, SourceKind.EML)
    assert MAIL_KINDS is row_facts.MAIL_SOURCE_KINDS


def test_message_files_are_the_message_readers_own_list() -> None:
    from app.extract.email_emlx import EmlxExtractor
    from app.extract.email_files import EmlExtractor, MsgExtractor

    claimed = EmlExtractor.extensions | MsgExtractor.extensions | EmlxExtractor.extensions
    assert suffixes(MESSAGE_FILE_EXTS) == claimed


@pytest.mark.parametrize("name", ["note.eml", "note.msg", "page.mht", "page.mhtml",
                                  "apple.emlx", "apple.partial.emlx"])
def test_every_message_file_keeps_its_size(name) -> None:
    """`.mht` and `.emlx` lost theirs: the list was ("eml", "msg")."""
    assert row_facts.own_size(4096, f"D:\\Mail\\{name}", "eml") == 4096


def test_mail_archives_are_the_archive_readers_own_list() -> None:
    from app.extract.email_mbox import MboxExtractor
    from app.extract.email_olm import OlmExtractor
    from app.extract.email_pst import OUTLOOK_EXTENSIONS
    from app.index import activity, walker
    from app.ui.presenter import mail

    assert OUTLOOK_EXTENSIONS == suffixes(OUTLOOK_ARCHIVE_EXTS)
    readers = OUTLOOK_EXTENSIONS | OlmExtractor.extensions | (MboxExtractor.extensions - {".mbox.bak"})
    assert suffixes(MAIL_ARCHIVE_EXTS) == readers
    assert activity.MAIL_ARCHIVE_EXTENSIONS == readers         # had lost `.olm`
    assert walker.STREAMED_MAILBOXES == readers
    assert mail.OUTLOOK_ARCHIVE_SUFFIXES == tuple(f".{e}" for e in OUTLOOK_ARCHIVE_EXTS)


def test_the_zip_family_is_one_list_and_both_patterns_are_built_from_it() -> None:
    from app.extract import archive, mail_attachments, pst_attachment
    from app.index import activity, scan
    from app.ui import attachment_open

    family = suffixes(ZIP_FAMILY_EXTS)
    assert frozenset(archive.ARCHIVE_EXTENSIONS) == family
    assert scan.ZIP_FAMILY == family
    assert activity.ARCHIVE_EXTENSIONS == family
    assert mail_attachments._ZIP_EXTENSIONS == family
    for ext in ZIP_FAMILY_EXTS:
        assert attachment_open.zip_member_of(f"D:\\a\\b.{ext}/x.txt")[0] == f"D:\\a\\b.{ext}"
        assert pst_attachment._NESTED.match(f"pack.{ext}/x.txt")
    assert attachment_open.zip_member_of("D:\\a\\b.docx/x.txt") == ("", "")


def test_attachment_open_reads_the_shared_mail_rule() -> None:
    from app.ui.attachment_open import is_archive_attachment

    assert is_archive_attachment("pst://A/E1/attachments/form.pdf")
    assert not is_archive_attachment("pst://A/E1")
    assert not is_archive_attachment("D:\\docs\\attachments\\form.pdf")


def test_code_is_the_code_tabs_source_list() -> None:
    from app.core.code_types import extensions_for
    from app.ui.presenter.results import CODE_EXTENSIONS, is_code_kind

    assert CODE_EXTENSIONS == extensions_for("source")
    assert is_code_kind("cbl") and is_code_kind("PY")   # COBOL was not code before
    for prose in ("txt", "md", "csv", "pdf", "json", ""):
        assert not is_code_kind(prose)


def test_the_drive_key_prefix_is_the_stores() -> None:
    from app.storage.sqlite_store import VOLUME_PATH_SCHEME
    from app.ui.presenter.opening import VOLUME_PREFIX

    assert VOLUME_PREFIX is VOLUME_PATH_SCHEME


# ---------------------------------------------------------------------------
# Words: sizes and dates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", [0, 812, 4_404_019, 25 * 1024 ** 2, 13 * 1024 ** 3,
                                  3 * 1024 ** 4])
def test_every_size_is_written_by_one_function(size) -> None:
    from app.chat.roles import _gb
    from app.cli.formats import _human_bytes
    from app.index.scan import human_bytes

    expected = row_facts.format_size(size)
    assert human_bytes(size) == _human_bytes(size) == _gb(size) == expected


def test_the_window_size_words_are_the_same_function(qapp) -> None:
    from app.ui.widgets.chat_roles import _size_words
    from app.ui.widgets.file_types import _human

    assert _human(25 * 1024 ** 2) == "25.0 MB"
    assert _size_words(512 * 1024 ** 2) == "512.0 MB" and _size_words(0) == ""


def test_a_download_reads_its_sizes_the_same_way() -> None:
    from app.core.model_fetch import _describe_bytes

    assert _describe_bytes(512 * 1024 ** 2, 0) == "512.0 MB so far"


def test_every_date_is_written_one_way() -> None:
    from app.ui.presenter.explain import _when
    from app.ui.presenter.formatting import _exact_date, _exact_date_from_epoch

    seconds = int(time.mktime((2023, 9, 17, 9, 30, 0, 0, 0, -1)))
    ns = seconds * 1_000_000_000
    assert _when(ns) == row_facts.day_words(ns) == "17 Sep 2023"
    assert _exact_date(ns) == _exact_date_from_epoch(seconds) == "17 Sep 2023, 09:30"
    assert _when(0) == "" and _exact_date(0) == ""
    chat = (ROOT / "app/chat/aggregate.py").read_text(encoding="utf-8")
    assert "%d %B %Y" not in chat


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------

def test_retry_is_offered_for_the_deferred_codes_a_run_reads_again() -> None:
    from app.core.file_state import DEFERRED_CODES, LATER_PASS_CODES, RETRY_CODES
    from app.ui.presenter import group_skips

    assert RETRY_CODES == DEFERRED_CODES - LATER_PASS_CODES
    assert LATER_PASS_CODES <= DEFERRED_CODES
    groups = {g.code: g for g in group_skips({code: 1 for code in DEFERRED_CODES})}
    assert {code for code, g in groups.items() if g.retryable} == RETRY_CODES


def test_every_error_code_raised_is_registered() -> None:
    """An unregistered code reaches the person as ERR_UNEXPECTED, "This is a
    bug". `ERR_FILE_NOT_FOUND` (mbox) and `ERR_PHASH` were."""
    from app.core.errors import ERROR_REGISTRY

    pattern = re.compile(r"(?:make_error|raise_error)\(\s*\"(ERR_[A-Z0-9_]+)\"")
    counted = re.compile(r"warned_by_code\[\"(ERR_[A-Z0-9_]+)\"\]")
    missing = set()
    for source in (ROOT / "app").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for match in [*pattern.finditer(text), *counted.finditer(text)]:
            if match.group(1) not in ERROR_REGISTRY:
                missing.add((match.group(1), source.relative_to(ROOT).as_posix()))
    assert not missing, sorted(missing)


def test_a_missing_file_is_reported_as_missing_not_corrupt(tmp_path, monkeypatch) -> None:
    from app.ui import workers

    error = workers.open_in_explorer(str(tmp_path / "gone.txt"))
    assert error is not None and error.code == "ERR_FILE_MISSING"
