"""Visio and Project files, without COM.

Layer: L2

Three formats, three very different answers, and pretending otherwise would be
worse than the gap:

| | what it is | what we get |
|---|---|---|
| `.vsdx` / `.vsdm` | a ZIP of XML, like every modern Office format | **all shape text, per page** |
| `.vsd` | a 2003-era OLE compound binary | title, author, subject - and the name |
| `.mpp` | a proprietary binary with no open specification | title, author, subject - and the name |

**Why not COM.** The rule in this project is that COM is a last resort, and it is
not needed here: `.vsdx` is a documented ZIP container, and the OLE summary
stream that `.vsd` and `.mpp` both carry is a documented Microsoft structure that
`olefile` reads in pure Python. COM would additionally require Visio and Project
to be *installed*, which they usually are not on the machine doing the indexing.

**Why a file we cannot read is still worth indexing.** A `.mpp` that the app has
never heard of does not exist as far as the person is concerned. One that is
indexed by name and summary metadata comes back when they search for the project
it belongs to, and the Files tab says plainly that its contents could not be
read. That is a far better answer than silence, and it costs almost nothing.

**`.mpp` contents are reachable, at a price.** The `mpxj` package wraps a Java
library and reads Project files properly - but it bundles 32 JARs and needs a
JVM and JPype. This application's entire premise is one process with no services,
so that is not a default. The hook is here and guarded; if somebody has Java,
`pip install mpxj jpype1` turns it on.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterable, Optional

from app.core.errors import AppError, make_error
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, register

__all__ = ["VisioExtractor", "ProjectExtractor", "summary_metadata", "vsdx_pages"]

log = logger.bind(component="extract.diagrams")

#: The OLE summary stream property ids worth having. Documented by Microsoft as
#: the standard PropertySet; `olefile` exposes them by name.
_SUMMARY_FIELDS = ("title", "subject", "author", "keywords", "comments", "last_saved_by")


def summary_metadata(path: Path) -> dict[str, str]:
    """Title, author and so on from an OLE compound file's summary stream.

    Works for `.vsd`, `.mpp`, `.doc`, `.xls` - anything in the pre-2007 Office
    container. Pure Python via `olefile`; no COM and no Office installation.

    Never raises. A file that is not an OLE container, or is damaged, returns an
    empty dict - the caller still has the filename, which is the point.
    """
    try:
        import olefile  # noqa: PLC0415 - optional, and only for these formats
    except ImportError:
        return {}

    try:
        if not olefile.isOleFile(str(path)):
            return {}
        with olefile.OleFileIO(str(path)) as ole:
            meta = ole.get_metadata()
            found: dict[str, str] = {}
            for field in _SUMMARY_FIELDS:
                value = getattr(meta, field, None)
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                if value and str(value).strip():
                    found[field] = str(value).strip()
            return found
    except Exception as exc:                    # noqa: BLE001 - see the docstring
        log.debug("no OLE summary for {}: {}", path.name, exc)
        return {}


def vsdx_pages(path: Path) -> list[tuple[str, list[str]]]:
    """`[(page name, [shape text, ...]), ...]` from a modern Visio file.

    Uses the `vsdx` package when it is installed, and falls back to reading the
    XML out of the ZIP directly when it is not. The fallback matters: a diagram
    is mostly *labels*, and labels are exactly what somebody searches for, so
    losing them to a missing optional dependency would be a poor trade.
    """
    try:
        import vsdx as vsdx_module  # noqa: PLC0415
    except ImportError:
        return _vsdx_pages_from_zip(path)

    try:
        with vsdx_module.VisioFile(str(path)) as diagram:
            pages: list[tuple[str, list[str]]] = []
            for page in diagram.pages:
                texts = [
                    shape.text.strip()
                    for shape in page.all_shapes
                    if getattr(shape, "text", None) and shape.text.strip()
                ]
                pages.append((str(getattr(page, "name", "") or ""), texts))
            return pages
    except Exception as exc:                    # noqa: BLE001
        log.debug("vsdx library could not read {}, falling back to XML: {}", path.name, exc)
        return _vsdx_pages_from_zip(path)


#: Visio stores shape labels in <Text> elements inside visio/pages/page*.xml.
_TEXT_TAG = "}Text"


def _vsdx_pages_from_zip(path: Path) -> list[tuple[str, list[str]]]:
    """Shape text straight out of the ZIP, with no third-party library.

    A `.vsdx` is an OPC package - a ZIP of XML - exactly like `.docx`. Reading
    the page parts directly means the feature degrades to "still works" rather
    than "unavailable" when an optional dependency is missing.
    """
    import xml.etree.ElementTree as ElementTree  # noqa: PLC0415

    pages: list[tuple[str, list[str]]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(
                name for name in archive.namelist()
                if name.startswith("visio/pages/page") and name.endswith(".xml")
            )
            for name in names:
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except ElementTree.ParseError:
                    continue
                texts: list[str] = []
                for element in root.iter():
                    if element.tag.endswith(_TEXT_TAG):
                        # itertext(), because Visio splits a label across child
                        # runs whenever any of it is styled differently - taking
                        # only `element.text` silently truncates at the first
                        # bold word.
                        joined = "".join(element.itertext()).strip()
                        if joined:
                            texts.append(joined)
                pages.append((name.rsplit("/", 1)[-1].removesuffix(".xml"), texts))
    except (zipfile.BadZipFile, OSError):
        return []
    return pages


def _metadata_document(path: Path, kind: str, note: str) -> Document:
    """A document made of what little is readable: name, then summary fields.

    The filename is included in the *text* deliberately. It means a search for
    "Barnsley layout" finds `Barnsley layout.mpp` through the ordinary search as
    well as through the Files tab, and it gives the chunker something to work
    with when the summary stream is empty - which it often is.
    """
    builder = DocumentBuilder(path)
    builder.add(path.stem, page=1)

    meta = summary_metadata(path)
    for field in _SUMMARY_FIELDS:
        value = meta.get(field)
        if value:
            builder.add(f"{field.replace('_', ' ').title()}: {value}", page=1)

    document = builder.build()
    document.meta.update({"format": kind, **meta})
    document.warnings = document.warnings + (make_error(
        "ERR_NO_TEXT_LAYER", "extract.diagrams",
        path=str(path),
        details=note,
        suggestion=(
            "The file is indexed by name and by its document properties, so it "
            "will still be found - but nothing inside it is searchable."
        ),
    ),)
    return document


class VisioExtractor:
    """Visio diagrams. `.vsdx` fully; `.vsd` by name and properties."""

    name = "visio"
    extensions = (".vsdx", ".vsdm", ".vsd")

    def extract(self, path: Path) -> Iterable[Document]:
        if path.suffix.lower() == ".vsd":
            # The 2003 binary format. Its shape text lives in an undocumented
            # compound-file layout; the summary stream is documented and is
            # what we can honestly read.
            yield _metadata_document(
                path, "visio-binary",
                "This is the older binary .vsd format, whose shape text has no "
                "open specification. Re-save it as .vsdx to make it searchable.",
            )
            return

        pages = vsdx_pages(path)
        builder = DocumentBuilder(path)
        builder.add(path.stem, page=1)
        for number, (page_name, texts) in enumerate(pages, start=1):
            if page_name:
                builder.add(page_name, page=number)
            for text in texts:
                builder.add(text, page=number)

        document = builder.build()
        document.meta.update({"format": "visio", "pages": len(pages)})
        if not any(texts for _name, texts in pages):
            document.warnings = document.warnings + (make_error(
                "ERR_NO_TEXT_LAYER", "extract.diagrams", path=str(path),
                details="The diagram contains no shape text - it may be all imagery.",
            ),)
        yield document


class ProjectExtractor:
    """Microsoft Project plans.

    `.mpp` has no open specification. The only complete reader is `mpxj`, which
    is a Java library: 32 bundled JARs plus a JVM plus JPype. This application
    is one process with no services, so that is offered, never assumed.

    Without it a plan is still indexed by name and by its document properties,
    which is what makes it findable at all.
    """

    name = "project"
    extensions = (".mpp", ".mpt")

    def extract(self, path: Path) -> Iterable[Document]:
        tasks = _mpp_tasks(path)
        if tasks is None:
            yield _metadata_document(
                path, "project",
                "Microsoft Project files have no open format specification. "
                "Reading the tasks needs the mpxj package, which is a Java "
                "library and needs a JVM.",
            )
            return

        builder = DocumentBuilder(path)
        builder.add(path.stem, page=1)
        for name in tasks:
            builder.add(name, page=1)
        document = builder.build()
        document.meta.update({"format": "project", "tasks": len(tasks), **summary_metadata(path)})
        yield document


#: The Java package mpxj publishes its reader under. It moved from
#: `net.sf.mpxj` to `org.mpxj` around version 14, and both are still in the wild
#: - so both are tried rather than one being assumed.
#:
#: This is not hypothetical caution. The first version of this code used only
#: `net.sf.mpxj` and would have failed on every modern install, silently, behind
#: a broad `except` that reported the file as merely unreadable. Verified
#: against mpxj 16.7.0, which is `org.mpxj`.
_MPXJ_PACKAGES = ("org.mpxj.reader", "net.sf.mpxj.reader")

#: Best-effort silence for log4j, which mpxj ships without a binding. Passed on
#: JVM start; unrecognised properties are ignored by the JVM, so none of these
#: can fail the run.
_JVM_QUIET_ARGS = (
    "-Dlog4j2.loggerContextFactory=org.apache.logging.log4j.simple.SimpleLoggerContextFactory",
    "-Dlog4j2.StatusLogger.level=OFF",
    "-Dorg.apache.logging.log4j.simplelog.StatusLogger.level=OFF",
)


def _mpp_reader():
    """Import mpxj's reader class, whichever package this version uses.

    Returns None when mpxj or a JVM is unavailable, which is the normal case -
    it is an optional extra, not a dependency.
    """
    try:
        import jpype  # noqa: PLC0415
        import jpype.imports  # noqa: PLC0415,F401 - registers the import hook
        import mpxj  # noqa: PLC0415,F401 - its import puts the JARs on the classpath
    except ImportError:
        return None

    try:
        if not jpype.isJVMStarted():
            # `import mpxj` has already called addClassPath for its 32 JARs, so
            # the JVM must be started *after* it and with no classpath override.
            #
            # The properties quieten log4j, which otherwise prints
            # "main ERROR Log4j API could not find a logging provider" straight
            # to stderr the first time mpxj reads a file. It is harmless - mpxj
            # ships the API without a binding - but it appears mid-run looking
            # exactly like a failure, which is its own kind of harm.
            jpype.startJVM(*_JVM_QUIET_ARGS)
    except Exception as exc:                    # noqa: BLE001
        log.debug("could not start a JVM for mpxj: {}", exc)
        return None

    for package in _MPXJ_PACKAGES:
        try:
            module = __import__(package, fromlist=["UniversalProjectReader"])
            return module.UniversalProjectReader
        except (ImportError, AttributeError):
            continue

    log.debug("mpxj is installed but neither {} could be imported", _MPXJ_PACKAGES)
    return None


def _mpp_tasks(path: Path) -> Optional[list[str]]:
    """Task names via mpxj, or None if it cannot be read that way.

    `None` rather than `[]`, because "not available" and "a plan with no tasks"
    must lead to different documents - one says why it is unreadable, the other
    would silently claim the plan is empty.
    """
    reader = _mpp_reader()
    if reader is None:
        return None

    try:
        project = reader().read(str(path))
        if project is None:
            return None
        return [
            str(task.getName())
            for task in project.getTasks()
            if task is not None and task.getName()
        ]
    except Exception as exc:                    # noqa: BLE001
        log.debug("mpxj could not read {}: {}", path.name, exc)
        return None


def readers_available() -> dict[str, str]:
    """What each format can currently do on this machine. For `doctor.py`.

    Reported rather than assumed, because every one of these is optional and a
    person needs to know whether their diagrams are being read or merely named.
    """
    try:
        import olefile  # noqa: PLC0415,F401
        ole = True
    except ImportError:
        ole = False
    try:
        import vsdx  # noqa: PLC0415,F401
        library = True
    except ImportError:
        library = False

    return {
        ".vsdx": (
            "full shape text (vsdx library)" if library
            else "full shape text (built-in ZIP reader)"
        ),
        ".vsd": (
            "name + document properties" if ole
            else "name only - install olefile for title and author"
        ),
        ".mpp": (
            "full task list (mpxj)" if _mpp_reader() is not None
            else ("name + document properties" if ole
                  else "name only - install olefile for title and author")
        ),
    }


def unreadable_note(document: Document) -> Optional[AppError]:
    """The warning explaining why a document has no searchable contents, if any."""
    for warning in document.warnings:
        if warning.code == "ERR_NO_TEXT_LAYER":
            return warning
    return None


# Instances, not classes - `register` takes the thing that will be called, and
# the rest of this package registers the same way.
register(VisioExtractor())
register(ProjectExtractor())
