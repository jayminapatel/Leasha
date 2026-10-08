r"""E-books: .epub and .fb2. No dependency.

Layer: L2

Both formats were read by `pandoc` until now - an external binary, for two
formats that are a zip of XHTML and a single XML file respectively. Neither
needs one, and non-negotiable 12 says a library or the standard library comes
first. See docs/WORKORDER-libraries-before-converters.md.

**EPUB is read in spine order, not manifest order.** The manifest lists the
files in the archive in no particular order; the spine lists them in *reading*
order. Indexing the manifest gives a book whose chapters are shuffled, which
only shows up in a snippet that reads like two sentences from different
chapters glued together - a bug that survives a long time because the text is
all present and only the order is wrong.

**EPUB's XHTML is parsed by `html.parser`, not `ElementTree`.** In theory it is
well-formed XML. In practice real books contain `&nbsp;` without declaring it,
and a strict XML parser rejects the entire chapter over one entity. `ebooklib`
was the alternative and is a real, maintained wheel - but it wraps the same two
standard-library modules used here, and this is a hundred lines. The same call
was made for ODF against `odfpy` and it was right.

**FB2's `<binary>` elements are skipped, and that is not an optimisation.** A
FictionBook stores its cover and illustrations as base64 *inside the document*.
A 2MB cover becomes 2.7MB of base64 text, which without this would be indexed
as the body of the book: every chunk would be random letters, the embedding
would be meaningless, and the actual prose would be a rounding error in the
document. It is the single thing about the format that has to be got right.
"""

from __future__ import annotations

import posixpath
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
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

__all__ = ["EpubExtractor", "Fb2Extractor", "text_from_xhtml", "text_from_fb2"]

log = logger.bind(component="extract.ebook")

#: Zip-bomb guard, matching `odf.MAX_CONTENT_BYTES`: generous for a real book,
#: ruinous for an archive built to expand.
MAX_MEMBER_BYTES = 64 * 1024 * 1024

#: Total text kept from one book. A reference work can be tens of megabytes of
#: prose, and one file that becomes ten thousand chunks crowds a corpus out of
#: its own search results.
MAX_TEXT_CHARS = 4 * 1024 * 1024

#: Where an EPUB is required to say which file describes it. The only fixed
#: path in the format - everything else is found by following pointers.
CONTAINER_MEMBER = "META-INF/container.xml"

#: HTML elements whose end means a line break rather than a space.
_HTML_BLOCK = frozenset({
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "section", "article", "td", "th", "figcaption",
})

#: Elements whose text is markup, not content.
_HTML_SKIP = frozenset({"script", "style", "head", "title"})

#: FB2 local names that end a block of text.
_FB2_BLOCK = frozenset({"p", "v", "subtitle", "text-author", "title", "td", "th"})

#: FB2 local names to drop entirely. `binary` is the important one - see the
#: module docstring.
_FB2_SKIP = frozenset({"binary", "image", "stylesheet"})


def _local(tag: str) -> str:
    """`{namespace}name` -> `name`.

    FictionBook's namespace URI differs between the 2.0 and 2.1 drafts and some
    writers omit it entirely, so matching on the URI silently yields nothing for
    a file another reader opens happily. Same lesson as ODF.
    """
    return tag.rpartition("}")[2]


class _TextHtmlParser(HTMLParser):
    """XHTML to text, keeping paragraph boundaries.

    `convert_charrefs=True` (the default) is what makes `&nbsp;` and `&mdash;`
    become characters rather than errors - the whole reason this is not
    `ElementTree`.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _HTML_SKIP:
            self._suppress += 1
        elif tag in _HTML_BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_SKIP:
            self._suppress = max(0, self._suppress - 1)
        elif tag in _HTML_BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppress and data.strip():
            self.parts.append(data)


def text_from_xhtml(markup: str) -> str:
    """Readable text from one XHTML chapter. Pure, so it is tested directly."""
    parser = _TextHtmlParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:                                     # noqa: BLE001
        # HTMLParser is forgiving, but a truncated file can still end mid-tag.
        # Whatever was parsed before the failure is still worth keeping - half a
        # chapter is a better search result than none.
        log.debug("xhtml parse ended early; keeping what was read")
    return normalise_whitespace("".join(parser.parts))


def text_from_fb2(xml_bytes: bytes) -> tuple[str, dict[str, str]]:
    """`(text, metadata)` from a FictionBook. Pure, so it is tested directly.

    Returns `("", {})` for markup that does not parse, which `extract` turns
    into a precise error - this function does not raise.
    """
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return "", {}

    meta: dict[str, str] = {}
    title_info = None
    for element in root.iter():
        if _local(element.tag) == "title-info":
            title_info = element
            break
    if title_info is not None:
        for child in title_info:
            name = _local(child.tag)
            if name == "book-title" and (child.text or "").strip():
                meta["title"] = child.text.strip()          # type: ignore[union-attr]
            elif name == "author":
                parts = [
                    (piece.text or "").strip()
                    for piece in child
                    if _local(piece.tag) in {"first-name", "middle-name", "last-name"}
                ]
                joined = " ".join(part for part in parts if part)
                if joined:
                    meta["author"] = joined

    lines: list[str] = []
    for body in root:
        if _local(body.tag) != "body":
            continue
        lines.extend(_fb2_blocks(body))

    return normalise_whitespace("\n".join(lines)), meta


def _fb2_blocks(body: ElementTree.Element) -> list[str]:
    """One line per block element, in document order.

    Iterative with an explicit stack rather than recursive, for the reason
    `odf._events` gives: a generated book can nest deeply enough to blow the
    recursion limit, and a `RecursionError` inside an extractor takes an index
    worker down with it.
    """
    lines: list[str] = []
    stack: list[tuple[str, object]] = [("open", body)]

    while stack:
        kind, payload = stack.pop()

        if kind == "text":
            text = str(payload).strip()
            if text:
                lines.append(text)
            continue

        element = payload                                  # type: ignore[assignment]
        name = _local(element.tag)                          # type: ignore[union-attr]
        if name in _FB2_SKIP:
            continue

        if name in _FB2_BLOCK:
            # `itertext` gathers this block's own text plus every inline
            # `<emphasis>` and `<strong>` inside it, which is exactly one
            # paragraph. Nothing below it is a block in FB2's schema.
            text = " ".join(part.strip() for part in element.itertext() if part.strip())
            if text:
                lines.append(text)
            continue

        for child in reversed(list(element)):                # type: ignore[call-overload]
            if child.tail and child.tail.strip():
                stack.append(("text", child.tail))
            stack.append(("open", child))

    return lines


class Fb2Extractor:
    """FictionBook 2. One XML document, read with the standard library."""

    name = "fb2"
    extensions = frozenset({".fb2"})
    reads_externally = False

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One document of the book's prose (capped at `MAX_TEXT_CHARS`) with
        title and author in `meta`. Not FictionBook XML at all is
        `ERR_FILE_CORRUPT`; a book with metadata but no prose yields nothing,
        which `base.extract` reports as `ERR_NO_TEXT_LAYER`. Reads only."""
        try:
            raw = path.read_bytes()
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.ebook", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.ebook", path=str(path), details=str(exc))
            return

        text, meta = text_from_fb2(raw)
        if not text.strip():
            if not meta:
                raise_error(
                    "ERR_FILE_CORRUPT", "extract.ebook", path=str(path),
                    details="not readable FictionBook XML, whatever the extension says",
                )
            return

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        builder.add(text[:MAX_TEXT_CHARS], label="Content")
        builder.meta["format"] = "fictionbook"
        builder.meta.update(meta)
        yield builder.build()


class EpubExtractor:
    """EPUB 2 and 3. A zip of XHTML, read in spine order."""

    name = "epub"
    extensions = frozenset({".epub"})
    reads_externally = False

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One document: the chapters in spine order, capped at `MAX_TEXT_CHARS`.

        Not a zip, no container.xml, or an unparseable package document is
        `ERR_FILE_CORRUPT`; a locked file `ERR_FILE_LOCKED`. A missing or
        oversized chapter costs that chapter only. Reads the zip; writes nothing.
        """
        try:
            with zipfile.ZipFile(path) as archive:
                chapters, meta = self._read(archive, path)
        except zipfile.BadZipFile as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.ebook", path=str(path),
                        details=f"not a readable EPUB archive: {exc}")
            return
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.ebook", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.ebook", path=str(path), details=str(exc))
            return

        if chapters is None:
            return                                          # error already raised

        text = normalise_whitespace("\n".join(chapters))
        if not text.strip():
            return                                          # base.extract -> ERR_NO_TEXT_LAYER

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        builder.add(text[:MAX_TEXT_CHARS], label="Content")
        builder.meta["format"] = "epub"
        builder.meta.update(meta)
        yield builder.build()

    def _read(
        self, archive: zipfile.ZipFile, path: Path
    ) -> tuple[Optional[list[str]], dict[str, str]]:
        """Chapters in spine order, plus title and author from the OPF."""
        opf_name = self._opf_path(archive, path)
        if opf_name is None:
            return None, {}

        try:
            opf_root = ElementTree.fromstring(self._member(archive, opf_name))
        except ElementTree.ParseError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.ebook", path=str(path),
                        details=f"the package document {opf_name} is not valid XML: {exc}")
            return None, {}
        except KeyError:
            raise_error("ERR_FILE_CORRUPT", "extract.ebook", path=str(path),
                        details=f"the archive names {opf_name} but does not contain it")
            return None, {}

        base = posixpath.dirname(opf_name)
        meta = self._metadata(opf_root)

        # id -> href, from the manifest.
        hrefs: dict[str, str] = {}
        for element in opf_root.iter():
            if _local(element.tag) != "item":
                continue
            item_id = element.get("id")
            href = element.get("href")
            media = (element.get("media-type") or "").lower()
            if item_id and href and ("html" in media or not media):
                hrefs[item_id] = href

        # Spine order is reading order. See the module docstring.
        order: list[str] = []
        for element in opf_root.iter():
            if _local(element.tag) != "itemref":
                continue
            ref = element.get("idref")
            if ref and ref in hrefs:
                order.append(hrefs[ref])

        # A book with no spine is malformed, but its chapters are still there.
        # Manifest order is a poor second and much better than nothing.
        if not order:
            order = list(hrefs.values())

        chapters: list[str] = []
        total = 0
        for href in order:
            member = posixpath.normpath(posixpath.join(base, href)) if base else href
            try:
                raw = self._member(archive, member)
            except KeyError:
                # A manifest entry pointing at a missing file is common in
                # hand-made books. Skip the chapter, keep the book.
                log.debug("epub member missing", member=member)
                continue
            except ValueError:
                continue                                    # oversized, already logged
            chapter = text_from_xhtml(raw.decode("utf-8", errors="replace"))
            if chapter:
                chapters.append(chapter)
                total += len(chapter)
                if total >= MAX_TEXT_CHARS:
                    break

        return chapters, meta

    def _opf_path(self, archive: zipfile.ZipFile, path: Path) -> Optional[str]:
        """The package document's path, from `META-INF/container.xml`."""
        try:
            container = ElementTree.fromstring(self._member(archive, CONTAINER_MEMBER))
        except KeyError:
            raise_error(
                "ERR_FILE_CORRUPT", "extract.ebook", path=str(path),
                details=f"the archive has no {CONTAINER_MEMBER}, so it is not "
                        "an EPUB whatever its extension says",
            )
            return None
        except (ElementTree.ParseError, ValueError) as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.ebook", path=str(path),
                        details=f"{CONTAINER_MEMBER} is not valid XML: {exc}")
            return None

        for element in container.iter():
            if _local(element.tag) == "rootfile":
                full_path = element.get("full-path")
                if full_path:
                    return full_path

        raise_error("ERR_FILE_CORRUPT", "extract.ebook", path=str(path),
                    details=f"{CONTAINER_MEMBER} names no rootfile")
        return None

    def _member(self, archive: zipfile.ZipFile, name: str) -> bytes:
        """One archive member, size-checked from its header before reading."""
        info = archive.getinfo(name)                         # KeyError if absent
        if info.file_size > MAX_MEMBER_BYTES:
            # Checked from the header: the point is not to allocate the gigabyte
            # in order to discover it is a gigabyte.
            log.warning("epub member too large, skipped",
                        member=name, size=info.file_size)
            raise ValueError(f"{name} expands to {info.file_size} bytes")
        return archive.read(name)

    @staticmethod
    def _metadata(opf_root: ElementTree.Element) -> dict[str, str]:
        meta: dict[str, str] = {}
        for element in opf_root.iter():
            name = _local(element.tag)
            value = (element.text or "").strip()
            if not value:
                continue
            if name == "title" and "title" not in meta:
                meta["title"] = value
            elif name == "creator" and "author" not in meta:
                meta["author"] = value
        return meta


register(Fb2Extractor())
register(EpubExtractor())
