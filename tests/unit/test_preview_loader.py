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
    KIND_NONE,
    KIND_PDF,
    KIND_TEXT,
    kind_for,
    load_preview,
)


# --- choosing a renderer ----------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("notes.txt", KIND_TEXT),
    ("README.md", KIND_TEXT),
    ("data.csv", KIND_TEXT),
    ("script.PY", KIND_TEXT),          # case is not a different file type
    ("mail.eml", KIND_HTML),
    ("page.html", KIND_HTML),
    ("report.pdf", KIND_PDF),
    ("scan.PNG", KIND_IMAGE),
    ("archive.zip", KIND_NONE),
    ("no-extension", KIND_NONE),
])
def test_the_renderer_is_chosen_by_extension(name, expected):
    assert kind_for(Path(name)) == expected


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
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"PK\x03\x04")

    preview = load_preview(str(archive))

    assert preview.kind == KIND_NONE
    assert preview.error is None, "not being previewable is not a failure"
    assert preview.title == "bundle.zip"
    assert preview.subtitle, "the card still says how big it is and when"


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
