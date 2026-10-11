r"""Order 1j section 2: Digital Inheritance shows what is there, whatever slash a root was saved with.

Layer: L2

2026-10-11, the owner: "the digital inheritance does not contain anything".
Measured on the owner's index: `ui:roots` held `D:/OutlookArchive|D:/Data`
(Qt's folder picker writes forward slashes) and `files.path` holds
`D:\Data\...`, so the root queries matched 0 of 135,841 files.
"""

from __future__ import annotations

import pytest

from app.reports.inheritance import catalogue_sources, render_inheritance_document
from app.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        for path, size in ((r"D:\Data\School\homework.docx", 10),
                           (r"D:\Data\School\Maths\sums.pdf", 20),
                           (r"D:\Data\Photos\beach.jpg", 30),
                           (r"D:\Data\notes.txt", 5),
                           (r"D:\Database\export.csv", 99)):        # a different folder
            parent = path.rsplit("\\", 1)[0]
            store.upsert_file(path, parent_dir=parent, size_bytes=size, mtime_ns=1,
                              status="INDEXED", source_kind="file")
        store.upsert_file("pst://1/7", size_bytes=1, mtime_ns=1, status="INDEXED",
                          source_kind="pst_message")
        yield store


@pytest.mark.parametrize("root", ["D:/Data", "D:\\Data", "D:/Data/", "D:\\Data\\"])
def test_a_root_counts_its_files_whatever_slash_it_was_saved_with(store, root):
    summary = store.local_root_summary(root)
    assert summary["n"] == 4 and summary["total_bytes"] == 65
    assert dict(store.local_root_folder_counts(root)) == {
        "School": 2, "Photos": 1, "(top level)": 1}


def test_a_root_does_not_take_a_folder_that_only_starts_with_its_name(store):
    assert store.local_root_summary("D:/Database")["n"] == 1
    assert store.local_root_summary("D:/Data")["n"] == 4


def test_the_report_lists_the_folders_and_an_empty_root_says_so(store):
    sources = catalogue_sources(store, roots=["D:/Data", "D:/OutlookArchive"])
    by_root = {s.name: s for s in sources}
    data = next(s for s in sources if s.file_count)
    assert data.file_count == 4
    document = render_inheritance_document(sources)
    assert "Holds: School (2 files)" in document
    assert "Nothing indexed from this source yet." in document
    assert len(by_root) == 2
