r"""OpenDocument: .odt, .ods, .odp and their templates. No dependency.

Layer: L2

An ODF file is a ZIP holding `content.xml`. That is the whole format as far as
search is concerned, and both halves are in the standard library - so this costs
nothing to ship and cannot break because a package changed.

**`odfpy` was the obvious choice and is the wrong one.** It publishes no wheel;
pip builds it from source, which needs a compiler present on every machine that
installs this. A dependency that can fail at install time, on Windows, for a
file format most corpora contain a handful of, is a bad trade against the
hundred lines below.

**Namespaces are not hardcoded.** ODF's text namespace URI has changed between
versions, and matching on it means a document written by a different office
suite silently yields nothing. Tags are matched on their local name instead, so
`{urn:...:text:1.0}p` and `{some-other-uri}p` are both a paragraph.

**Tables become rows of cells, not a wall of words.** A spreadsheet flattened
into prose is unsearchable in the way that matters: "12,400" next to "Leeds" is
a fact, and separating them with a space loses it. Cells are joined with tabs
and rows with newlines, which the chunker then splits on sensibly.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterable, Iterator, Optional
from xml.etree import ElementTree

from app.core.errors import raise_error
from app.core.logging import logger
from app.extract.base import (
    Document,
    DocumentBuilder,
    SourceKind,
    normalise_whitespace,
    register,
)

__all__ = ["OdfExtractor", "text_from_content", "CONTENT_MEMBER"]

log = logger.bind(component="extract.odf")

#: The one member that matters. `styles.xml` holds headers and footers, which
#: repeat on every page and would add noise to every document in the corpus.
CONTENT_MEMBER = "content.xml"

#: Guard against a zip bomb: a small archive that expands to gigabytes. The
#: limit is generous for a real document and ruinous for an attack.
MAX_CONTENT_BYTES = 64 * 1024 * 1024

#: Local names that end a block of text. Everything between two of these is one
#: paragraph as far as the reader is concerned.
_BLOCK = frozenset({"p", "h", "list-item"})

#: Local names whose text is not content: annotations, tracked changes and the
#: metadata Office suites leave behind.
_SKIP = frozenset({
    "tracked-changes", "annotation", "note-citation", "binary-data",
    "office-annotation", "editing-cycles", "generator",
})


def _local(tag: str) -> str:
    """`{namespace}name` -> `name`. See the module docstring on why."""
    return tag.rpartition("}")[2]


def _events(root: ElementTree.Element) -> Iterator[tuple[str, str]]:
    """`("start", name)`, `("text", s)` and `("end", name)` in document order.

    **Enter/exit events, not just text.** A first version yielded
    `(element-name, text)` and treated a `p` as ending a block the moment its
    text was seen - but ODF marks inline formatting with nested elements, so
    `<p>Findings from the <span>annual</span> inspection.</p>` holds "Findings
    from the" in the paragraph, "annual" in the span, and "inspection." in the
    span's *tail*. Ending the block on the first fragment produced

        Findings from the
        inspection. annual

    which is not what the document says. A block ends when the element closes,
    which needs an exit event, which needs this shape.

    Iterative with an explicit stack rather than recursive: a generated document
    can nest tables deeply enough to exceed Python's recursion limit, and a
    `RecursionError` inside an extractor takes an index worker down with it.
    """
    # Each entry is either an element to descend into, a literal string of tail
    # text, or a marker that an element has closed.
    stack: list[tuple[str, object]] = [("open", root)]

    while stack:
        kind, payload = stack.pop()

        if kind == "tail":
            yield "text", str(payload)
            continue
        if kind == "close":
            yield "end", str(payload)
            continue

        element = payload                                  # type: ignore[assignment]
        name = _local(element.tag)                          # type: ignore[union-attr]
        if name in _SKIP:
            continue

        # Pushed in reverse, because a stack pops backwards and document order
        # is the entire point of reading text out of a document.
        stack.append(("close", name))
        yield "start", name
        for child in reversed(list(element)):                # type: ignore[call-overload]
            if child.tail and child.tail.strip():
                stack.append(("tail", child.tail))
            stack.append(("open", child))

        if element.text and element.text.strip():           # type: ignore[union-attr]
            yield "text", element.text                      # type: ignore[union-attr]


def text_from_content(xml_bytes: bytes) -> str:
    """The readable text of a `content.xml`. Pure, so it is tested directly.

    Returns "" for markup that parses but holds nothing, which is a normal
    outcome for an empty presentation and not an error.
    """
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return ""

    if _local(root.tag) == "document":
        # A flat ODF file (`.fodt`...) holds styles, fonts and metadata beside the
        # body. Only the body is the document.
        for child in root:
            if _local(child.tag) == "body":
                root = child
                break

    lines: list[str] = []
    words: list[str] = []          # text seen since the last block ended
    cells: list[str] = []          # cells seen since the last row ended
    # **Depth, not a flag.** A cell can hold a table, and a paragraph inside a
    # nested cell must still belong to its own cell rather than to the prose.
    # Without this the `<text:p>` inside each cell closed first and flushed the
    # cell's text into `lines`, so a spreadsheet came out as one word per line
    # and "Licence" lost its "12400".
    cell_depth = 0

    def flush_block() -> str:
        nonlocal words
        joined = " ".join(words).strip()
        words = []
        return joined

    for kind, value in _events(root):
        if kind == "text":
            cleaned = value.strip()
            if cleaned:
                words.append(cleaned)
            continue

        if kind == "start":
            if value == "table-cell":
                cell_depth += 1
            continue

        if value == "table-cell":
            cell_depth = max(0, cell_depth - 1)
            # A cell's text belongs to its row, not to the surrounding prose.
            cell = flush_block()
            if cell:
                cells.append(cell)
        elif value == "table-row":
            # Tabs between cells, so "Licence" and "12400" stay adjacent. A
            # spreadsheet flattened into prose loses exactly the relationship
            # that made it worth indexing.
            if cells:
                lines.append("\t".join(cells))
                cells = []
        elif value in _BLOCK and not cell_depth:
            # Inside a cell, a paragraph is part of the cell - not a line of
            # its own. The cell's own close is what ends it.
            block = flush_block()
            if block:
                lines.append(block)

    if cells:
        lines.append("\t".join(cells))
    trailing = flush_block()
    if trailing:
        lines.append(trailing)

    return normalise_whitespace("\n".join(line for line in lines if line.strip()))


class OdfExtractor:
    """OpenDocument text, spreadsheets, presentations and their templates."""

    name = "odf"
    #: 2026-09-20: the OpenOffice 1.x family (`.sxw` text, `.sxc` sheets, `.sxi`
    #: presentations, `.sxd` drawings and their `.st*` templates) is the same ZIP
    #: with the same `content.xml`, and the reader matches tags on their local
    #: name, so it reads them unchanged. The *flat* forms (`.fodt`, `.fods`,
    #: `.fodp`, `.fodg`) are the same markup in one plain XML file, read below.
    extensions = frozenset({
        ".odt", ".ods", ".odp", ".ott", ".ots", ".otp", ".odg",
        ".sxw", ".sxc", ".sxi", ".sxd", ".stw", ".stc", ".sti", ".std",
        ".fodt", ".fods", ".fodp", ".fodg",
    })
    #: Extensions that are a single XML document rather than a ZIP.
    FLAT = frozenset({".fodt", ".fods", ".fodp", ".fodg"})
    reads_externally = False

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One document of `content.xml`'s text, tables as tab-separated rows.

        Not a zip or no `content.xml` is `ERR_FILE_CORRUPT`; a content part over
        `MAX_CONTENT_BYTES` (checked from the header) `ERR_FILE_TOO_LARGE`; a
        locked file `ERR_FILE_LOCKED`. An empty document yields nothing. Reads only.
        """
        try:
            if path.suffix.lower() in self.FLAT:
                if path.stat().st_size > MAX_CONTENT_BYTES:
                    raise_error("ERR_FILE_TOO_LARGE", "extract.odf", path=str(path),
                                details="a flat OpenDocument file over 64MB")
                    return
                content = path.read_bytes()
            else:
                with zipfile.ZipFile(path) as archive:
                    content = self._read_content(archive, path)
        except zipfile.BadZipFile as exc:
            # An ODF file that is not a zip is corrupt, truncated, or something
            # else wearing the extension. Precise, so the skip ledger says which.
            raise_error("ERR_FILE_CORRUPT", "extract.odf", path=str(path),
                        details=f"not a readable ODF archive: {exc}")
            return
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.odf", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.odf", path=str(path), details=str(exc))
            return

        if content is None:
            return

        text = text_from_content(content)
        if not text.strip():
            # Nothing to index is a skip reason, not a success. `base.extract`
            # turns an empty yield into ERR_NO_TEXT_LAYER, which says exactly
            # that and lands in the skip ledger where somebody can see it.
            return

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        builder.add(text, label="Content")
        builder.meta["format"] = "opendocument"
        yield builder.build()

    def _read_content(
        self, archive: zipfile.ZipFile, path: Path
    ) -> Optional[bytes]:
        """`content.xml`, or None with a precise error already raised."""
        try:
            info = archive.getinfo(CONTENT_MEMBER)
        except KeyError:
            raise_error(
                "ERR_FILE_CORRUPT", "extract.odf", path=str(path),
                details=f"the archive has no {CONTENT_MEMBER}, so it is not "
                        "an OpenDocument file whatever its extension says",
            )
            return None

        if info.file_size > MAX_CONTENT_BYTES:
            # Checked before reading, from the header - the whole point is to
            # not allocate the gigabyte in order to discover it is a gigabyte.
            raise_error(
                "ERR_FILE_TOO_LARGE", "extract.odf", path=str(path),
                details=f"{CONTENT_MEMBER} expands to {info.file_size / 1e6:.0f}MB",
            )
            return None

        return archive.read(CONTENT_MEMBER)


register(OdfExtractor())
