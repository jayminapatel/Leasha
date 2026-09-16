"""What the preview reads, how much of it, and what it refuses to do.

Layer: L5

Every decision here runs on a worker and none of it needs a display, which is
why it is a module of its own - and why these tests run on any machine.

The properties that matter: **a preview never raises**, because it happens for
every row somebody arrows past and a traceback for a file they merely scrolled
by is absurd; **a preview is bounded**, because reading a 400MB log to fill one
screen is the most expensive thing the window could do; and **email never keeps
a remote reference**, which `test_sanitise.py` covers in depth and one test here
confirms end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ui.preview_loader import (
    CAPS,
    KIND_HTML,
    KIND_IMAGE,
    KIND_MARKDOWN,
    KIND_NONE,
    KIND_PDF,
    KIND_TEXT,
    kind_for,
    load_preview,
    load_preview_for,
    offline_volume_subtitle,
    volume_preview,
)
from app.storage.sqlite_store import SqliteStore, volume_synthetic_path


# --- choosing a renderer ----------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("notes.txt", KIND_TEXT),
    ("data.csv", KIND_TEXT),
    ("script.PY", KIND_TEXT),          # case is not a different file type
    # §4a: rendered through QTextDocument.setMarkdown, not shown as raw text.
    ("README.md", KIND_MARKDOWN),
    ("notes.markdown", KIND_MARKDOWN),
    ("mail.eml", KIND_HTML),
    ("page.html", KIND_HTML),
    ("report.pdf", KIND_PDF),
    ("scan.PNG", KIND_IMAGE),
    # §4a: SVG via QtSvg (Qt already decodes it through QImage in this build).
    ("drawing.svg", KIND_IMAGE),
    # §4a: the indexer's own scanner-output extensions - see the note on
    # `_IMAGE_SUFFIXES`.
    ("scan.tif", KIND_IMAGE),
    ("scan.TIFF", KIND_IMAGE),
    # §4d: HEIC/HEIF, extending the image pipeline rather than a new kind.
    ("photo.heic", KIND_IMAGE),
    ("photo.HEIF", KIND_IMAGE),
    ("archive.zip", KIND_NONE),
    ("no-extension", KIND_NONE),
])
def test_the_renderer_is_chosen_by_extension(name, expected):
    assert kind_for(Path(name)) == expected


def test_markdown_has_its_own_cap_equal_to_text():
    """Not a separate number to keep in step - prose is prose."""
    assert CAPS[KIND_MARKDOWN] == CAPS[KIND_TEXT]


def test_a_markdown_file_comes_back_as_markdown_not_text(tmp_path: Path):
    note = tmp_path / "notes.md"
    note.write_text("# Barnsley Dairy\n\nHACCP review notes.", encoding="utf-8")

    preview = load_preview(str(note))

    assert preview.kind == KIND_MARKDOWN
    assert "HACCP" in preview.body
    assert "# Barnsley Dairy" in preview.body, (
        "the raw markup is kept in `body` - rendering happens in the widget, "
        "via QTextDocument.setMarkdown, not here"
    )


def test_choosing_a_renderer_never_touches_the_file():
    """The decision is needed before anything is read - including for a file on
    a drive that is not plugged in."""
    assert kind_for(Path("Z:/not/mounted/report.pdf")) == KIND_PDF


# --- reading ----------------------------------------------------------------

def test_a_text_file_comes_back_whole_when_it_is_small(tmp_path: Path):
    note = tmp_path / "note.txt"
    note.write_text("Barnsley Dairy HACCP review", encoding="utf-8")

    preview = load_preview(str(note))

    assert preview.kind == KIND_TEXT
    assert "HACCP" in preview.body
    assert not preview.truncated
    assert preview.title == "note.txt"


def test_a_huge_file_is_cut_and_says_so(tmp_path: Path):
    """A preview of a 400MB log is the first page of it. Reading the rest to
    show a screenful would be the most expensive thing the window ever does."""
    big = tmp_path / "huge.log"
    big.write_text("x" * (CAPS[KIND_TEXT] + 50_000), encoding="utf-8")

    preview = load_preview(str(big))

    assert preview.truncated
    assert len(preview.body) <= CAPS[KIND_TEXT]


def test_a_file_exactly_at_the_cap_is_not_called_truncated(tmp_path: Path):
    exact = tmp_path / "exact.txt"
    exact.write_text("y" * CAPS[KIND_TEXT], encoding="utf-8")

    assert not load_preview(str(exact)).truncated


def test_a_legacy_encoding_is_shown_rather_than_refused(tmp_path: Path):
    """cp1252 curly quotes and a pound sign. A preview that will not show a
    legacy file is worse than one showing an occasional wrong dash."""
    legacy = tmp_path / "old.txt"
    legacy.write_bytes("Cost: £250 — agreed".encode("cp1252"))

    body = load_preview(str(legacy)).body

    assert "250" in body
    assert "Cost" in body


def test_undecodable_bytes_still_produce_something(tmp_path: Path):
    broken = tmp_path / "broken.txt"
    broken.write_bytes(b"\xff\xfe\x00\x01 readable tail")

    preview = load_preview(str(broken))

    assert preview.kind == KIND_TEXT
    assert "readable tail" in preview.body


# --- email ------------------------------------------------------------------

def test_email_html_arrives_sanitised(tmp_path: Path):
    """The end-to-end version of `test_sanitise.py`: whatever the file holds,
    what reaches the widget has no remote reference in it."""
    message = tmp_path / "message.eml"
    message.write_text(
        '<html><body><p>Hello</p>'
        '<img src="https://tracker.example/open.gif">'
        '<script>steal()</script></body></html>',
        encoding="utf-8",
    )

    preview = load_preview(str(message))

    assert preview.kind == KIND_HTML
    assert "https://" not in preview.body
    assert "steal" not in preview.body
    assert "Hello" in preview.body
    assert "blocked" in preview.notice


def test_a_message_with_no_file_uses_the_text_it_was_given(tmp_path: Path):
    """A message's path is synthetic - `pst://archive/E12` - so there is no
    file to open and the body comes from the store."""
    preview = load_preview("pst://2007/2097828", mail_body="Subject: Agenda\n\nHi")

    assert preview.kind == KIND_TEXT
    assert "Agenda" in preview.body
    assert preview.error is None


# --- things that are not read here ------------------------------------------

def test_a_pdf_is_not_read_into_memory(tmp_path: Path):
    """It is paged by the viewer. Reading it here would pull the whole document
    in to hand it straight back."""
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.4\n" + b"0" * 5000)

    preview = load_preview(str(document), page=12)

    assert preview.kind == KIND_PDF
    assert preview.body == ""
    assert preview.page == 12


def test_an_enormous_image_is_declined_with_a_reason(tmp_path: Path):
    """Decoding is by pixel count, and an image is decoded whole whatever the
    pane displays."""
    huge = tmp_path / "scan.png"
    huge.write_bytes(b"\x89PNG\r\n" + b"0" * (CAPS[KIND_IMAGE] + 1024))

    preview = load_preview(str(huge))

    assert preview.kind == KIND_NONE
    assert preview.error is not None
    assert "untouched" in (preview.error.details or ""), (
        "say the file itself is unharmed - a refused preview reads as damage"
    )


def test_an_unknown_type_offers_the_card_rather_than_an_error(tmp_path: Path):
    """A type nothing can read is a card, not a failure.

    This used a `bundle.zip` until reading inside archives was built. A `.zip`
    is no longer an unknown type - something now tries to open it - so the case
    needs a file that genuinely has no reader, which is what it was always
    about. The archive half is the test below.
    """
    clip = tmp_path / "holiday.mp4"
    clip.write_bytes(b"\x00" * 64)

    preview = load_preview(str(clip))

    assert preview.kind == KIND_NONE
    assert preview.error is None, "not being previewable is not a failure"
    assert preview.title == "holiday.mp4"
    assert preview.subtitle, "the card still says how big it is and when"


def test_an_archive_that_will_not_open_says_why(tmp_path: Path):
    """**And here an error is the right answer**, which is the distinction.

    "There is no reader for this" and "the reader could not open it" look the
    same on screen and are not the same fact. A truncated download deserves to
    be told about; a `.mp4` does not.
    """
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"PK\x03\x04")

    preview = load_preview(str(archive))

    assert preview.error is not None
    assert preview.error.code == "ERR_ARCHIVE_UNREADABLE"


# --- never raising ----------------------------------------------------------

def test_a_missing_file_is_reported_not_raised(tmp_path: Path):
    preview = load_preview(str(tmp_path / "gone.txt"))

    assert preview.error is not None
    assert preview.error.code == "ERR_FILE_MISSING"
    assert "re-index" in preview.error.render().lower()


def test_a_disconnected_drive_does_not_raise():
    preview = load_preview("Z:/not/mounted/report.txt")

    assert preview.error is not None


def test_a_directory_is_not_mistaken_for_a_file(tmp_path: Path):
    folder = tmp_path / "folder.txt"       # a directory that looks like a file
    folder.mkdir()

    preview = load_preview(str(folder))

    assert preview.error is not None


def test_nonsense_input_does_not_raise():
    for path in ("", "   ", "\x00", "con", "//?/bad"):
        assert load_preview(path) is not None


# ---------------------------------------------------------------------------
# Offline Media §3b: preview from the index works offline - the mail
# synthetic-path pattern, applied to a row on a catalogued volume.
# ---------------------------------------------------------------------------

class _VolumeRow:
    """Just enough of a `ResultRow`/`FileRow` for `volume_preview` - a plain
    stand-in rather than the real dataclass, the same style
    `test_offline_media.py` already uses for its own row-shaped fixtures."""

    def __init__(self, *, volume_id, relative_path, file_id, name):
        self.volume_id = volume_id
        self.relative_path = relative_path
        self.file_id = file_id
        self.name = name
        self.path = f"leasha-volume://{volume_id}/{relative_path}"
        self.page = 0


def _store_with_offline_volume(tmp_path: Path):
    r"""A catalogued volume that is never connected on this machine - the
    same `TEST-GUID-...` trick `test_offline_media.py` uses, so no real
    Windows volume check has to be mocked to prove the offline path."""
    store = SqliteStore(tmp_path / "index.db").connect()
    volume_id = store.upsert_volume(
        "TEST-GUID-PREVIEW-0001", kind="drive", name="Projects 2019",
        volume_guid=r"\\?\Volume{00000000-0000-0000-0000-0000000000aa}",
        seen_at=1_700_000_000,
    )
    file_id = store.upsert_file(
        volume_synthetic_path(volume_id, "reports/q3.txt"),
        size_bytes=100, mtime_ns=1_700_000_000_000_000_000,
        volume_id=volume_id, relative_path="reports/q3.txt",
    )
    store.replace_chunks(file_id, [{"text": "Northern pump station report."}])
    return store, volume_id, file_id


def test_an_offline_volume_row_previews_from_the_stored_text(tmp_path: Path):
    r"""§3b's own wording: "the mail synthetic-path pattern: stored text +
    segments" - a row whose drive is not plugged in shows what the index
    already holds, not a "file missing" error for a file sitting in a
    drawer."""
    store, volume_id, file_id = _store_with_offline_volume(tmp_path)
    try:
        row = _VolumeRow(volume_id=volume_id, relative_path="reports/q3.txt",
                         file_id=file_id, name="q3.txt")
        preview = volume_preview(row, store)
    finally:
        store.close()

    assert preview.kind == KIND_TEXT
    assert "Northern pump station" in preview.body
    assert preview.error is None
    assert "Projects 2019" in preview.subtitle
    assert "offline" in preview.subtitle.lower()


def test_an_offline_volume_row_with_no_stored_text_says_so_honestly(tmp_path: Path):
    r"""A photo with no OCR text, say. Never "missing" - that claim is false
    of a file sitting in a drawer - always "the drive is not connected"."""
    store = SqliteStore(tmp_path / "index.db").connect()
    try:
        volume_id = store.upsert_volume(
            "TEST-GUID-PREVIEW-0002", kind="drive", name="Old WD",
            volume_guid=r"\\?\Volume{00000000-0000-0000-0000-0000000000bb}",
        )
        file_id = store.upsert_file(
            volume_synthetic_path(volume_id, "photo.jpg"),
            size_bytes=100, mtime_ns=1_700_000_000_000_000_000,
            volume_id=volume_id, relative_path="photo.jpg",
        )
        row = _VolumeRow(volume_id=volume_id, relative_path="photo.jpg",
                         file_id=file_id, name="photo.jpg")
        preview = volume_preview(row, store)
    finally:
        store.close()

    assert preview.kind == KIND_NONE
    assert preview.error is not None
    assert "plug" in preview.error.suggestion.lower()
    assert "missing" not in (preview.error.message or "").lower()


def test_an_online_volume_row_previews_exactly_like_an_ordinary_file(
    tmp_path: Path, monkeypatch,
):
    r"""Online: resolved through the current mount point (1b) and previewed
    exactly like any other file - the pre-existing bug this closes tried
    the synthetic key directly, which reported every volume-backed row as
    missing regardless of whether the drive was actually plugged in."""
    mount = tmp_path / "mount_e"
    (mount / "reports").mkdir(parents=True)
    real_file = mount / "reports" / "q3.txt"
    real_file.write_text("Live text straight off the drive.", encoding="utf-8")

    store, volume_id, file_id = _store_with_offline_volume(tmp_path)
    try:
        monkeypatch.setattr(
            "app.index.offline_media.resolve_file_path",
            lambda store, row, connected=None: real_file,
        )
        row = _VolumeRow(volume_id=volume_id, relative_path="reports/q3.txt",
                         file_id=file_id, name="q3.txt")
        preview = volume_preview(row, store)
    finally:
        store.close()

    assert preview.kind == KIND_TEXT
    assert "Live text straight off the drive" in preview.body


def test_load_preview_for_routes_a_volume_row_through_volume_preview(tmp_path: Path):
    """The wiring `load_preview_for` needs: a `store` and a `volume_id` on
    the row together must reach `volume_preview`, not the ordinary path-read
    below it - proven end to end through the public entry point every pane
    actually calls."""
    store, volume_id, file_id = _store_with_offline_volume(tmp_path)
    try:
        row = _VolumeRow(volume_id=volume_id, relative_path="reports/q3.txt",
                         file_id=file_id, name="q3.txt")
        preview = load_preview_for(row, store=store)
    finally:
        store.close()

    assert "Northern pump station" in preview.body


def test_load_preview_for_without_a_store_leaves_a_volume_row_unresolved(tmp_path: Path):
    """Additive, never a new failure mode: a caller that predates §3b (no
    `store` argument) gets exactly the old behaviour back."""
    row = _VolumeRow(volume_id=1, relative_path="reports/q3.txt",
                     file_id=1, name="q3.txt")
    preview = load_preview_for(row)

    assert preview.kind == KIND_NONE
    assert preview.error is not None


def test_offline_volume_subtitle_reads_nothing_for_an_ordinary_row(tmp_path: Path):
    store = SqliteStore(tmp_path / "index.db").connect()
    try:
        row = _VolumeRow.__new__(_VolumeRow)
        row.volume_id = None
        assert offline_volume_subtitle(store, row) == ""
    finally:
        store.close()
