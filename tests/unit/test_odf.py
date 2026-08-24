"""OpenDocument, read with nothing but the standard library.

Layer: L2

An ODF file is a ZIP holding `content.xml`, and both halves are in the standard
library - so this ships with no dependency and cannot break because a package
changed. `odfpy` was the obvious choice and publishes no wheel: pip builds it
from source, needing a compiler on every machine that installs this.

Two bugs are pinned below, and both were found by running the extractor against
documents rather than by reasoning about it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract.odf import CONTENT_MEMBER, OdfExtractor, text_from_content

NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"')


def content(body: str) -> bytes:
    return f'<?xml version="1.0"?><office:document-content {NS}>{body}</office:document-content>'.encode()


def odf_file(folder: Path, name: str, body: str) -> Path:
    path = folder / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(CONTENT_MEMBER, content(body).decode())
    return path


# ---------------------------------------------------------------------------
# The two bugs, pinned
# ---------------------------------------------------------------------------

def test_inline_formatting_does_not_scramble_the_sentence():
    """**The first bug.** ODF marks inline formatting with nested elements, so
    `<p>Findings from the <span>annual</span> inspection.</p>` holds three
    fragments: the paragraph's text, the span's text, and the span's *tail*.

    A first version ended the paragraph when it saw the first fragment and
    produced "Findings from the / inspection. annual" - not what the document
    says, and not searchable as a phrase.
    """
    text = text_from_content(content(
        "<office:text><text:p>Findings from the "
        "<text:span>annual</text:span> inspection.</text:p></office:text>"
    ))
    assert text == "Findings from the annual inspection."


def test_a_spreadsheet_keeps_its_rows_together():
    """**The second bug.** A `<text:p>` inside a cell closed before the cell
    did, flushing each cell onto its own line - so "Licence" and "12400" ended
    up two lines apart.

    That relationship is the entire reason a spreadsheet is worth indexing: a
    number next to a label is a fact, and a number on its own is noise.
    """
    text = text_from_content(content(
        "<office:spreadsheet><table:table>"
        "<table:table-row>"
        "<table:table-cell><text:p>Licence</text:p></table:table-cell>"
        "<table:table-cell><text:p>12400</text:p></table:table-cell>"
        "</table:table-row></table:table></office:spreadsheet>"
    ))
    assert text == "Licence\t12400"


def test_a_cell_containing_a_table_still_belongs_to_its_own_cell():
    """Depth, not a flag - a cell can hold a table."""
    text = text_from_content(content(
        "<office:spreadsheet><table:table><table:table-row>"
        "<table:table-cell><table:table><table:table-row>"
        "<table:table-cell><text:p>inner</text:p></table:table-cell>"
        "</table:table-row></table:table></table:table-cell>"
        "<table:table-cell><text:p>outer</text:p></table:table-cell>"
        "</table:table-row></table:table></office:spreadsheet>"
    ))
    assert "inner" in text and "outer" in text


# ---------------------------------------------------------------------------
# Reading documents
# ---------------------------------------------------------------------------

def test_headings_paragraphs_and_lists_each_become_a_line():
    text = text_from_content(content(
        "<office:text>"
        "<text:h>Leeds Site Safety Report</text:h>"
        "<text:p>Findings from the inspection.</text:p>"
        "<text:list><text:list-item><text:p>First point</text:p></text:list-item>"
        "<text:list-item><text:p>Second point</text:p></text:list-item></text:list>"
        "</office:text>"
    ))
    assert text.splitlines() == [
        "Leeds Site Safety Report",
        "Findings from the inspection.",
        "First point",
        "Second point",
    ]


def test_annotations_and_tracked_changes_are_left_out():
    """A comment is somebody's aside, not the document. Indexing it puts words
    in a document that its author never wrote there."""
    text = text_from_content(content(
        "<office:text><text:p>Real content.</text:p>"
        "<office:annotation><text:p>internal note</text:p></office:annotation>"
        "</office:text>"
    ))
    assert "Real content." in text
    assert "internal note" not in text


def test_a_foreign_namespace_is_still_read():
    """ODF's namespace URI has changed between versions, and other office
    suites write their own. Matching on the URI means a document from a
    different tool silently yields nothing at all."""
    text = text_from_content(
        b'<?xml version="1.0"?><document xmlns="http://some.other/ns">'
        b"<p>Still readable</p></document>"
    )
    assert "Still readable" in text


def test_broken_xml_is_empty_rather_than_an_exception():
    """It becomes ERR_NO_TEXT_LAYER upstream, which is the honest answer: the
    file is there and nothing could be read from it."""
    assert text_from_content(b"<<<not xml") == ""


# ---------------------------------------------------------------------------
# The file on disk, and every way it goes wrong
# ---------------------------------------------------------------------------

def test_a_real_odt_extracts(tmp_path):
    path = odf_file(tmp_path, "report.odt",
                    "<office:text><text:p>Leeds safety findings.</text:p></office:text>")
    documents = list(OdfExtractor().extract(path))

    assert len(documents) == 1
    assert "Leeds safety findings." in documents[0].text
    assert documents[0].meta["format"] == "opendocument"


def test_a_file_that_is_not_a_zip_is_a_precise_skip(tmp_path):
    path = tmp_path / "fake.odt"
    path.write_text("just text with the wrong extension", encoding="utf-8")

    with pytest.raises(AppErrorException) as caught:
        list(OdfExtractor().extract(path))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


def test_a_zip_without_content_xml_says_so(tmp_path):
    path = tmp_path / "empty.odt"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("styles.xml", "<x/>")

    with pytest.raises(AppErrorException) as caught:
        list(OdfExtractor().extract(path))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"
    assert "content.xml" in caught.value.error.details


def test_an_empty_document_yields_nothing_rather_than_an_empty_document(tmp_path):
    """Nothing to index is a skip reason, not a success. `base.extract` turns an
    empty yield into ERR_NO_TEXT_LAYER, which lands in the skip ledger."""
    path = odf_file(tmp_path, "blank.odp", "<office:presentation></office:presentation>")
    assert list(OdfExtractor().extract(path)) == []


def test_a_zip_bomb_is_refused_from_the_header(tmp_path):
    """Checked before reading, from the archive header - the whole point is not
    to allocate the gigabyte in order to discover it is a gigabyte."""
    from app.extract import odf

    path = odf_file(tmp_path, "big.odt", "<office:text><text:p>x</text:p></office:text>")
    original = odf.MAX_CONTENT_BYTES
    odf.MAX_CONTENT_BYTES = 1
    try:
        with pytest.raises(AppErrorException) as caught:
            list(OdfExtractor().extract(path))
        assert caught.value.error.code == "ERR_FILE_TOO_LARGE"
    finally:
        odf.MAX_CONTENT_BYTES = original


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_every_opendocument_extension_reaches_this_extractor():
    import app.extract  # noqa: F401 - importing populates the registry
    from app.extract.base import REGISTRY

    for extension in (".odt", ".ods", ".odp", ".ott", ".ots", ".otp"):
        assert REGISTRY.get(extension) is not None, f"{extension} is unregistered"
        assert REGISTRY[extension].name == "odf"


def test_the_shipped_config_now_has_these_routes_switched_on():
    """They shipped disabled while the extractor did not exist. It does now, and
    a route left off would mean the work landed and changed nothing."""
    from app.core.formats import load_rules

    rules = load_rules()
    for extension in (".odt", ".ods", ".odp"):
        rule = rules.rule_for(extension)
        assert rule is not None and rule.enabled, f"{extension} is still off"


def test_it_needs_no_third_party_package():
    """The reason this module exists rather than a dependency."""
    import ast

    source = Path(__file__).resolve().parents[2] / "app" / "extract" / "odf.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    allowed = {"zipfile", "pathlib", "typing", "xml", "app", "__future__"}
    assert imported <= allowed, f"unexpected imports: {imported - allowed}"
