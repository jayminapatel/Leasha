"""Structured application errors.

Layer: L0

The contract, from LOCAL_KNOWLEDGE_GRAPH_V2.md: every error surfaced anywhere -
installer, doctor, indexing pipeline, search, UI - carries a payload saying what
happened, what the user should do, and what kind of action that is. Never a bare
traceback. Never a silent 'except: pass'.

Two shapes:

    AppError            a value object; safe to store, serialise, show in the UI
    AppErrorException   the raisable wrapper, carrying an AppError as .error

Use `guard()` at a worker boundary so an unexpected exception still arrives as an
AppError rather than escaping as a traceback.
"""

from __future__ import annotations

import traceback
from contextlib import contextmanager
from enum import Enum
from typing import Any, Iterator, Optional

from pydantic import BaseModel, Field

__all__ = [
    "ActionType",
    "AppError",
    "AppErrorException",
    "ERROR_REGISTRY",
    "make_error",
    "raise_error",
    "guard",
    "to_app_error",
]


class ActionType(str, Enum):
    """What the user (or the app) is expected to do about an error."""

    AUTO_FIX = "AUTO_FIX"            # the app fixes it itself and informs the user
    USER_RETRY = "USER_RETRY"        # the user does something, then retries
    RUN_COMMAND = "RUN_COMMAND"      # an exact command is shown, ready to copy
    SKIP_CONTINUE = "SKIP_CONTINUE"  # this item is skipped, the batch carries on
    #: Nothing to do, by anyone. For conditions that are reported for the log's
    #: sake but are not failures - a search abandoned because the window closed
    #: is the case this exists for. Without it, every such condition had to
    #: borrow a code that tells the user to retry something they did not ask for.
    NONE = "NONE"


class AppError(BaseModel):
    """One error, fully described.

    `message` says what happened in plain English; `suggestion` says what to do
    about it. `details` holds the technical detail the UI keeps collapsed.
    """

    code: str
    component: str
    message: str
    details: Optional[str] = None
    suggestion: str = ""
    action_type: ActionType = ActionType.USER_RETRY
    action_payload: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def render(self) -> str:
        """Multi-line form for a console or a log."""
        lines = [f"[{self.code}] {self.message}"]
        if self.suggestion:
            lines.append(f"  FIX: {self.suggestion}")
        if self.action_payload:
            lines.append(f"  RUN: {self.action_payload}")
        if self.details:
            lines.append(f"  DETAIL: {self.details}")
        return "\n".join(lines)

    @property
    def is_fatal(self) -> bool:
        """SKIP_CONTINUE errors never halt a batch. Everything else may."""
        return self.action_type is not ActionType.SKIP_CONTINUE


class AppErrorException(Exception):
    """Raisable carrier for an AppError."""

    def __init__(self, error: AppError):
        self.error = error
        super().__init__(str(error))


class _Spec(BaseModel):
    """The registry entry behind a code."""

    message: str
    suggestion: str
    action_type: ActionType
    action_payload: Optional[str] = None


# ---------------------------------------------------------------------------
# The registry. Codes marked (spec) come from the recovery table in
# LOCAL_KNOWLEDGE_GRAPH_V2.md and are the required minimum set.
#
# Message and suggestion are format templates: `make_error` fills them from
# keyword arguments, and leaves any placeholder it cannot fill intact rather
# than raising. An error path must never fail because of a missing field.
# ---------------------------------------------------------------------------

ERROR_REGISTRY: dict[str, _Spec] = {
    # --- spec: required recovery mappings ---------------------------------
    "ERR_MODEL_LOAD": _Spec(
        message="The embedding model failed to load.",
        suggestion=(
            "The first run needs internet once to download it (about 130MB); "
            "after that the app is fully offline. Check connectivity or your proxy, then retry."
        ),
        action_type=ActionType.USER_RETRY,
    ),
    # **Not a failure - a race that is expected and harmless.** A search
    # running when the window closes finds the engine gone. It used to surface
    # as ERR_UNEXPECTED with "This is a bug... send the log file", which is
    # alarming, wrong, and buries the real errors it is printed among.
    "ERR_SHUTTING_DOWN": _Spec(
        message="The window is closing, so this search was abandoned.",
        suggestion=(
            "No action needed - nothing is wrong and nothing was lost. Start "
            "the application again whenever you want; indexing resumes where "
            "it stopped."
        ),
        action_type=ActionType.NONE,
    ),
    "ERR_OLLAMA_DOWN": _Spec(
        message="Ollama is not running, so AI answers are unavailable. Search still works normally.",
        suggestion="Start Ollama to enable AI answers and LLM entity extraction.",
        action_type=ActionType.AUTO_FIX,
        action_payload="ollama serve",
    ),
    # A separate code from ERR_OLLAMA_DOWN because it has a different cause and
    # a different fix, and conflating them sent a real diagnosis in the wrong
    # direction: Ollama answered a trial question in 0.59s and the application
    # still reported "Ollama is not answering". It was answering. It was being
    # asked a much longer question with a five-second budget.
    "ERR_OLLAMA_TIMEOUT": _Spec(
        message="Ollama did not finish within {timeout_s}s. Search still works normally.",
        suggestion=(
            "The model is running but slower than the time allowed. A first call also "
            "loads the model into memory and is much slower than the ones after it. "
            "Try again, or set OLLAMA_MODEL to something smaller - a 1.5B model rewrites "
            "a query as well as a 7B one and answers in a fraction of the time."
        ),
        action_type=ActionType.USER_RETRY,
    ),
    # --- file types, converters and OCR -----------------------------------
    "ERR_CLOUD_STUB": _Spec(
        message="'{path}' is a link to a Google Docs file, not the document itself.",
        suggestion=(
            "There is no text in it to index - Drive for Desktop stores only a pointer for "
            "anything created in Google Workspace. To make it searchable, open it in Drive "
            "and choose File > Download > Microsoft Word (.docx) or OpenDocument, or set "
            "Drive for Desktop to sync Workspace files as Office formats."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_CONVERTER_MISSING": _Spec(
        message="'{binary}' is needed to read {ext} files and was not found.",
        suggestion=(
            "Install it and it will be picked up automatically - nothing else needs changing. "
            "Until then these files are indexed by name only."
        ),
        action_type=ActionType.RUN_COMMAND,
        action_payload="winget install --id TheDocumentFoundation.LibreOffice -e",
    ),
    "ERR_CONVERTER_FAILED": _Spec(
        message="Converting '{path}' did not work.",
        suggestion=(
            "This file is skipped and indexing continues. If it happens to every file of "
            "this type, the converter is probably misconfigured; if it happens to one, that "
            "file is likely damaged."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_CONVERTER_BLOCKED": _Spec(
        message="'{binary}' is not an allowed converter, so it was not run.",
        suggestion=(
            "Converters may only run programs on a fixed list held in the application's own "
            "code - a configuration file that could name any executable would be a way to "
            "run anything. Edit the converter to use one of: {allowed}."
        ),
        action_type=ActionType.USER_RETRY,
    ),
    "ERR_OCR_UNAVAILABLE": _Spec(
        message="Reading text from images is switched on, but the OCR engine is not available.",
        suggestion=(
            "Images are indexed by name only until it is installed. Everything else is "
            "unaffected."
        ),
        action_type=ActionType.RUN_COMMAND,
        action_payload=r"venv\Scripts\python.exe -m pip install rapidocr-onnxruntime",
    ),
    # **A queue, not a failure**, and the wording has to carry that or 40,000
    # of these read as 40,000 broken files.
    #
    # At a terabyte, OCR at 3.6 seconds a page dominates everything else: a
    # single pass that reads text and images together means nothing is
    # searchable until everything is. Two passes make search useful in a day
    # or two rather than a fortnight, and this is what the first pass leaves
    # behind for the second.
    "ERR_OCR_HELD": _Spec(
        message="Held for the images pass: '{path}' is a picture of text.",
        suggestion=(
            "Nothing is wrong with it and nothing has been lost - this run was "
            "asked to index text only, so images are queued rather than read. "
            "Run the images pass to fill them in; they are picked up exactly "
            "where they are."
        ),
        # **SKIP_CONTINUE, and the status it produces is the point.**
        # `mark_skipped` writes FAILED for anything `is_fatal`, and a queue of
        # 40,000 files recorded as FAILED is a corpus that looks broken - to a
        # person reading the panel, and to every query that counts failures.
        # The command is still carried, for the button that offers to run it.
        action_type=ActionType.SKIP_CONTINUE,
        action_payload=r"venv\Scripts\python.exe -m app.cli index --only-ocr",
    ),
    "ERR_OCR_FAILED": _Spec(
        message="Could not read any text from '{path}'.",
        suggestion=(
            (
            "No action needed - the image is skipped and indexing continues. "
            "Photographs, logos and diagrams with no writing in them are the "
            "usual reason. If it does contain text, open it and check it is "
            "not blank or rotated, then index that folder again."
        )
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_OCR_LOW_CONFIDENCE": _Spec(
        message="The text read from '{path}' may be unreliable.",
        suggestion=(
            (
            "No action needed - it is indexed and findable, and marked as "
            "read by OCR. Expect mistakes in numbers and names, so open the "
            "document to check anything you are relying on."
        )
        ),
        action_type=ActionType.AUTO_FIX,
    ),
    "ERR_FILE_CORRUPT": _Spec(
        message="Cannot read '{path}' - it is encrypted or damaged.",
        suggestion="Repair it (scanpst.exe for PST files) or leave it skipped. Indexing continues either way.",
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_MOSTLY_PICTURES": _Spec(
        message="Most of '{path}' is pictures rather than text.",
        suggestion=(
            (
            "No action needed - the title and headings are searchable. To "
            "read the pictures too, set 'Pages of a scanned PDF' in Settings "
            "and run `app.cli index --only-ocr`; at roughly 3.6 seconds a "
            "page that is worth doing for a handful of documents, not a "
            "corpus."
        )
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),

    # --- inside archives -----------------------------------------------------
    #
    # Every one of these is `SKIP_CONTINUE`. From
    # `docs/WORKORDER-zip-archives.md` §4: "one bad archive must not end a
    # five-day index", and none of these is a fault in the archive - a
    # password-protected member is somebody's deliberate choice, and a 40GB
    # expansion is a decision about cost.
    "ERR_ARCHIVE_TOO_LARGE": _Spec(
        message="'{member}' in '{path}' was not read: {reason}.",
        suggestion=(
            "The archive itself is indexed by name and its other members are "
            "read as usual. Raise the limit in Settings if this is a file you "
            "need searchable."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_ARCHIVE_TOO_DEEP": _Spec(
        message="'{member}' in '{path}' is nested too deeply to read.",
        suggestion=(
            "Archives are read {depth} level(s) down. Anything further is "
            "recorded by name only - extract it if you need its contents "
            "searchable."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_ARCHIVE_ENCRYPTED": _Spec(
        message="'{member}' in '{path}' is password-protected.",
        suggestion=(
            "It is recorded by name; its contents cannot be read without the "
            "password. Extract it by hand if you need it searchable - this "
            "never prompts and never guesses."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    # **Not `ERR_NO_TEXT_LAYER`, and the distinction is the same one that split
    # `ERR_MOSTLY_PICTURES` out.** An archive that cannot be opened at all and
    # an archive that opened and held nothing worth reading look identical in a
    # report and mean opposite things: one is a damaged file somebody should be
    # told about, the other is an empty container and entirely fine. Sharing a
    # code also puts a corrupt zip in the queue that `--only-ocr` reads back.
    "ERR_ARCHIVE_UNREADABLE": _Spec(
        message="'{path}' could not be opened as an archive: {reason}.",
        suggestion=(
            (
            "It is still indexed by name, folder and type, so it remains "
            "findable - only its contents could not be read. Truncated "
            "downloads and interrupted copies are the usual cause: copy the "
            "file again, then run `app.cli index --force` on its folder to "
            "read inside it."
        )
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_FILE_TRUNCATED": _Spec(
        message="Only part of '{path}' was indexed.",
        suggestion=(
            "The file was read successfully - it is simply large enough that one "
            "part of it was capped so it cannot dominate the index. Split it, or "
            "export the part you search for, if the rest matters."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_FILE_LOCKED": _Spec(
        message="'{path}' is locked by another program.",
        suggestion="Close the program holding it. The file is retried automatically on the next incremental pass.",
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_DISK_SPACE": _Spec(
        message="Indexing paused: only {free_gb}GB free on {drive}.",
        suggestion=(
            "Indexing stopped rather than filling the disk. Free some space, or move the index "
            "location in Settings, then resume. Progress is preserved."
        ),
        action_type=ActionType.USER_RETRY,
    ),
    "ERR_DB_LOCKED": _Spec(
        message="Another copy of the application is already running.",
        suggestion="Close the other copy, then try again. Two copies cannot share one index safely.",
        action_type=ActionType.USER_RETRY,
    ),
    # **Not `ERR_DB_LOCKED`, and the difference is the whole point of the
    # split.** "Another copy of the application is running" tells somebody to
    # close their window, which for this is both wrong and annoying: the window
    # may stay open, and what they have to wait for is a *run* rather than a
    # process. See `core/run_lock.py`.
    "ERR_INDEX_RUNNING": _Spec(
        message="An index run is already in progress ({holder}).",
        suggestion=(
            "Only one process may write to the index at a time. Wait for it to "
            "finish, or stop it - the Indexing page has a Stop button, and it "
            "works on a run started from the command line too. Searching is "
            "unaffected and needs no wait."
        ),
        action_type=ActionType.USER_RETRY,
    ),
    "ERR_OUTLOOK_MISSING": _Spec(
        message="PST indexing needs classic Outlook, which was not found.",
        suggestion=(
            "It must be CLASSIC Outlook. The 'new Outlook' for Windows is a web app: it has no "
            "COM or MAPI automation and cannot open .pst files at all, so having it installed "
            "does not help. If both are installed, use the toggle in Outlook's top-right to "
            "switch back to classic, then re-run. Every other file type indexes normally either "
            "way. If classic Outlook is not available, convert the archives to EML with "
            "XstReader and index that folder instead."
        ),
        action_type=ActionType.RUN_COMMAND,
        action_payload="https://github.com/iluvadev/XstReader",
    ),
    "ERR_ENCODING": _Spec(
        message="Could not decode '{path}' cleanly.",
        suggestion=(
            "No action needed - it is indexed and searchable, and the "
            "original file is untouched. If the preview shows the wrong "
            "characters, re-save the file as UTF-8 and index that folder "
            "again."
        ),
        action_type=ActionType.AUTO_FIX,
    ),
    "ERR_CLOUD_ONLY": _Spec(
        message="'{path}' is stored online only and has no local copy.",
        suggestion=(
            "Skipped so indexing does not force a download. OneDrive and SharePoint 'Files "
            "On-Demand' keep placeholders on disk; reading one downloads the whole file. "
            "To include them, right-click the folder and choose 'Always keep on this device', "
            "or enable 'Index cloud-only files' in Settings and make sure you have the disk "
            "space and bandwidth for it."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_OUTLOOK_BUSY": _Spec(
        message="Outlook is busy or was closed while reading '{folder}'.",
        suggestion=(
            "Email indexing talks to a running Outlook. Leave Outlook open during the first "
            "index run; the remaining folders are retried on the next pass."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    # --- Layer 2 additions --------------------------------------------------
    "ERR_FILE_TOO_LARGE": _Spec(
        message="'{path}' is larger than the limit for its type.",
        suggestion=(
            "Size limits are per format in config\\extractors.toml, and exist because the "
            "cost of reading one is per byte. Raise the limit for this type if the file is "
            "genuinely worth indexing, or leave it: it is counted and can be found again."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_NO_TEXT_LAYER": _Spec(
        message="'{path}' contains no text that can be extracted.",
        suggestion=(
            "Scanned documents and photographs hold text as pixels, not characters, so there "
            "is nothing to index without OCR - which V2 does not do. The file is left in place "
            "and counted, so it can be found again if OCR is added later."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    "ERR_UNSUPPORTED_TYPE": _Spec(
        message="'{ext}' files are not supported, so '{path}' was skipped.",
        suggestion=(
            "Supported: PDF, DOCX, XLSX, PPTX, EML, MSG, PST and plain text. The pre-2007 "
            "binary Office formats (.doc, .xls, .ppt) are not - open one in Office and save it "
            "as the modern equivalent to have it indexed."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
    # --- Layer 0 additions --------------------------------------------------
    "ERR_CONFIG_INVALID": _Spec(
        message="Configuration problem with '{key}': {reason}",
        suggestion="Fix the value in .env, or re-run install.ps1 to regenerate it.",
        action_type=ActionType.USER_RETRY,
    ),
    "ERR_CONFIG_MISSING": _Spec(
        message="No .env file was found at {path}.",
        suggestion="Run install.ps1 from the project folder; it writes .env.",
        action_type=ActionType.RUN_COMMAND,
        action_payload="run-install.cmd",
    ),
    "ERR_NOT_IMPLEMENTED": _Spec(
        message="'{feature}' is not built yet - it arrives in {layer}.",
        suggestion=(
            "Nothing to do: this part is not built yet. Check "
            "BUILD_SPEC_V2.md for what each layer delivers, and use the "
            "command line for anything it lists as done."
        ),
        action_type=ActionType.USER_RETRY,
    ),
    "ERR_UNEXPECTED": _Spec(
        message="An unexpected error occurred in {component}.",
        suggestion=(
            "This is a bug - please report it with the detail below and "
            "today's file from the logs folder. Settings has a Copy button "
            "under Recent activity. Restarting is usually enough to carry on "
            "in the meantime."
        ),
        action_type=ActionType.USER_RETRY,
    ),
    # Appended, never inserted - the file's own rule, so two threads adding a
    # code on the same day merge instead of conflicting.
    "ERR_FILE_MISSING": _Spec(
        message="'{path}' is no longer where it was indexed.",
        suggestion=(
            "It has been moved, renamed or deleted since the last index run. The result is "
            "still worth showing - knowing a file existed and where it was is often the point "
            "- and re-indexing that folder will clear it."
        ),
        action_type=ActionType.SKIP_CONTINUE,
    ),
}


def _safe_format(template: str, values: dict[str, Any]) -> str:
    """Format `template`, leaving unknown placeholders intact.

    An error path must never itself raise. A missing context key produces a
    slightly worse message, never a KeyError on top of the original problem.
    """
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def make_error(
    code: str,
    component: str,
    *,
    details: Optional[str] = None,
    suggestion: Optional[str] = None,
    action_payload: Optional[str] = None,
    **context: Any,
) -> AppError:
    """Build an AppError from the registry, filling templates from `context`.

    An unregistered code degrades to ERR_UNEXPECTED's shape rather than raising,
    so a typo in a rarely-hit error path cannot crash the app.
    """
    spec = ERROR_REGISTRY.get(code)
    if spec is None:
        spec = ERROR_REGISTRY["ERR_UNEXPECTED"]
        details = details or f"Unregistered error code: {code}"

    # `component` is a parameter, not a context key, but templates may still
    # reference {component}. Merge it into the formatting values only - passing
    # it through **context would collide with the positional argument.
    values = dict(context)
    values.setdefault("component", component)

    return AppError(
        code=code,
        component=component,
        message=_safe_format(spec.message, values),
        details=details,
        suggestion=_safe_format(suggestion if suggestion is not None else spec.suggestion, values),
        action_type=spec.action_type,
        action_payload=action_payload if action_payload is not None else spec.action_payload,
        context=context,
    )


def raise_error(code: str, component: str, **kwargs: Any) -> None:
    """Build an AppError and raise it."""
    raise AppErrorException(make_error(code, component, **kwargs))


def to_app_error(exc: BaseException, component: str, code: str = "ERR_UNEXPECTED", **context: Any) -> AppError:
    """Convert any exception into an AppError, preserving the traceback as detail."""
    if isinstance(exc, AppErrorException):
        return exc.error
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
    context.pop("component", None)  # never shadow the positional parameter
    return make_error(code, component, details=detail, **context)


@contextmanager
def guard(component: str, code: str = "ERR_UNEXPECTED", **context: Any) -> Iterator[None]:
    """Worker-boundary guard: converts any escaping exception into an AppError.

    Place this at the boundary of a worker, not around every call. An AppError
    raised inside passes through unchanged, so a precise error is never
    flattened into a generic one.

        with guard("indexer.pdf", path=str(path)):
            parse(path)
    """
    try:
        yield
    except AppErrorException:
        raise
    except Exception as exc:  # noqa: BLE001 - converting at the boundary is the point
        raise AppErrorException(to_app_error(exc, component, code, **context)) from exc
