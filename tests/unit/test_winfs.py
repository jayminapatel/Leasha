"""Cloud placeholder detection.

The scenario that makes this matter: pointing the indexer at a OneDrive or
SharePoint folder with Files On-Demand enabled. Reading a placeholder downloads
the whole file, so a naive index run would hydrate an entire cloud library.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.winfs import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    describe_placeholder,
    file_attributes,
    is_cloud_placeholder,
)

FILE_ATTRIBUTE_NORMAL = 0x80
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_ARCHIVE = 0x20


@pytest.mark.parametrize(
    "attributes,expected",
    [
        (FILE_ATTRIBUTE_NORMAL, False),
        (FILE_ATTRIBUTE_ARCHIVE, False),
        (0, False),
        (FILE_ATTRIBUTE_OFFLINE, True),
        # 2026-09-30: was `(FILE_ATTRIBUTE_RECALL_ON_OPEN, True)`. The same bit is
        # FILE_ATTRIBUTE_EA, which Smart App Control sets on every executable it has
        # checked; alone it is not a placeholder, on a reparse point it is.
        (FILE_ATTRIBUTE_RECALL_ON_OPEN, False),
        (FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_REPARSE_POINT, True),
        (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, True),
        # The common real case: a dehydrated OneDrive file is also ARCHIVE.
        (FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, True),
    ],
)
def test_placeholder_detection(tmp_path: Path, attributes: int, expected: bool) -> None:
    assert is_cloud_placeholder(tmp_path / "any.docx", attributes) is expected


def test_a_checked_executable_is_not_a_cloud_file(tmp_path: Path) -> None:
    """Owner, 2026-09-30: *"still skipped file this is unacceptable"*. Two local
    `.exe` files in `D:\\OutlookArchive` read 0x40020 - ARCHIVE plus the
    extended-attribute bit Smart App Control's `$KERNEL.PURGE.ESBCACHE` sets -
    and were skipped as stored online only on every run. The walker's own
    check is held to the same answer."""
    from app.index.walker import Candidate

    bits = FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_RECALL_ON_OPEN
    assert is_cloud_placeholder(tmp_path / "pstfree.exe", bits) is False
    exe = Candidate(path=tmp_path / "pstfree.exe", size_bytes=1, mtime_ns=1, priority=0,
                    attributes=bits, flags=None, readable=False)
    assert exe.is_cloud_placeholder is False
    cloud = Candidate(path=tmp_path / "report.docx", size_bytes=1, mtime_ns=1, priority=0,
                      attributes=FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                      flags=None, readable=True)
    assert cloud.is_cloud_placeholder is True


def test_a_pinned_file_is_read_normally(tmp_path: Path) -> None:
    """'Always keep on this device' clears the recall bits: index it."""
    assert is_cloud_placeholder(tmp_path / "pinned.docx", FILE_ATTRIBUTE_ARCHIVE) is False


def test_unknown_attributes_default_to_indexing(tmp_path: Path) -> None:
    """Off Windows, or when stat fails, do not skip: a false skip loses data."""
    real = tmp_path / "real.txt"
    real.write_text("content", encoding="utf-8")
    assert is_cloud_placeholder(real, None) is False or file_attributes(real) is not None


def test_describe_names_the_attributes() -> None:
    described = describe_placeholder(
        FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    )
    assert "OFFLINE" in described
    assert "RECALL_ON_DATA_ACCESS" in described
    assert describe_placeholder(0) == ""
    assert describe_placeholder(None) == ""


def test_attributes_of_a_missing_file_are_none(tmp_path: Path) -> None:
    """Must not raise: the walker races with files being deleted."""
    assert file_attributes(tmp_path / "does_not_exist.txt") is None


def test_cloud_only_error_explains_the_fix() -> None:
    from app.core.errors import ActionType, make_error

    err = make_error("ERR_CLOUD_ONLY", "index.walker", path=r"D:\OneDrive\report.docx")
    assert "report.docx" in err.message
    assert "Always keep on this device" in err.suggestion
    # A skipped cloud file must never halt the run.
    assert err.action_type is ActionType.SKIP_CONTINUE
    assert not err.is_fatal
