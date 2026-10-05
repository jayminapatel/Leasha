r"""Order 202626270602 (0n) section 5 - the fixture tests.

Layer: L4 + L5

"inheritance PDF contains every fixture source's name and location text, and NO
file contents; space report finds the planted cross-source duplicates and the
planted unique-to-one-drive file; timeline June-2015 fixture shows the photo
(EXIF), the letter (mtime), and the offline drive's item with badge; thin-data
degradation states itself."

One shared fixture (`family_index`) is a small family's index: a computer, a
drive in a drawer, a tape in a safe, a share. The **PDF is really written by
`reports_view._write_pdf` and really read back** (PyMuPDF), because a document
that reads well as Markdown and loses a name in the print layout is the failure
that matters to the person holding the printout.

The timeline and thin-data halves live where their subject is
(`test_reports_timeline.py::test_june_2015_shows_the_photo_the_letter_the_drive_and_the_mail_oldest_first`,
`test_timeline_cli.py`, `test_timeline_view.py`, `test_reports_thin_data.py`);
this file adds the June 2015 fixture through the report entry point people
would actually use, so the four fixture sentences of the order are all asserted.
"""

from __future__ import annotations

import os

import pytest

from app.reports.inheritance import catalogue_sources, render_inheritance_document, report_generated_at
from app.reports.space import (
    document_for, find_duplicate_groups, find_source_uniqueness, hash_coverage,
    SpaceFindings, total_reclaimable_bytes,
)
from app.storage.sqlite_store import SqliteStore
from tests.unit.timeline_env import add_file, noon

#: Text that is inside the planted files. **It must never reach a printed page.**
SECRETS = ("ACCOUNT-PIN-4471-SECRET", "the will is in the blue folder BODYTEXT")


@pytest.fixture
def family_index(tmp_path):
    store = SqliteStore(tmp_path / "family.db").connect()
    drive = store.upsert_volume(
        "guid-projects", kind="drive", name="Projects 2019",
        description="the old work drive", status="OFFLINE", size_bytes=9_000_000)
    tape = store.upsert_volume(
        "tape-b-0042", kind="archived", name="LTO tape B-0042",
        description="everything before 2012", location_note="fire safe, hallway cupboard",
        status="ARCHIVED", size_bytes=50_000_000)
    share = store.upsert_volume("\\\\nas\\family", kind="network", name="Family NAS", status="ONLINE")

    def place(path, *, volume=None, rel=None, content_hash=None, size=1000, secret=None):
        file_id = add_file(
            store, path, mtime=noon(2015, 6, 1), content_hash=content_hash, size=size,
            volume_id=volume, relative_path=rel)
        if secret:
            store.replace_chunks(file_id, [{"ordinal": 0, "text": secret}])
        return file_id

    # A planted duplicate across three sources, a planted duplicate on one, and one
    # file that exists ONLY on the drive in the drawer.
    place(r"D:\Family\Photos\holiday.jpg", content_hash="dup-holiday", size=4_000_000, secret=SECRETS[0])
    place("leasha-volume://1/Photos/holiday.jpg", volume=drive, rel="Photos/holiday.jpg",
          content_hash="dup-holiday", size=4_000_000)
    place("leasha-volume://2/holiday-2011.jpg", volume=tape, rel="holiday-2011.jpg",
          content_hash="dup-holiday", size=4_000_000)
    place(r"D:\Family\Letters\a.txt", content_hash="dup-letters", size=2000, secret=SECRETS[1])
    place(r"D:\Family\Letters\a-copy.txt", content_hash="dup-letters", size=2000)
    place("leasha-volume://1/Accounts/2014.xlsx", volume=drive, rel="Accounts/2014.xlsx",
          content_hash="only-on-projects", size=700_000, secret=SECRETS[0])
    place("leasha-volume://3/Music/song.mp3", volume=share, rel="Music/song.mp3", content_hash="song")
    with store.write() as conn:
        conn.execute("UPDATE files SET indexed_at = 1700000000 + id")
    yield store, {"drive": drive, "tape": tape, "share": share}
    store.close()


# ---------------------------------------------------------------------------
# The inheritance PDF
# ---------------------------------------------------------------------------

def _write_the_pdf(document: str, target) -> None:
    r"""`reports_view._write_pdf`, in a fresh process - the way `test_grab_ui.py`
    makes its pictures. The offscreen platform reads its font directory once,
    when the first `QApplication` of a process is built, and a test run that
    has already built one without it would print a page with no letters on it
    (measured: an empty page, not an error)."""
    import subprocess
    import sys
    from pathlib import Path

    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    if fonts.is_dir():
        env["QT_QPA_FONTDIR"] = str(fonts)
    source = Path(target).with_suffix(".md")
    source.write_text(document, encoding="utf-8")
    script = ("import sys; from pathlib import Path; from PySide6.QtWidgets import QApplication;"
              "app = QApplication([]); from app.ui.reports_view import _write_pdf;"
              "_write_pdf(Path(sys.argv[1]).read_text(encoding='utf-8'), sys.argv[2])")
    done = subprocess.run([sys.executable, "-c", script, str(source), str(target)], env=env,
                          capture_output=True, text=True, timeout=120,
                          cwd=str(Path(__file__).resolve().parents[2]))
    assert done.returncode == 0, done.stderr[-800:]


def _pdf_text(path) -> str:
    import pymupdf

    with pymupdf.open(str(path)) as document:
        return "\n".join(page.get_text() for page in document)


def test_the_printed_map_names_every_source_and_where_it_is_kept_and_none_of_what_is_inside(
        tmp_path, family_index):
    store, _ids = family_index
    sources = catalogue_sources(store, roots=[r"D:\Family"])
    document = render_inheritance_document(
        sources, generated_at=report_generated_at(store), owner_name="Aunt Meera")
    target = tmp_path / "map.pdf"
    _write_the_pdf(document, target)
    text = _pdf_text(target)
    # NFKC (2026-10-05): Linux's fonts set "fi" as one ligature character, so
    # the extracted text read "ﬁre safe" and the words were not found. The
    # page was right; reading it back has to unfold ligatures first.
    import unicodedata

    flat = " ".join(unicodedata.normalize("NFKC", text).split())

    assert "A map of Aunt Meera" in flat
    # every source's name...
    for name in ("Family", "Projects 2019", "LTO tape B-0042", "Family NAS"):
        assert name in flat, f"{name!r} is missing from the printed map"
    # ...and where it is kept (the location text a family member needs to find the drawer):
    assert "fire safe, hallway cupboard" in flat
    assert "the old work drive" in flat
    # what it is, in words a reader who is not the owner can act on:
    assert "Drives" in flat and "Network shares" in flat
    # NEVER what is inside a file:
    for secret in SECRETS:
        assert secret not in flat and secret not in document
    assert "holiday.jpg" not in flat                      # not even a file name - folders only


def test_a_source_left_out_is_left_out_of_the_print_too(tmp_path, family_index):
    from dataclasses import replace

    store, _ids = family_index
    sources = [replace(s, include=s.name != "LTO tape B-0042")
               for s in catalogue_sources(store, roots=[r"D:\Family"])]
    target = tmp_path / "map.pdf"
    _write_the_pdf(render_inheritance_document(sources), target)
    flat = " ".join(_pdf_text(target).split())
    assert "Projects 2019" in flat and "LTO tape" not in flat and "fire safe" not in flat


# ---------------------------------------------------------------------------
# The Space Report
# ---------------------------------------------------------------------------

def test_the_space_report_finds_the_planted_duplicates_across_sources(family_index):
    store, _ids = family_index
    groups = {g.content_hash: g for g in find_duplicate_groups(store)}
    holiday = groups["dup-holiday"]
    assert {(c.source_name, c.source_kind) for c in holiday.copies} == {
        ("This computer", "local"), ("Projects 2019", "drive"), ("LTO tape B-0042", "archived")}
    assert holiday.reclaimable_bytes == 2 * 4_000_000
    assert groups["dup-letters"].reclaimable_bytes == 2000
    assert "only-on-projects" not in groups and "song" not in groups
    assert total_reclaimable_bytes(store) == 2 * 4_000_000 + 2000


def test_the_space_report_names_the_file_that_exists_only_on_one_drive(family_index):
    store, _ids = family_index
    unique = {u.name: u for u in find_source_uniqueness(store)}
    assert unique["Projects 2019"].file_count == 1          # the spreadsheet, and nothing else
    assert unique["Family NAS"].file_count == 1
    assert "LTO tape B-0042" not in unique                   # its only file is also elsewhere
    assert list(unique)[0] in ("Projects 2019", "Family NAS")           # volumes rank before this computer
    document = str(document_for(SpaceFindings(
        groups=tuple(find_duplicate_groups(store)), uniqueness=tuple(find_source_uniqueness(store)),
        total_reclaimable=total_reclaimable_bytes(store), **hash_coverage(store))))
    assert "1 file exist nowhere else but 'Projects 2019', currently offline" in document
    for secret in SECRETS:
        assert secret not in document


# ---------------------------------------------------------------------------
# The timeline, from the same family index
# ---------------------------------------------------------------------------

def test_june_2015_in_the_family_index_holds_what_the_order_says_it_should(family_index):
    from app.reports.timeline import Period, timeline_page

    store, ids = family_index
    photo = add_file(store, r"D:\Family\Photos\lake.jpg", mtime=noon(2019, 3, 1), taken=noon(2015, 6, 10))
    letter = add_file(store, r"D:\Family\Letters\to-the-bank.docx", mtime=noon(2015, 6, 20))
    beach = add_file(store, "leasha-volume://1/Holiday/beach.jpg", mtime=noon(2020, 1, 1),
                     taken=noon(2015, 6, 15), volume_id=ids["drive"], relative_path="Holiday/beach.jpg")
    page = timeline_page(store, Period.month(2015, 6), connected={})
    by_id = {e.file_id: e for e in page.entries}
    assert by_id[photo].basis == "taken" and by_id[letter].basis == "saved"
    assert by_id[beach].source_name == "Projects 2019" and not by_id[beach].reachable
    from app.reports.timeline_words import badge_words

    assert badge_words(by_id[beach]) == "Projects 2019 - not plugged in"
    assert [e.file_id for e in page.entries if e.file_id in (photo, letter, beach)] == [photo, beach, letter]
