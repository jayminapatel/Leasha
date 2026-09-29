r"""Where a reader is inside one file: the "inner progress" hook.

Layer: L2

Work order 0x section 3b. **One mail archive is one file with a hundred
thousand things in it.** The pipeline can say which *file* each reader has
open, but until this existed it could not say how far into that file the
reader had got - so a 4GB `.pst` looked exactly the same at message 12 as at
message 90,000, and a run that was working hard looked like a run that had
stopped.

## How a reader uses it

A reader that walks through many things inside one file opens a *frame* for
that file and moves a counter along as it goes::

    with progress.enter("mbox", path.name, unit="message", total=len(keys)) as frame:
        for position, key in enumerate(keys, start=1):
            frame.n = position          # one attribute store per message
            ...

That is the whole contract. The reader never imports the pipeline, never
formats a sentence and never takes a lock. Readers that do not care about
progress (a `.docx`, a photo) do nothing at all and pay nothing.

## Why a frame stack, and why per thread

**Nesting is real.** A `.zip` can hold an `.mbox`; a `.pst` message can carry
a `.zip` as an attachment. `archive.py` reads a member by handing it to the
ordinary registry *on the same thread*, so the mbox reader opens its frame on
top of the zip's frame, and the stack reads outside-in:
`backup.zip › mail.mbox › message 812`.

**Each extraction thread has its own stack**, kept in a `threading.local`,
because several files are read at once (0x section 3c) and one reader's
position must never overwrite another's. The pipeline hands each of its
worker threads a list to use as that thread's stack (`attach`), keeps a
reference to the same list, and reads it from its own thread when it builds a
progress snapshot.

## Why reading it from another thread is safe without a lock

The writer only ever does three things: append a frame, remove a frame, and
store a number or a string on a frame's attribute. In CPython each of those is
a single operation that cannot be seen half-done. The reader copies the list
with `list(stack)` (one call) and then reads attributes. The worst it can see
is a counter one message behind, or a frame that has just closed - which is
fine for something that is only ever *displayed*, and is refreshed a second
later. A lock taken per message on an archive of 100,000 messages would cost
more than the rest of this module put together.

**Nothing is formatted here.** Frames hold raw numbers and names; the words
live in the presenter (`app/ui/presenter/live_progress.py`), and they are made
only when somebody actually looks.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

__all__ = [
    "Frame",
    "STAGE_OPENING",
    "STAGE_FOLDER",
    "STAGE_MESSAGES",
    "STAGE_ATTACHMENTS",
    "STAGE_ZIP",
    "STAGE_OCR",
    "READER_STAGES",
    "STATUS_WORDS",
    "STATUS_INDEXED",
    "STATUS_SKIPPED",
    "STATUS_FAILED",
    "STATUS_TIMED_OUT",
    "STATUS_DUPLICATE",
    "STATUS_HELD",
    "attach",
    "detach",
    "enter",
    "frames",
    "stage",
    "trail",
]

#: The stages a *reader* can be in, as short codes. Work order 0x section 3a.
#:
#: **Codes, not words.** The sentences the Indexing page shows for each live in
#: the presenter (`STAGE_WORDS`), so wording can change without touching a
#: reader, and a reader can never produce a sentence nobody has checked.
#: The stages that belong to the pipeline rather than to a reader - finding
#: files, chunking, embedding, writing, saving the resume point - are
#: `STAGE_*` constants in `app/index/pipeline.py`, which also holds the one
#: ordered list of every stage (`pipeline.STAGES`).
STAGE_OPENING = "opening"          # opening an archive (a .pst, a .zip)
STAGE_FOLDER = "folder"            # reading a folder's list inside a .pst
STAGE_MESSAGES = "messages"        # reading messages one by one
STAGE_ATTACHMENTS = "attachments"  # reading what is attached to a message
STAGE_ZIP = "zip"                  # reading a member inside a .zip
STAGE_OCR = "ocr"                  # reading text out of a picture

#: Every reader stage, in the order a person would meet them.
READER_STAGES = (
    STAGE_OPENING, STAGE_FOLDER, STAGE_MESSAGES, STAGE_ATTACHMENTS,
    STAGE_ZIP, STAGE_OCR,
)

#: What happened to each item inside a container, as one word each. Order 0z
#: lane C: the owner asked to see, *inside* a mail archive's progress,
#: "12,400 Indexed · 3 Failed · 1 TimedOut". The word is also the key in
#: `Frame.counts`, so it crosses to the window as plain JSON with no mapping
#: to keep in step. Items are messages *and* attachments.
#:
#: * Indexed - read, and handed on to be indexed.
#: * Skipped - nothing to read: an empty message, an attachment of a type
#:   nothing reads, one with no text, or one over the size ceiling.
#: * Failed - could not be read; the reason is in the log.
#: * TimedOut - reserved for a per-item time limit. **Nothing sets it yet**: a
#:   libpff call cannot be interrupted from Python, so a limit has to live
#:   outside the reader (the per-file limit).
#: * Duplicate - an attachment whose bytes were already read in this archive.
#: * Held - a picture left for the pictures pass (`app.extract.reading`).
STATUS_INDEXED = "Indexed"
STATUS_SKIPPED = "Skipped"
STATUS_FAILED = "Failed"
STATUS_TIMED_OUT = "TimedOut"
STATUS_DUPLICATE = "Duplicate"
STATUS_HELD = "Held"

#: Every status word, in the order they are shown.
STATUS_WORDS = (
    STATUS_INDEXED, STATUS_SKIPPED, STATUS_FAILED, STATUS_TIMED_OUT,
    STATUS_DUPLICATE, STATUS_HELD,
)


class Frame:
    """Where one reader is inside one container file.

    `__slots__` rather than a dataclass: the reader writes `n` once per
    message, and an attribute store on a slotted object is about as cheap as
    Python gets. Every field is a plain `int`, `str` or `None`, so a frame
    turns into JSON without any help - the indexer is moving into its own
    process (0x section 2) and streams its progress as JSON lines.

    Fields:

    * `kind` - which reader: "pst", "mbox", "zip", "olm".
    * `name` - the container's own file name, for example `Archive2019.pst`.
    * `unit` - what is being counted: "message" or "member".
    * `n` - the position of the thing being read now, counting from 1.
      0 means "opened, nothing read yet".
    * `total` - how many there are, or `None` when that is not known cheaply.
      **Never guessed and never pre-scanned for.** A second pass over a 4GB
      archive to learn a denominator would cost more than the progress is
      worth, so a reader that does not already know says `None` and the page
      shows "message 812" without "of ...".
    * `where` - a place inside the container: a `.pst` folder path, or a zip
      member's name. "" when there is none.
    * `stage` - one of the `STAGE_*` codes.
    * `detail` - one extra name worth showing, for example the attachment
      being read. "" when there is none.
    * `counts` - how many items ended as each `STATUS_WORDS` word so far,
      for the whole container. Empty for a reader that does not count.
    * `beat` - rises by one for **every** item looked at: message or
      attachment, read, skipped, failed or held. `n` is a position within a
      folder, goes back to 1 when the next folder starts, and stands still
      while a message's attachments are read; `beat` only ever rises, so "has
      this reader moved?" is one comparison. 0 for a reader that does not
      keep it.

    `counts` and `beat` appear in `as_dict` only once a reader has set them,
    so the frame of a reader that does not count is exactly what it was.
    """

    __slots__ = ("kind", "name", "unit", "n", "total", "where", "stage", "detail",
                 "counts", "beat")

    def __init__(self, kind: str, name: str, *, unit: str = "",
                 total: Optional[int] = None, stage: str = "") -> None:
        self.kind = kind
        self.name = name
        self.unit = unit
        self.n = 0
        self.total = total
        self.where = ""
        self.stage = stage
        self.detail = ""
        self.counts: dict[str, int] = {}
        self.beat = 0

    def count(self, word: str) -> None:
        """One more item ended as `word`, and the reader moved on."""
        self.counts[word] = self.counts.get(word, 0) + 1
        self.beat += 1

    def as_dict(self) -> dict[str, Any]:
        """The frame as plain values, ready for JSON. Called only when read.

        `dict(self.counts)` is one C-level copy, so a reader adding to it on
        another thread cannot tear it (see the module docstring).
        """
        out: dict[str, Any] = {
            "kind": self.kind, "name": self.name, "unit": self.unit,
            "n": self.n, "total": self.total, "where": self.where,
            "stage": self.stage, "detail": self.detail,
        }
        if self.counts:
            out["counts"] = dict(self.counts)
        if self.beat:
            out["beat"] = self.beat
        return out


#: Each thread's own stack of frames. See the module docstring.
_local = threading.local()


def frames() -> list[Frame]:
    """This thread's frame stack, created empty the first time it is asked for.

    A reader running outside the pipeline - a test, `app.cli`, a one-off
    script - still gets a working stack; nobody is reading it, and that is
    fine.
    """
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = []
        _local.stack = stack
    return stack


def attach(stack: list[Frame]) -> None:
    """Make this thread's readers use `stack`, which the caller also keeps.

    Called once by each pipeline worker thread when it starts. Keeping a
    reference to the same list is what lets the pipeline read the frames from
    a *different* thread - a `threading.local` on its own is invisible to
    every thread but its owner.
    """
    _local.stack = stack


def detach() -> None:
    """Forget this thread's stack. Called when a pipeline worker ends."""
    _local.stack = None


@contextmanager
def enter(kind: str, name: str, *, unit: str = "",
          total: Optional[int] = None, stage: str = STAGE_OPENING) -> Iterator[Frame]:
    """Open a frame for one container file, and close it however the read ends.

    **The stack is captured when the frame opens, not looked up again when it
    closes.** A reader is usually a generator, and a generator that is
    abandoned part-way is closed whenever Python gets round to collecting it -
    possibly on another thread. Looking the stack up by thread at that moment
    would remove a frame from the wrong reader's stack. Removal is by identity
    for the same reason: if something above this frame was left behind, the
    right frame still goes.
    """
    stack = frames()
    frame = Frame(kind, name, unit=unit, total=total, stage=stage)
    stack.append(frame)
    try:
        yield frame
    finally:
        _remove(stack, frame)


def _remove(stack: list[Frame], frame: Frame) -> None:
    """Take `frame` off `stack`, if it is still there. Never raises."""
    if stack and stack[-1] is frame:
        try:
            stack.pop()
        except IndexError:                     # cleared by the pipeline meanwhile
            pass
        return
    for index in range(len(stack) - 1, -1, -1):
        try:
            if stack[index] is frame:
                del stack[index]
                return
        except IndexError:
            return


@contextmanager
def stage(code: str) -> Iterator[None]:
    """Mark the innermost open frame as being in `code` for a while.

    For work that happens inside a frame but is not the frame's main loop -
    OCR of one scanned page inside an archive member, say. Outside any frame
    it does nothing, so a caller never has to ask first. The frame's previous
    stage comes back afterwards.
    """
    stack = frames()
    if not stack:
        yield
        return
    frame = stack[-1]
    before = frame.stage
    frame.stage = code
    try:
        yield
    finally:
        frame.stage = before


def trail(stack: Optional[list[Frame]] = None) -> list[dict[str, Any]]:
    """The frames on `stack` (default: this thread's), outermost first, as plain dicts.

    `list(stack)` is one call, so the copy cannot be torn by a reader pushing
    or popping at the same moment on another thread.
    """
    source = frames() if stack is None else stack
    return [frame.as_dict() for frame in list(source)]
