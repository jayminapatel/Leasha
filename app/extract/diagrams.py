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
from app.core.format_health import Requirement
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
    #: Both soft: `.vsdx` shape text is read by the built-in ZIP reader without
    #: `vsdx`, and `.vsd` falls back to the file name without `olefile`. Neither
    #: absence stops a file being indexed, so neither is `hard`.
    requires = (
        Requirement("vsdx", "vsdx",
                    provides="richer shape text from .vsdx", hard=False,
                    extensions=(".vsdx", ".vsdm")),
        Requirement("olefile", "olefile",
                    provides="title and author from older .vsd files", hard=False,
                    extensions=(".vsd",)),
    )

    def extract(self, path: Path) -> Iterable[Document]:
        """`.vsdx`/`.vsdm`: the file name, each page name and every shape label.
        `.vsd`: the name and OLE summary fields only, with an `ERR_NO_TEXT_LAYER`
        warning saying why. Never raises and never yields nothing. Reads only."""
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
    #: Soft: without mpxj a plan is still indexed by name and properties, which
    #: is what makes it findable. Both are needed together - mpxj is the Java
    #: library, jpype1 is the bridge - and the first missing one is reported.
    #: Reported in order, so a machine with neither is told to install mpxj
    #: first - `pip install mpxj` pulls jpype1 in with it, and naming the
    #: bridge first would send somebody after a dependency of the thing they
    #: actually need.
    requires = (
        Requirement("mpxj", "mpxj",
                    provides="task names from inside the plan", hard=False),
        Requirement("jpype", "jpype1",
                    provides="the Java bridge mpxj needs", hard=False),
    )

    def extract(self, path: Path) -> Iterable[Document]:
        """The plan's task names when the JVM route is switched on and works;
        otherwise the file name and summary fields with an `ERR_NO_TEXT_LAYER`
        warning explaining the gap. Never raises; reads only."""
        tasks = _mpp_tasks(path)
        if tasks is None:
            yield _metadata_document(
                path, "project",
                "Microsoft Project files have no open format specification. "
                "Reading the tasks needs the mpxj package, which runs a Java "
                "virtual machine inside this process - and a JVM fault kills "
                "the whole indexing run rather than skipping one file, which "
                "is why it is off by default. Set LEASHA_ENABLE_JVM=1 to turn "
                "it on. The plan is indexed by name and properties either way."
                if not _jvm_allowed() else
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


#: Environment switch that turns the JVM-backed `.mpp` reader on.
#:
#: **Off by default, and it took a five-hour run dying to justify it.**
#:
#: `mpxj` runs a Java virtual machine *inside this process* through JPype. A JVM
#: fault is therefore not a Python exception - it is a Windows access violation
#: that kills the interpreter where it stands. Observed:
#:
#:     # A fatal error has been detected by the Java Runtime Environment:
#:     #  EXCEPTION_ACCESS_VIOLATION (0xc0000005) at pc=..., pid=44512
#:     #  Problematic frame: C  [python312.dll+0x76c49]
#:
#: That ended an index run at 1,880 documents after five hours. Nothing in this
#: application could have caught it: `except Exception` does not see a segfault.
#:
#: **The hazard was already known.** `pyproject.toml` excludes JVM tests from
#: the default run for exactly this reason - *"JPype's startJVM can hard-crash
#: the host process... A crash in one optional integration must not cost the
#: report for everything else."* The same sentence applies with far more force
#: to a multi-day index over 600GB, where the cost is not a test report.
#:
#: So the reasoning that protected the test suite now protects the index run.
#: A `.mpp` is still indexed by name, author and title - which is what makes it
#: findable - and the task list is available to anybody who accepts the risk:
#:
#:     $env:LEASHA_ENABLE_JVM=1
#:
#: The real fix is to run mpxj in a subprocess, where a crash costs one file
#: instead of the run. Until that exists, this is the honest default.
JVM_SWITCH = "LEASHA_ENABLE_JVM"

#: `JVM_READERS_ENABLED` from Settings, read once. `None` until asked.
_SETTINGS_JVM: Optional[bool] = None


def _jvm_from_settings() -> bool:
    """The Settings control (`JVM_READERS_ENABLED`). Cached, never raises.

    2026-10-08 review: the environment variable was the whole interface, a
    tunable with no control (non-negotiable 11). The same shape as
    `pdf._pages_from_settings`: cached because it is asked per `.mpp` on a
    worker, and a missing or unreadable configuration is "off", which is what
    this did before it was configurable.
    """
    global _SETTINGS_JVM
    if _SETTINGS_JVM is None:
        try:
            from app.core.config import load_settings

            settings = load_settings(create_dirs=False, check_writable=False)
            _SETTINGS_JVM = bool(getattr(settings, "jvm_readers_enabled", False))
        except Exception:                        # noqa: BLE001 - a switch; off is the safe answer
            _SETTINGS_JVM = False
    return bool(_SETTINGS_JVM)


def _jvm_allowed() -> bool:
    """May mpxj start a JVM in this process? The environment variable is the
    override for one run; the Settings control is the ordinary route."""
    import os

    raw = os.environ.get(JVM_SWITCH)
    if raw is not None:
        return bool(raw)
    return _jvm_from_settings()


def _mpp_tasks(path: Path) -> Optional[list[str]]:
    """Task names via mpxj, or None if it cannot be read that way.

    `None` rather than `[]`, because "not available" and "a plan with no tasks"
    must lead to different documents - one says why it is unreadable, the other
    would silently claim the plan is empty.
    """
    if not _jvm_allowed():
        return None

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
            "full task list (mpxj)" if _jvm_allowed() and _mpp_reader() is not None
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
