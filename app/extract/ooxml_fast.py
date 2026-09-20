r"""Word `.docx` text straight from the ZIP, without python-docx's object model.

Layer: L2

Measured 2026-09-20 (cProfile, `tools/extract_speed.py`): python-docx spent 2.0 s
reading a 6 MB report with 30 tables. **Almost none of that was parsing.** 0.6 s
went on reading all 160 parts of the package - every embedded picture - into
memory, and the rest on `paragraph.text` and `cell.text`, each of which runs a
fresh XPath over its element (6,493 of them, 1.7 s). The words live in exactly one
part, `word/document.xml`; this reads that one part with a single pass of
`ElementTree` and never opens the others.

**Same answer, with two differences that are improvements.** It is checked
against python-docx on every real `.docx` in the measured sample
(`tests/unit/test_docx_fast.py` holds the shape, the sample check is in the work
order note): identical word sets. The differences are deliberate - content
controls (`w:sdt`) and tracked insertions are read, where python-docx's
`body.iterchildren()` walk skipped a block-level control entirely and its
`paragraph.text` ignored a run inside `w:ins`; and a vertically merged cell is
empty in its continuation rows rather than repeating the text above.

Any file this cannot read cleanly raises, and the extractor then does exactly what
it did before - python-docx - so the fast path can only ever be a speed-up.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree

__all__ = ["docx_blocks", "DocxUnreadable"]

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_P, _TBL, _TR, _TC, _SDT, _SDT_CONTENT = (_W + n for n in ("p", "tbl", "tr", "tc", "sdt", "sdtContent"))
_T, _TAB, _BR, _CR, _NBH, _PTAB = (_W + n for n in ("t", "tab", "br", "cr", "noBreakHyphen", "ptab"))
_SKIP = frozenset({_W + "del", _W + "instrText", _W + "delText", _W + "pPr", _W + "rPr"})
_TXBX = _W + "txbxContent"

#: `word/document.xml` past this is not a document; python-docx would read the
#: same bytes into memory and be slower about it, so this is only a guard against
#: a zip bomb, not a limit on real files.
MAX_DOCUMENT_XML = 256 * 1024 * 1024


class DocxUnreadable(Exception):
    """Not a cleanly readable Word package. The caller falls back to python-docx."""


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    """One paragraph's text, in document order, as python-docx's `.text` would give it.

    Iterative so a pathological nesting depth cannot hit the recursion limit.
    """
    parts: list[str] = []
    stack = [iter(paragraph)]
    while stack:
        for child in stack[-1]:
            tag = child.tag
            if tag == _T:
                if child.text:
                    parts.append(child.text)
            elif tag == _TAB or tag == _PTAB:
                parts.append("\t")
            elif tag == _BR:
                # A page or column break is not a line break in the text.
                if child.get(_W + "type") in (None, "textWrapping"):
                    parts.append("\n")
            elif tag == _CR:
                parts.append("\n")
            elif tag == _NBH:
                parts.append("-")
            elif tag in _SKIP or tag == _TXBX:
                continue
            elif len(child):
                stack.append(iter(child))
                break
        else:
            stack.pop()
    return "".join(parts)


def _textboxes(paragraph: ElementTree.Element) -> Iterator[str]:
    """Text inside text boxes anchored in this paragraph: real content, often a
    heading or a callout, that python-docx never returned.

    A box is written twice - once for current Word and once as the fallback for
    old readers - so a repeat within one paragraph is dropped.
    """
    seen: set[str] = set()
    for box in paragraph.iter(_TXBX):
        for inner in box.iter(_P):
            text = _paragraph_text(inner).strip()
            if text and text not in seen:
                seen.add(text)
                yield text


def _cell_lines(container: ElementTree.Element) -> Iterator[str]:
    """Paragraphs of a cell in order, including any table nested in it."""
    for child in container:
        if child.tag == _P:
            yield _paragraph_text(child)
        elif child.tag == _TBL:
            yield _table_text(child)
        elif child.tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                yield from _cell_lines(content)


def _cell_text(cell: ElementTree.Element) -> str:
    return " ".join("\n".join(_cell_lines(cell)).split())


def _row_cells(row: ElementTree.Element) -> Iterator[ElementTree.Element]:
    for child in row:
        if child.tag == _TC:
            yield child
        elif child.tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                yield from _row_cells(content)


def _table_text(table: ElementTree.Element) -> str:
    lines: list[str] = []
    for row in table.findall(_TR):
        cells: list[str] = []
        for cell in _row_cells(row):
            text = _cell_text(cell)
            # A cell merged across columns repeats in python-docx and is one
            # cell here; the same adjacent-duplicate rule keeps both readers'
            # output the same shape.
            if not cells or cells[-1] != text:
                cells.append(text)
        line = "\t".join(cells).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _body_blocks(container: ElementTree.Element) -> Iterator[str]:
    for child in container:
        tag = child.tag
        if tag == _P:
            yield _paragraph_text(child)
            yield from _textboxes(child)
        elif tag == _TBL:
            yield _table_text(child)
        elif tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                yield from _body_blocks(content)


def docx_blocks(path: Path) -> list[str]:
    """Body blocks (paragraphs and tables, in order) of a `.docx` as text.

    Raises `DocxUnreadable` for anything that is not a plain Word package with a
    `word/document.xml` - including a content-type mismatch, which python-docx
    treats as an error and this does not: a template (`.dotx`) is read like a
    document, which is what somebody searching their own files wants.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise DocxUnreadable("no word/document.xml") from exc
            if info.file_size > MAX_DOCUMENT_XML:
                raise DocxUnreadable("word/document.xml is implausibly large")
            data = archive.read(info)
        root = ElementTree.fromstring(data)
    except (zipfile.BadZipFile, ElementTree.ParseError, RuntimeError, EOFError, ValueError) as exc:
        raise DocxUnreadable(str(exc)) from exc

    body = root.find(_W + "body")
    if body is None:
        raise DocxUnreadable("no w:body")
    return list(_body_blocks(body))
