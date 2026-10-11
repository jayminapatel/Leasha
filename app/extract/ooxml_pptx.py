r"""PowerPoint `.pptx` text straight from the ZIP, without python-pptx's object model.

Layer: L2

Same finding as `ooxml_fast` for `.docx`, measured on the same day: python-pptx
builds an object for every part of the package (391 `parse_xml` calls and 0.3 s
for a 10 MB deck in which 25 parts hold any words) and then asks each shape
whether it has a text frame, a table or a chart, one XPath at a time - the
`getattr` chain in `office._shape_text` was a third of the read. The slide order is
in `presentation.xml`, each slide's notes and charts are one hop away in its
`.rels`, and the words are `a:t` elements.

Same contract as the docx fast path: anything it will not read cleanly raises
`PptxUnreadable` and the extractor does exactly what it did before, python-pptx,
so this can only ever be a speed-up. Checked word-for-word against python-pptx on
real decks by `tools/reader_recall.py`-style comparison (see the work-order note).
"""

from __future__ import annotations

import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

__all__ = ["pptx_slides", "PptxUnreadable"]

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
_MAX_PART = 128 * 1024 * 1024


class PptxUnreadable(Exception):
    """Not a cleanly readable PowerPoint package. The caller falls back to python-pptx."""


def _paragraph(node: ElementTree.Element) -> str:
    parts: list[str] = []
    for child in node.iter():
        if child.tag == _A + "t":
            if child.text:
                parts.append(child.text)
        elif child.tag == _A + "br":
            parts.append("\n")
    return "".join(parts)


def _frame_text(body: ElementTree.Element) -> str:
    return "\n".join(_paragraph(p) for p in body.findall(_A + "p"))


def _table_text(table: ElementTree.Element) -> str:
    """Rows as tab-separated lines - the shape python-pptx's path wrote."""
    lines = []
    for row in table.findall(_A + "tr"):
        cells = []
        for cell in row.findall(_A + "tc"):
            body = cell.find(_A + "txBody")
            cells.append(" ".join(_frame_text(body).split()) if body is not None else "")
        line = "\t".join(cells).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _drawing_text(root: ElementTree.Element) -> list[str]:
    """Every text frame and table on a slide, in shape order, groups included."""
    found: list[str] = []
    stack = [iter(root)]
    while stack:
        for node in stack[-1]:
            tag = node.tag
            if tag == _A + "tbl":
                text = _table_text(node)
                if text:
                    found.append(text)
            elif tag == _P + "txBody":
                text = _frame_text(node)
                if text.strip():
                    found.append(text)
            elif len(node):
                stack.append(iter(node))
                break
        else:
            stack.pop()
    return found


def _read_part(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    info = archive.getinfo(name)
    if info.file_size > _MAX_PART:
        raise PptxUnreadable(f"{name} is implausibly large")
    return ElementTree.fromstring(archive.read(info))


def _relationships(archive: zipfile.ZipFile, part: str) -> list[tuple[str, str, str]]:
    """`[(rId, type suffix, absolute target)]` for one part's internal relationships."""
    folder, _, leaf = part.rpartition("/")
    name = f"{folder}/_rels/{leaf}.rels" if folder else f"_rels/{leaf}.rels"
    try:
        root = _read_part(archive, name)
    except KeyError:
        return []
    found = []
    for rel in root.findall(_REL + "Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        if target.startswith("/"):
            absolute = target[1:]
        else:
            resolved: list[str] = []
            for piece in (folder.split("/") if folder else []) + target.split("/"):
                if piece == "..":
                    if resolved:
                        resolved.pop()
                elif piece and piece != ".":
                    resolved.append(piece)
            absolute = "/".join(resolved)
        found.append((rel.get("Id", ""), rel.get("Type", "").rsplit("/", 1)[-1], absolute))
    return found


def _notes_text(root: ElementTree.Element) -> str:
    """The speaker-notes placeholder only - not the slide image or the slide number."""
    for shape in root.iter(_P + "sp"):
        holder = shape.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
        if holder is not None and holder.get("type") == "body":
            body = shape.find(_P + "txBody")
            if body is not None:
                return _frame_text(body)
    return ""


def _chart_words(root: ElementTree.Element) -> list[str]:
    """Title, category labels and series names - the words, not the numbers."""
    found: list[str] = []
    title = root.find(f"{_C}chart/{_C}title")
    if title is not None:
        text = " ".join(t.text for t in title.iter(_A + "t") if t.text)
        if text.strip():
            found.append(text)
    for plot in root.iter():
        if plot.tag.startswith(_C) and plot.tag.endswith("Chart"):
            categories = [v.text for cat in plot.iter(_C + "cat") for v in cat.iter(_C + "v") if v.text]
            if categories:
                found.append(" ".join(categories))
            for series in plot.findall(_C + "ser"):
                names = [v.text for tx in series.findall(_C + "tx") for v in tx.iter(_C + "v") if v.text]
                if names:
                    found.append(names[0])
    return found


def pptx_slides(path: Path) -> list[tuple[str, str]]:
    """`[(slide text, speaker notes)]` in show order. Raises `PptxUnreadable`."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "ppt/presentation.xml" not in names:
                raise PptxUnreadable("no ppt/presentation.xml")
            presentation = _read_part(archive, "ppt/presentation.xml")
            targets = {rid: target for rid, _kind, target in _relationships(archive, "ppt/presentation.xml")}
            order = [targets[i.get(_R + "id", "")] for i in presentation.iter(_P + "sldId")
                     if targets.get(i.get(_R + "id", "")) in names]
            if not order:
                raise PptxUnreadable("no slides listed in presentation.xml")
            slides: list[tuple[str, str]] = []
            for part in order:
                parts = _drawing_text(_read_part(archive, part))
                notes = ""
                for _rid, kind, target in _relationships(archive, part):
                    if target not in names:
                        continue
                    if kind == "notesSlide":
                        notes = _notes_text(_read_part(archive, target))
                    elif kind == "chart":
                        try:
                            parts.extend(_chart_words(_read_part(archive, target)))
                        except ElementTree.ParseError:
                            pass
                slides.append(("\n".join(parts), notes))
            return slides
    except (zipfile.BadZipFile, zlib.error, ElementTree.ParseError, RuntimeError, EOFError, ValueError,
            KeyError) as exc:
        raise PptxUnreadable(str(exc)) from exc
