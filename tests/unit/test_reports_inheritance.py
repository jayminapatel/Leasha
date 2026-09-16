r"""Order 202626270602 (0n) section 2 - the Digital Inheritance report.

Layer: L4

Everything here runs off a real `SqliteStore` against a real temp database -
the report is a set of SQL queries plus formatting, and both halves are
worth proving against real rows rather than a hand-built fixture that
might not match what `upsert_file`/`upsert_volume` actually write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.reports.inheritance import (
    SourceSummary,
    TOP_FOLDERS_SHOWN,
    catalogue_sources,
    data_timestamp_sentence,
    render_inheritance_document,
    report_generated_at,
)
from app.storage.sqlite_store import SqliteStore


def _write_local(store, path, *, size_bytes=100, mtime_ns=1_700_000_000_000_000_000,
                 ext="txt"):
    parent = str(Path(path).parent)
    file_id = store.upsert_file(
        path, size_bytes=size_bytes, mtime_ns=mtime_ns, ext=ext,
        parent_dir=parent, source_kind="file", status="INDEXED",
    )
    store.mark_indexed(file_id)
    return file_id


# ---------------------------------------------------------------------------
# catalogue_sources - the query half
# ---------------------------------------------------------------------------

def test_a_local_root_is_summarised_by_file_count_and_size(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        _write_local(store, r"D:\Docs\Invoices\2019.pdf", size_bytes=1000)
        _write_local(store, r"D:\Docs\Invoices\2020.pdf", size_bytes=2000)
        _write_local(store, r"D:\Docs\Photos\a.jpg", size_bytes=500)

        sources = catalogue_sources(store, roots=[r"D:\Docs"])

    assert len(sources) == 1
    source = sources[0]
    assert source.kind == "local"
    assert source.name == "Docs"
    assert source.file_count == 3
    assert source.size_bytes == 3500
    assert dict(source.top_folders) == {"Invoices": 2, "Photos": 1}


def test_a_local_root_with_nothing_indexed_is_still_listed(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        sources = catalogue_sources(store, roots=[r"D:\Empty"])

    assert len(sources) == 1
    assert sources[0].file_count == 0
    assert sources[0].top_folders == ()


def test_a_catalogued_volume_is_summarised_from_relative_path(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        volume_id = store.upsert_volume(
            "TEST-GUID-INHERITANCE", kind="drive", name="Projects 2019",
            description="the old work drive", size_bytes=12_345,
        )
        file_id = store.upsert_file(
            f"leasha-volume://{volume_id}/Invoices/2019.pdf",
            size_bytes=10, mtime_ns=1, ext="pdf", parent_dir="p",
            source_kind="file", status="INDEXED",
            volume_id=volume_id, relative_path="Invoices/2019.pdf",
        )
        store.mark_indexed(file_id)

        sources = catalogue_sources(store)

    assert len(sources) == 1
    source = sources[0]
    assert source.kind == "drive"
    assert source.name == "Projects 2019"
    assert source.description == "the old work drive"
    assert source.size_bytes == 12_345
    assert dict(source.top_folders) == {"Invoices": 1}


def test_local_roots_come_before_catalogued_volumes(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        store.upsert_volume("TEST-GUID-ORDER", kind="drive", name="A Drive")
        sources = catalogue_sources(store, roots=[r"D:\Docs"])

    assert [s.kind for s in sources] == ["local", "drive"]


def test_many_top_level_folders_are_capped_and_counted(tmp_path):
    r"""A source with more folders than `TOP_FOLDERS_SHOWN` names the
    busiest ones and says how many more there are, rather than either
    listing every one of them or silently dropping the rest."""
    with SqliteStore(tmp_path / "i.db") as store:
        for i in range(TOP_FOLDERS_SHOWN + 3):
            _write_local(store, rf"D:\Docs\Folder{i:02d}\file.txt")

        sources = catalogue_sources(store, roots=[r"D:\Docs"])

    source = sources[0]
    assert len(source.top_folders) == TOP_FOLDERS_SHOWN
    assert source.more_folders == 3


def test_a_source_that_cannot_be_summarised_does_not_lose_the_rest(tmp_path, monkeypatch):
    r"""Non-negotiable #3's own rule, applied to a report: one bad source
    must not cost the whole document."""
    with SqliteStore(tmp_path / "i.db") as store:
        _write_local(store, r"D:\Docs\a.txt")
        store.upsert_volume("TEST-GUID-SURVIVES", kind="drive", name="Fine Drive")

        original = store.local_root_summary

        def _broken(root):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(store, "local_root_summary", _broken)
        sources = catalogue_sources(store, roots=[r"D:\Docs"])
        monkeypatch.setattr(store, "local_root_summary", original)

    names = {s.name for s in sources}
    assert "Docs" in names           # still listed, with empty numbers
    assert "Fine Drive" in names     # untouched by the local root's failure
    broken = next(s for s in sources if s.name == "Docs")
    assert broken.file_count == 0


def test_a_root_containing_percent_or_underscore_is_not_a_wildcard(tmp_path):
    r"""`local_root_summary`/`local_root_folder_counts` go through
    `like_escape` - a folder genuinely named with a literal `%` or `_`
    must not accidentally match everything, or everything else."""
    with SqliteStore(tmp_path / "i.db") as store:
        _write_local(store, r"D:\100%_Done\report.txt")
        _write_local(store, r"D:\other\report.txt")

        summary = store.local_root_summary(r"D:\100%_Done")

    assert summary["n"] == 1


# ---------------------------------------------------------------------------
# report_generated_at / data_timestamp_sentence - 1b
# ---------------------------------------------------------------------------

def test_report_generated_at_reads_the_newest_indexed_at(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        fid = _write_local(store, r"D:\Docs\a.txt")
        generated = report_generated_at(store)

    assert generated is not None
    assert generated > 0


def test_report_generated_at_is_none_for_an_empty_index(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        assert report_generated_at(store) is None


def test_data_timestamp_sentence_names_the_date():
    assert "16 September 2025" in data_timestamp_sentence(1_758_000_000)


def test_data_timestamp_sentence_is_honest_about_an_empty_index():
    sentence = data_timestamp_sentence(None)
    assert "nothing recorded" in sentence.lower()


# ---------------------------------------------------------------------------
# render_inheritance_document - the words
# ---------------------------------------------------------------------------

def test_the_document_names_every_included_source():
    sources = [
        SourceSummary(name="Docs", kind="local", file_count=3, size_bytes=3500,
                      top_folders=(("Invoices", 2), ("Photos", 1))),
        SourceSummary(name="Projects 2019", kind="drive", file_count=1,
                      size_bytes=10, status="offline", snapshot_date=1_690_000_000),
    ]
    doc = render_inheritance_document(sources, generated_at=1_758_000_000)

    assert "Docs" in doc
    assert "Projects 2019" in doc
    assert "Invoices" in doc and "Photos" in doc


def test_an_excluded_source_is_left_out_of_the_document():
    r"""2c: "optional per-source include/exclude checkboxes before
    export (a source can be private even from the map)"."""
    sources = [
        SourceSummary(name="Private Drive", kind="drive", file_count=5,
                      include=False),
        SourceSummary(name="Docs", kind="local", file_count=3),
    ]
    doc = render_inheritance_document(sources)

    assert "Private Drive" not in doc
    assert "Docs" in doc


def test_the_document_never_shows_file_contents():
    r"""2b's own words: "contents = names/locations/summaries ONLY -
    never file contents." Proven the honest way this codebase already
    proves an absence: put content-shaped text in the fixture and assert
    it never reaches the rendered document."""
    sources = [
        SourceSummary(name="Docs", kind="local", file_count=1,
                      top_folders=(("Invoices", 1),))
    ]
    doc = render_inheritance_document(sources)
    assert "SECRET-DOCUMENT-BODY-TEXT" not in doc


def test_the_document_states_its_data_timestamp():
    """1b, inside the actual rendered document, not only as a function
    somebody could call separately."""
    doc = render_inheritance_document([], generated_at=1_758_000_000)
    assert "16 September 2025" in doc


def test_an_empty_catalogue_says_so_rather_than_rendering_nothing():
    doc = render_inheritance_document([])
    assert "nothing" in doc.lower()


def test_sources_are_grouped_by_kind_with_plain_words_headings():
    sources = [
        SourceSummary(name="Docs", kind="local", file_count=1),
        SourceSummary(name="Old NAS", kind="network", file_count=1),
    ]
    doc = render_inheritance_document(sources)

    assert "Folders on this computer" in doc
    assert "Network shares" in doc
    # Never the raw column value on its own as a heading.
    assert "## local" not in doc
    assert "## network" not in doc


def test_an_archived_source_names_when_it_was_archived():
    sources = [SourceSummary(name="Tape B-0042", kind="archived",
                             file_count=200, snapshot_date=1_690_000_000)]
    doc = render_inheritance_document(sources)
    assert "archived" in doc.lower()


def test_a_sources_physical_location_text_appears_in_the_document():
    r"""2a's own words: "physical location text where given" - the field
    exists on `SourceSummary` (`location_note`, from `volumes.location_note`)
    but nothing previously proved it reached the rendered document rather
    than being read and silently dropped on the way to `_source_paragraph`."""
    sources = [SourceSummary(name="Old WD", kind="drive", file_count=40,
                             location_note="loft, blue crate")]
    doc = render_inheritance_document(sources)
    assert "loft, blue crate" in doc


def test_a_source_with_no_location_text_renders_without_a_stray_dash():
    r"""The paragraph builder only appends "- <location>" when a location
    is given; confirms the empty case doesn't leave a dangling separator
    a reader beside the will would have to puzzle over."""
    sources = [SourceSummary(name="Docs", kind="local", file_count=1)]
    doc = render_inheritance_document(sources)
    line = next(l for l in doc.splitlines() if l.startswith("'Docs'"))
    assert not line.rstrip().endswith("-")


# ---------------------------------------------------------------------------
# read-only guarantee - order 0n's own §5 first bullet, applied to the one
# module that talks to the store: no write syscalls against a user path.
# ---------------------------------------------------------------------------

def test_the_inheritance_module_never_writes_to_disk():
    r"""§5: "no write syscalls to user paths from any report path". This
    module reads `SqliteStore` and returns strings; nothing in it should
    ever open a path for writing, remove one, or shell out to move one -
    the same guard `test_preview_window.py::
    test_nothing_in_the_window_opens_a_file_for_writing` already applies
    to the preview pane, applied here to the module `ReportsView` calls
    off its worker thread."""
    source = (Path(__file__).resolve().parents[2] / "app" / "reports"
              / "inheritance.py").read_text(encoding="utf-8")
    for writing in ("write_text(", "write_bytes(", "shutil.", "os.remove",
                    "unlink(", '"w")', "'w')", "open("):
        assert writing not in source, f"inheritance.py has {writing}"
