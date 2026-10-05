"""Workspace §4c: EPUB previewed as chapters, not flattened text.

Layer: L5

**A zip of XHTML, and both halves already exist**: the spine-reading logic
mirrors `extract.ebook.EpubExtractor` (deliberately not shared - that one
flattens to plain text for the index, this one keeps the markup for the
pane), and the renderer is the same sanitised `QTextBrowser` `KIND_HTML`
already uses.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.ui.preview_loader import (
    KIND_EPUB,
    KIND_NONE,
    EpubChapter,
    kind_for,
    load_preview,
)

CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Barnsley Dairy: A History</dc:title>
    <dc:creator>A. Farmer</dc:creator>
  </metadata>
  <manifest>
    <item id="c1" href="chap1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="chap2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="c2"/>
    <itemref idref="c1"/>
  </spine>
</package>
"""

CHAP1 = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One: Beginnings</title></head>
<body><p>The dairy opened in 1974.</p>
<img src="https://tracker.example/x.gif"><script>steal()</script></body>
</html>
"""

CHAP2 = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter Two: Growth</title></head>
<body><p>By 1990 it supplied the whole county.</p></body>
</html>
"""


def _write_epub(path: Path, *, opf: str = OPF, container: str = CONTAINER_XML,
                chapters: dict | None = None) -> Path:
    chapters = chapters if chapters is not None else {
        "OEBPS/chap1.xhtml": CHAP1, "OEBPS/chap2.xhtml": CHAP2,
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/book.opf", opf)
        for name, text in chapters.items():
            archive.writestr(name, text)
    return path


@pytest.fixture()
def book(tmp_path: Path) -> Path:
    return _write_epub(tmp_path / "dairy.epub")


# --- choosing the kind -------------------------------------------------------

def test_epub_is_routed_to_its_own_kind():
    assert kind_for(Path("dairy.epub")) == KIND_EPUB


# --- reading ------------------------------------------------------------------

def test_chapters_come_back_in_spine_order_not_manifest_order(book: Path):
    """The manifest lists c1 then c2; the spine says c2 then c1 - and spine
    order is reading order, exactly as `extract.ebook`'s own docstring
    argues."""
    preview = load_preview(str(book))

    assert preview.kind == KIND_EPUB
    chapters = preview.meta["chapters"]
    assert [c.title for c in chapters] == ["Chapter Two: Growth",
                                           "Chapter One: Beginnings"]


def test_a_chapter_title_comes_from_its_own_title_tag(book: Path):
    preview = load_preview(str(book))
    first = preview.meta["chapters"][0]
    assert first.title == "Chapter Two: Growth"


def test_a_chapter_with_no_title_tag_falls_back_to_a_heading(tmp_path: Path):
    chapters = {"OEBPS/only.xhtml":
               "<html><body><h1>The Only Heading</h1><p>text</p></body></html>"}
    opf = OPF.replace(
        '<item id="c1" href="chap1.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="c2" href="chap2.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="c1" href="only.xhtml" media-type="application/xhtml+xml"/>',
    ).replace(
        '<itemref idref="c2"/>\n    <itemref idref="c1"/>',
        '<itemref idref="c1"/>',
    )
    path = _write_epub(tmp_path / "heading.epub", opf=opf, chapters=chapters)

    preview = load_preview(str(path))

    assert preview.meta["chapters"][0].title == "The Only Heading"


def test_chapter_html_is_sanitised_like_any_other_html(book: Path):
    """Remote images and script have no more business in a novel than in an
    email - the same cleaner, the same rule."""
    preview = load_preview(str(book))
    second = preview.meta["chapters"][1]        # spine order: chap1 is second

    assert "1974" in second.html
    assert "https://" not in second.html
    assert "steal" not in second.html


# --- failures -----------------------------------------------------------------

def test_not_a_zip_at_all_says_so(tmp_path: Path):
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"this is not a zip archive")

    preview = load_preview(str(broken))

    assert preview.kind == KIND_NONE
    assert preview.error is not None


def test_a_zip_with_no_container_xml_says_so(tmp_path: Path):
    path = tmp_path / "notabook.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "not a book")

    preview = load_preview(str(path))

    assert preview.kind == KIND_NONE
    assert preview.error is not None


def test_a_missing_manifest_member_is_skipped_not_fatal(tmp_path: Path):
    """A manifest entry pointing at a file that is not actually in the
    archive is common in hand-made books - skip the chapter, keep the book."""
    opf = OPF.replace(
        '<item id="c1" href="chap1.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="c1" href="missing.xhtml" media-type="application/xhtml+xml"/>',
    )
    path = _write_epub(tmp_path / "gap.epub", opf=opf,
                       chapters={"OEBPS/chap2.xhtml": CHAP2})

    preview = load_preview(str(path))

    assert preview.kind == KIND_EPUB
    assert len(preview.meta["chapters"]) == 1
    assert preview.meta["chapters"][0].title == "Chapter Two: Growth"


# --- widget --------------------------------------------------------------------

def _qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_widget_lists_every_chapter_and_shows_the_first():
    _qapp()
    from app.ui.widgets.epub_view import EpubView

    view = EpubView()
    view.show_chapters([
        EpubChapter(title="One", html="<p>first</p>"),
        EpubChapter(title="Two", html="<p>second</p>"),
    ])

    assert view.list.count() == 2
    assert view.list.item(0).text() == "One"
    assert "first" in view.browser.toPlainText()


def test_selecting_a_chapter_shows_its_text():
    _qapp()
    from app.ui.widgets.epub_view import EpubView

    view = EpubView()
    view.show_chapters([
        EpubChapter(title="One", html="<p>first</p>"),
        EpubChapter(title="Two", html="<p>second</p>"),
    ])
    view.list.setCurrentRow(1)

    assert "second" in view.browser.toPlainText()


def test_an_empty_book_clears_the_browser():
    _qapp()
    from app.ui.widgets.epub_view import EpubView

    view = EpubView()
    view.show_chapters([EpubChapter(title="One", html="<p>first</p>")])
    view.show_chapters([])

    assert view.list.count() == 0
    assert view.browser.toPlainText() == ""
