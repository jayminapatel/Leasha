r"""Reading files in a process of their own: one reader process per extraction thread.

Layer: L3

Work order 0x section 5b. **The extraction threads share one interpreter
lock.** Parsing mail, unzipping a `.docx` and cutting text into passages are
all Python work, so four reader threads mostly take turns - and the writing
thread, which only needs the lock for a moment between SQLite statements,
waits behind them (the 5d note in `pipeline.py` measured a run using under one
core of four). A process of its own has a lock of its own. Measured on the
prototype: -30% to -43% wall time on the benchmark corpus.

**The shape.** Each extraction thread gets one child, started when the thread
starts and kept for the whole run, so the start-up cost (a fresh interpreter
and the reader imports, well under a second) is paid once per thread rather
than once per file. The thread sends the child one file at a time; the child
reads it, cuts it into passages, and sends the documents back **one at a
time, as they are read**. Nothing is buffered whole: a 4GB mailbox streams
exactly as it does in-process, and when the writer falls behind the pipe
fills, the child blocks on its next write, and memory stays flat - the same
back-pressure the in-process generator gets from the bounded results queue.

**Only readers that are pure file parsing go to a child** (`PROCESS_READERS`).
The rest stay on the thread, where their process-wide state lives: OCR and
picture models and the GPU lock that keeps them apart (`gpu_exclusive`), the
warm LibreOffice session, Outlook, the media backlog, and the zip reader, which
hands each member to *any* reader. A file whose reader is not on the list is
read in-process exactly as before, so switching this on can only change where
the listed readers run, never what any reader produces.

**What a crash costs.** In-process, a reader that crashes the interpreter (a
native library fault on a damaged file) takes the window with it. Here it
takes one child: the file is recorded as skipped with
`ERR_READER_PROCESS_ENDED`, a fresh child is started for the next file, and
the run carries on (non-negotiable #3).

**Inner progress still shows.** The child's readers write their position into
a frame stack exactly as they do in-process (`app.extract.progress`); a small
thread in the child sends a copy of the stack four times a second while it
changes, and the parent writes it into the thread's own slot, so the Indexing
page reads `Archive.mbox › message 812 of 20,000` either way.

**The protocol** is length-prefixed `pickle` frames over the child's own
standard input and output. `pickle` is safe here because both ends are this
application: nothing but the child ever writes to that pipe. The child moves
its real standard output out of the way first, so a stray `print` in a library
lands on standard error instead of corrupting the stream.

Started as `python -m app.index.read_process` (an argument list, never a
shell), with the window's own interpreter - `pythonw.exe` on Windows, so no
console window appears - and the same on macOS, where `sys.executable` is the
venv's `python`.
"""

from __future__ import annotations

import os
import pickle
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

log = logger.bind(component="index.read_process")

__all__ = [
    "PROCESS_READERS",
    "ReaderProcess",
    "RemoteDocument",
    "reads_in_process",
    "main",
]

#: Readers that only parse a file's bytes: no model, no GPU, no converter, no
#: Outlook, no settings, no process-wide cache. Named by class so the list
#: reads as a decision, and `test_read_process.py` checks every name is still
#: a registered reader - a rename cannot quietly empty it.
#:
#: Deliberately **not** here: `ArchiveExtractor` (a zip member can be a
#: picture that needs OCR), `PdfExtractor` (the OCR ladder), `OcrExtractor`,
#: `RawExtractor`, the media readers, `DocExtractor` / `PptExtractor` /
#: `XlsExtractor` (the warm LibreOffice session), `MobiExtractor` (falls back
#: to a converter), `PstExtractor` (Outlook, or libpff's own handle), and
#: everything else not listed. Each can join once it is shown to carry no
#: process-wide state.
PROCESS_READERS = frozenset({
    # Order `reader-process-isolation` (2026-10-09): PDF joins the list. Its
    # text layer is pure PyMuPDF parsing, which is exactly what faulted inside
    # `mupdfcpp64.dll` and ended the owner's overnight run; its OCR half
    # declines in a child (`pdf._ocr_pages`, `in_reader_process`), so the
    # models stay with the indexer and the pictures pass reads scanned rows
    # back in the parent as it always did.
    "PdfExtractor",
    "PlainTextExtractor",
    "DocxExtractor",
    "XlsxExtractor",
    "PptxExtractor",
    "OdfExtractor",
    "RtfExtractor",
    "EmlExtractor",
    "MsgExtractor",
    "MboxExtractor",
    "EmlxExtractor",
    "OlmExtractor",
    "EpubExtractor",
    "Fb2Extractor",
})

#: Seconds between the child's progress copies while a file is being read.
_FRAMES_EVERY_S = 0.25
#: Seconds `close()` waits for a child to leave by itself before ending it.
_CLOSE_WAIT_S = 2.0
#: How long a child is given to say it is ready (`ReaderProcess.wait_ready`).
#: A constant (non-negotiable 11). A fresh interpreter and the reader imports
#: take about a second on a quiet machine and were measured at over two on
#: this laptop under load (2026-09-30); a minute is far past anything a
#: working start takes, and short enough that a child which can never start -
#: a broken install, a security product holding the interpreter - costs a
#: minute once per reader, after which that reader reads in its own thread.
START_LIMIT_S = 60.0
#: How often `wait_ready` looks up from its wait, to notice a stop.
_READY_POLL_S = 0.05

# Four bytes of length before each frame: one document's passages are kilobytes
# to a few megabytes, nowhere near the 4GB this allows, and a fixed-width
# header is what lets `_read_exact` know how much to wait for.
_HEADER = struct.Struct("<I")


def reads_in_process(path: Path) -> bool:
    """Would this file's reader run in a reader process? False for any doubt."""
    from app.extract.base import extractor_for, reads_externally

    extractor = extractor_for(path)
    if extractor is None or reads_externally(path):
        return False
    return type(extractor).__name__ in PROCESS_READERS


# ---------------------------------------------------------------------------
# Framing - shared by both ends
# ---------------------------------------------------------------------------

def _write(stream: Any, message: Any) -> None:
    """One pickled frame onto `stream`, flushed. Raises what the pipe raises
    (`BrokenPipeError`, `ValueError` on a closed stream) - the callers decide
    what a gone peer means."""
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(_HEADER.pack(len(data)) + data)
    stream.flush()


def _read_exact(stream: Any, size: int) -> Optional[bytes]:
    """`size` bytes, or None at the end of the stream (the other end went)."""
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read(stream: Any) -> Any:
    """The next message, or None when the other end has gone."""
    head = _read_exact(stream, _HEADER.size)
    if head is None:
        return None
    body = _read_exact(stream, _HEADER.unpack(head)[0])
    if body is None:
        return None
    return pickle.loads(body)


# ---------------------------------------------------------------------------
# The parent's side
# ---------------------------------------------------------------------------

class RemoteDocument:
    """A document read in a child: the four things the pipeline uses of one.

    The pipeline reads `key`, `source_kind`, `meta` and `warnings` from a
    `Document` (`Pipeline._extract_stream`), and the passages arrive already
    cut, so the text itself never crosses the pipe twice.
    """

    __slots__ = ("key", "source_kind", "meta", "warnings")

    def __init__(self, key: str, source_kind: str, meta: dict[str, Any],
                 warnings: tuple[Any, ...]) -> None:
        self.key = key
        self.source_kind = source_kind
        self.meta = meta
        self.warnings = warnings


class _Hello:
    """One child's first message: has it said it is ready, or gone without?"""

    __slots__ = ("done", "ready")

    def __init__(self) -> None:
        #: Set once the child's first message has arrived, or its pipe closed.
        self.done = threading.Event()
        self.ready = False


class ReaderProcess:
    r"""One child that reads files for one extraction thread.

    Used only from the thread that owns it. `start()` launches the child
    without waiting for it, so the child's start-up overlaps the walk;
    `wait_ready()` waits for it to say it is ready, for a bounded time.

    **Start-up is not reading (2026-09-30).** `read()` used to send the file
    and wait for the child's "ready" in the same loop as the file's documents,
    so the seconds a child took to start were the first file's reader time -
    and the file watchdog (`file_watch`) times a file by exactly that. A
    child slower to start than the limit timed out the file it was started
    for; and since a child that is ended for a time-out is replaced by a fresh
    one, which has to start in its turn, *every file after the first stuck one
    timed out too*. The pipeline now waits for the child (`wait_ready`) before
    the file's clock starts, under this module's own `START_LIMIT_S`.
    """

    def __init__(self, *, low_priority: bool = True, python: Optional[str] = None,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.low_priority = bool(low_priority)
        from app.core.osbridge.stdio import own_python

        self.python = python or own_python()
        self._popen = popen
        self._proc: Any = None
        #: The current child's `_Hello`, or None when there is no child.
        self._hello: Optional[_Hello] = None
        #: Numbers each file sent, so a progress copy the child sent just as
        #: one file finished is never shown against the next one.
        self._sequence = 0
        #: True while a `read()` has not finished or been closed. A new read
        #: while one is still open means the old one was abandoned without
        #: being closed; its child is ended first, so the new file can never
        #: receive the old file's leftover documents.
        self._busy = False
        #: How many children this reader has started - more than one means a
        #: child ended part-way (a crash, or a file abandoned mid-read).
        self.started = 0
        #: Set by `kill_child` (0z lane B: a time limit or Force skip), so the
        #: child's end is logged as what it was rather than as a crash.
        self._killed = False

    @property
    def reading(self) -> bool:
        """True while a `read()` is under way - the file is in the child."""
        return self._busy

    def kill_child(self) -> None:
        r"""End the child now, from **another** thread. Never raises, never waits.

        Work order 0z lane B: the file watchdog's way of freeing a thread whose
        file has run past its time limit (`app/index/file_watch.py`). Only the
        process is ended here; the owning thread, blocked reading the pipe,
        sees it close, and its own `read()` tidies up (`_ended`, `_abandon`)
        and starts a fresh child for the next file - so nothing is shared
        between the two threads except the one `kill` call.
        """
        proc = self._proc
        self._killed = True
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:                              # noqa: BLE001 - already gone
            pass

    # -- life ----------------------------------------------------------------

    def argv(self) -> list[str]:
        """The child's command line: this module as `-m`, with the window's own
        interpreter, so it imports the same `app` package."""
        argv = [self.python, "-m", "app.index.read_process"]
        if self.low_priority:
            argv.append("--low-priority")
        return argv

    def start(self) -> None:
        """Launch the child if there is none. Never waits for it."""
        if self._proc is not None and self._proc.poll() is None:
            return
        # The folder that holds the `app` package, so `-m` finds it whatever
        # the window's own working folder is.
        here = Path(__file__).resolve().parents[2]
        proc = self._proc = self._popen(
            self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(here), close_fds=True,
        )
        hello = self._hello = _Hello()
        self.started += 1
        # The child's first message is read here, off the owning thread, so
        # that waiting for it can be bounded and can notice a stop: a read
        # from a pipe cannot be given a time limit, but an `Event` can.
        # Nothing else reads the pipe until `hello.done` is set.
        threading.Thread(target=_await_hello, args=(proc, hello),
                         name="read-process-hello", daemon=True).start()

    @property
    def ready(self) -> bool:
        """True when there is a child and it has said it is ready to read."""
        proc, hello = self._proc, self._hello
        return bool(proc is not None and hello is not None and hello.ready
                    and proc.poll() is None)

    def wait_ready(self, *, cancelled: Optional[Callable[[], bool]] = None) -> bool:
        r"""Start a child if there is none, and wait until it is ready to read.

        True when it is. False when `cancelled()` said to stop waiting (the
        run is stopping); the child is left as it is, for `close()`.

        **Bounded by `START_LIMIT_S`.** A child that has not said it is ready
        by then, or that ended before saying so, is ended and
        `ERR_READER_PROCESS_START` is raised - which says what happened and
        what the pipeline does about it. No file is named, because no file
        was involved: nothing had been sent to the child yet.
        """
        self.start()
        proc, hello = self._proc, self._hello
        limit = float(START_LIMIT_S)
        began = time.monotonic()
        while not hello.done.wait(_READY_POLL_S):
            if cancelled is not None and cancelled():
                return False
            if time.monotonic() - began >= limit:
                self._abandon()
                raise AppErrorException(make_error(
                    "ERR_READER_PROCESS_START", "index.read_process",
                    why=f"it was not ready after {limit:.0f} seconds",
                    details=f"No 'ready' from the reader process in {limit:.0f}s; "
                            "it was ended."))
        if hello.ready:
            return True
        code = None
        try:
            code = proc.wait(timeout=_CLOSE_WAIT_S)
        except Exception:                              # noqa: BLE001
            pass
        self._abandon()
        raise AppErrorException(make_error(
            "ERR_READER_PROCESS_START", "index.read_process",
            why="it ended before it was ready",
            details=f"The reader process exited with code {code} before saying "
                    "it was ready."))

    def close(self) -> None:
        """Ask the child to leave, and end it if it does not. Never raises."""
        proc, self._proc = self._proc, None
        self._hello = None
        if proc is None:
            return
        try:
            _write(proc.stdin, ("quit",))
            proc.stdin.close()
        except Exception:                              # noqa: BLE001 - already gone
            pass
        try:
            proc.wait(timeout=_CLOSE_WAIT_S)
        except Exception:                              # noqa: BLE001
            self._kill(proc)
        self._close_pipes(proc)

    def _abandon(self) -> None:
        """End the child now: it is part-way through a file nobody wants."""
        proc, self._proc = self._proc, None
        self._hello = None
        if proc is not None:
            self._kill(proc)
            self._close_pipes(proc)

    @staticmethod
    def _kill(proc: Any) -> None:
        try:
            proc.kill()
            proc.wait(timeout=_CLOSE_WAIT_S)
        except Exception:                              # noqa: BLE001 - nothing more to do
            pass

    @staticmethod
    def _close_pipes(proc: Any) -> None:
        for pipe in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:                          # noqa: BLE001
                pass

    # -- reading -------------------------------------------------------------

    def read_raw(self, path: Path, *, frames: Optional[list[Any]] = None) -> Iterator[Any]:
        """Each `Document` of `path` whole - text and segments, not passages.

        Order `reader-process-isolation` (2026-10-09): what the archive reader
        asks for one member, because it re-wraps the document with the
        archive's own path and the pipeline chunks it afterwards. Everything
        else - the sequence numbers, the frames, the kill, the error on a dead
        child - is `read()`'s.
        """
        for document in self.read(path, frames=frames, raw=True):
            yield document

    def read(self, path: Path, *, resume_from: int = 0,
             resume_extra: Optional[dict[str, Any]] = None,
             frames: Optional[list[Any]] = None, raw: bool = False,
             ) -> Iterator[Any]:
        r"""Each document of `path` with its passages, as the child reads them.

        `raw=True` (see `read_raw`) yields the `Document` itself instead of
        `(RemoteDocument, chunks)`.

        Raises `AppErrorException` exactly where `extract()` would (the
        child's reader raised it), or with `ERR_READER_PROCESS_ENDED` when the
        child ended without finishing the file. Any other exception in the
        child's reader comes back as a `RuntimeError` naming it, which the
        pipeline's per-file guard turns into a skip, as it would in-process.

        **Closed part-way** (the run stopped or paused between documents), the
        child is ended rather than drained: it may be deep in a large mailbox,
        and draining would read the rest of it for nothing. The next file
        starts a fresh one.
        """
        if self._busy:
            self._abandon()
        # For a caller that did not wait first (the pipeline does, before the
        # file's clock starts - see the class docstring). A no-op when the
        # child is already ready.
        self.wait_ready()
        proc = self._proc
        self._busy = True
        self._killed = False
        self._sequence += 1
        sequence = self._sequence
        finished = False
        try:
            # Order `pictures-process-isolation` (2026-10-09): a seventh field,
            # which pass this is (`reading.current().images`), so the child
            # holds a scanned PDF on the text pass and reads it on the images
            # pass exactly as the thread would. A six-field request still
            # means "read everything", as a five-field one still means chunks.
            from app.extract import reading as _reading

            try:
                _write(proc.stdin, ("read", sequence, str(path), int(resume_from),
                                    resume_extra, "raw" if raw else "chunks",
                                    _reading.current().images))
            except (OSError, ValueError):
                raise self._ended(path)
            while True:
                message = _read(proc.stdout)
                if message is None:
                    raise self._ended(path)
                kind = message[0]
                if kind == "frames":
                    if frames is not None and message[1] == sequence:
                        frames[:] = [_frame_from(item) for item in message[2]]
                elif kind == "doc":
                    _, key, source_kind, meta, warnings, chunks = message
                    yield RemoteDocument(key, source_kind, meta, tuple(warnings)), chunks
                elif kind == "rawdoc":
                    yield message[1]
                elif kind == "ocr":
                    # Order `pictures-process-isolation`: the child rendered a
                    # scanned page and asks for its text. This thread answers
                    # through `ocr_image` - the run's helper process when it
                    # has one - so the child never loads the engine.
                    from app.extract.ocr import OcrResult, ocr_image

                    try:
                        answer = ocr_image(message[1])
                    except Exception:              # never raises; belt and braces
                        answer = OcrResult()
                    try:
                        _write(proc.stdin, ("ocr-result", answer))
                    except (OSError, ValueError):
                        raise self._ended(path) from None
                elif kind == "end":
                    finished = True
                    error, failure = message[1], message[2]
                    if frames is not None:
                        frames.clear()
                    if error is not None:
                        raise AppErrorException(error)
                    if failure is not None:
                        raise RuntimeError(failure)
                    return
        finally:
            self._busy = False
            if not finished:
                self._abandon()

    def _ended(self, path: Path) -> AppErrorException:
        """The child went without finishing `path`. Records how, and resets."""
        proc, code = self._proc, None
        if proc is not None:
            try:
                code = proc.wait(timeout=_CLOSE_WAIT_S)
            except Exception:                          # noqa: BLE001
                code = None
        self._abandon()
        if self._killed:
            log.info("the reader process reading {} was ended: the file ran past "
                     "its time limit, or was force-skipped", Path(path).name)
        else:
            log.warning("the reader process ended while reading {} (exit code {})",
                        Path(path).name, code)
        return AppErrorException(make_error(
            "ERR_READER_PROCESS_ENDED", "index.read_process", path=str(path),
            details=f"The reader process exited with code {code}."))


def _await_hello(proc: Any, hello: _Hello) -> None:
    """Read one child's first message - "ready" - and say it has arrived.

    Runs on a thread of its own, started with the child. It ends when the
    message arrives or the pipe closes (the child ended, or was ended), so it
    never outlives the child.
    """
    try:
        message = _read(proc.stdout)
    except Exception:                                  # noqa: BLE001 - the pipe went
        message = None
    hello.ready = bool(message) and message[0] == "ready"
    hello.done.set()


def _frame_from(item: dict[str, Any]) -> Any:
    """A `progress.Frame` rebuilt from the plain values the child sent."""
    from app.extract.progress import Frame

    frame = Frame(item.get("kind", ""), item.get("name", ""),
                  unit=item.get("unit", ""), total=item.get("total"),
                  stage=item.get("stage", ""))
    frame.n = item.get("n", 0)
    frame.where = item.get("where", "")
    frame.detail = item.get("detail", "")
    return frame


# ---------------------------------------------------------------------------
# The child's side
# ---------------------------------------------------------------------------

class _FrameSender(threading.Thread):
    """Sends a copy of the reader's frame stack while it changes."""

    def __init__(self, stack: list[Any], send: Callable[[Any], None]) -> None:
        super().__init__(name="read-process-frames", daemon=True)
        self._stack = stack
        self._send = send
        self._active = threading.Event()
        self._last: Any = None
        self._sequence = 0

    def begin(self, sequence: int) -> None:
        """A file is being read: send its frames, tagged with `sequence` so the
        parent can drop a copy that belongs to the previous file."""
        self._last = None
        self._sequence = sequence
        self._active.set()

    def end(self) -> None:
        """The file is done; nothing more is sent until the next `begin`."""
        self._active.clear()

    def run(self) -> None:
        """Every `_FRAMES_EVERY_S` while a file is open, send the frame stack if
        it changed. Ends only when a send fails, which means the parent went."""
        while True:
            self._active.wait()
            time.sleep(_FRAMES_EVERY_S)
            if not self._active.is_set():
                continue
            sequence = self._sequence
            snapshot = [frame.as_dict() for frame in list(self._stack)]
            if snapshot != self._last:
                self._last = snapshot
                try:
                    self._send(("frames", sequence, snapshot))
                except Exception:                      # noqa: BLE001 - parent gone
                    return


def _serve(inbound: Any, outbound: Any, *, relay_ocr: bool = False) -> None:
    """The child's loop: say "ready", then for each `("read", ...)` request
    stream `("doc", ...)` frames and one `("end", error, failure)`.

    Returns when the parent sends `("quit",)` or closes the pipe. A reader's
    `AppErrorException` travels as `error`; any other exception as `failure`
    text - neither ends the child, so the next file is read by the same
    process. Only a crash of the interpreter itself ends it, which the parent
    records as `ERR_READER_PROCESS_ENDED`.
    """
    from app.extract import chunk_document, extract
    from app.extract import ocr as ocr_module
    from app.extract import progress as reader_progress
    from app.extract import reading as reading_module

    # One writer at a time on the pipe: the frame sender thread and this loop
    # both send, and two interleaved frames would corrupt the stream.
    lock = threading.Lock()

    def send(message: Any) -> None:
        with lock:
            _write(outbound, message)

    def relay(source: Any) -> Any:
        """Order `pictures-process-isolation` (2026-10-09): a scanned page this
        child rendered, read by the parent's helper. The answer is the next
        frame on the inbound pipe - nothing else is read while a file is
        being read, so it is the parent's `("ocr-result", ...)`."""
        payload = str(source) if isinstance(source, Path) else source
        send(("ocr", payload))
        reply = _read(inbound)
        if reply is None or reply[0] != "ocr-result":
            raise RuntimeError("the parent went while a page's text was being read")
        return reply[1]

    if relay_ocr:
        # Only the real child (`main`): a test that drives `_serve` in-process
        # must not leave this installed for the next in-process `ocr_image`.
        ocr_module.set_engine_process(relay)

    stack: list[Any] = []
    reader_progress.attach(stack)
    sender = _FrameSender(stack, send)
    sender.start()
    # **Ready means ready to read (2026-09-30).** The readers register
    # themselves the first time the registry is asked a question
    # (`app/extract/__init__.py`), which in a child was the first file sent to
    # it: measured on this laptop, 0.4-0.8 s of the first read, 1.2-1.7 s with
    # every core busy, against 3-5 ms for the reads after it. That is start-up,
    # and the file watchdog was timing it as the first file's reading. Asked
    # here instead, before "ready" - the same imports, only earlier, so
    # nothing is loaded that the first file would not have loaded anyway.
    try:
        from app.extract import supported_extensions

        supported_extensions()
    except Exception:                                  # noqa: BLE001 - the first read reports it
        pass
    send(("ready", os.getpid()))

    while True:
        request = _read(inbound)
        if request is None or request[0] == "quit":
            return
        # Order `reader-process-isolation` (2026-10-09): a sixth field, the
        # mode. "chunks" is what the pipeline wants for a whole file; "raw" is
        # what the archive reader wants for a member - the document itself,
        # text and segments, which it re-wraps and the pipeline chunks later.
        # A five-field request from an older parent still means "chunks".
        _, sequence, path, resume_from, resume_extra = request[:5]
        mode = request[5] if len(request) > 5 else "chunks"
        # Order `pictures-process-isolation`: which pass this is; a six-field
        # request from an older parent means "read everything", as before.
        images = request[6] if len(request) > 6 else reading_module.IMAGES_READ
        error = failure = None
        sender.begin(sequence)
        try:
            with reading_module.reading(images=images):
                for document in extract(Path(path), resume_from=resume_from,
                                        resume_extra=resume_extra):
                    if mode == "raw":
                        # The whole `Document`, pickled: both ends are this
                        # application, and nothing but the child writes the pipe.
                        send(("rawdoc", document))
                        continue
                    chunks = [
                        {"ordinal": ordinal, "text": chunk.text, "page": chunk.page,
                         "char_start": chunk.char_start, "char_end": chunk.char_end,
                         "label": chunk.label}
                        for ordinal, chunk in enumerate(chunk_document(document))
                    ]
                    send(("doc", document.key, document.source_kind, document.meta,
                          list(document.warnings), chunks))
        except AppErrorException as exc:
            error = exc.error
        except Exception as exc:                       # noqa: BLE001 - reported to the parent
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            sender.end()
            stack.clear()
        send(("end", error, failure))


def main(argv: Optional[list[str]] = None) -> int:
    """The child: read the files the parent sends until it says stop."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # The protocol gets the real standard output; everything else that writes
    # to "standard output" from here on lands on standard error instead.
    outbound = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except (OSError, AttributeError, ValueError):
        # `pythonw.exe` has no standard error to point at; a null device does.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    sys.stdout = sys.stderr if sys.stderr is not None else open(os.devnull, "w")
    if "--low-priority" in argv:
        try:
            import psutil

            from app.core.osbridge.priority import lower_process_priority
            lower_process_priority(psutil)
        except Exception:                              # noqa: BLE001 - politeness, not correctness
            pass
    try:
        # Order `reader-process-isolation` (2026-10-09): the readers may ask
        # which process they are in. The PDF reader does, and never OCRs here.
        # 2026-10-09: a native fault in a reader child leaves the Python stack
        # of its thread in `logs/crash/reader-crash.log` (`app.core.
        # crash_guard`), as the window and the index process do. `log_dir_for`
        # reads `.env` without validating it and never raises.
        from app.core.config import log_dir_for
        from app.core.crash_guard import catch_native_crashes
        from app.extract.base import set_reader_process

        catch_native_crashes(log_dir_for(), "reader")
        set_reader_process(True)
        _serve(sys.stdin.buffer, outbound, relay_ocr=True)
    except (BrokenPipeError, OSError):
        return 0                                       # the parent went first
    return 0


if __name__ == "__main__":
    sys.exit(main())
