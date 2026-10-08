"""The words for an index run's log, and the time on every line.

Layer: L5. Part of the presenter package; imports no Qt.

Work order 0w §2b-§2d. The pipeline records what happened as keys and data
(`app/index/activity.py`); this module is the only place that turns an entry
into a sentence, so the page's log, the notices label and `app.cli index` say
the same thing at the same time. **Every line starts with `HH:MM:SS`**, local
time, the same clock the Settings debug pane shows - two logs on one screen
with two different clocks would be a puzzle nobody asked for.

Its own module, beside `indexing.py` rather than inside it: nothing here
depends on the progress-bar rules there, only on its phase wording.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from app.ui.presenter.formatting import format_size
from app.ui.presenter.indexing import PHASE_WORDS

__all__ = [
    "LOG_LINES_SHOWN",
    "READING_WORDS",
    "PICTURE_REASON_WORDS",
    "STATUS_ORDER",
    "STATUS_SEPARATOR",
    "status_counts_text",
    "activity_line",
    "activity_lines",
    "activity_text",
    "clock_time",
    "console_safe",
    "timed_notices",
]

#: How many lines the page's log holds before the oldest go. Fewer than the
#: run keeps (`activity.ACTIVITY_LIMIT`): the page is for "what is it doing
#: now", and a run's whole story is what the log file is for.
LOG_LINES_SHOWN = 200

#: The phase `PHASE_WORDS` leaves out on purpose - reading has a count, so the
#: detail line draws that instead. The log still needs a line for its start.
READING_WORDS = "Reading your files…"

#: Between the time and the words. Two spaces, so the words start in one
#: column and the eye can run down the times.
_GAP = "  "

#: A large file's opening words, by `activity.large_file_kind`.
_LARGE_WORDS = {
    "mail": "Reading a large mail archive",
    "archive": "Reading a large archive",
    "recording": "Reading a large video or recording",
    "file": "Reading a large file",
}

#: Punctuation the words here and the notices use, and its ASCII look-alike.
_ASCII = {
    "…": "...", "–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
    " ": " ",
}


def clock_time(at: Any) -> str:
    """`HH:MM:SS` in local time for a `time.time()` value, or "" if unusable."""
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(at)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def activity_text(entry: Any) -> str:
    """The sentence for one entry, without its time. Never raises.

    An entry of a kind this does not know is shown by its text alone, rather
    than dropped: a newer pipeline talking to an older page should still be
    heard.
    """
    kind = str(getattr(entry, "kind", "") or "")
    text = str(getattr(entry, "text", "") or "")
    detail = str(getattr(entry, "detail", "") or "")

    if kind == "phase":
        if text == "reading":
            return READING_WORDS
        return PHASE_WORDS.get(text, text)
    if kind == "large_file":
        opening = _LARGE_WORDS.get(detail, _LARGE_WORDS["file"])
        size = int(getattr(entry, "size", 0) or 0)
        return f"{opening}: {text} ({format_size(size)})" if size else f"{opening}: {text}"
    if kind == "pause":
        if detail == "manual":
            return "Paused at your request."
        return f"Paused - {text}" if text else "Paused to stay out of the way."
    if kind == "resume":
        return "Carrying on."
    if kind == "stopping":
        return "Stopping after the current file. Everything indexed so far is kept."
    if kind == "archive":
        # 0w 3b/3c. Interruption, never damage: nothing here is "partly read",
        # which is the damaged-archive row's wording and means something else.
        if detail == "resumed":
            return f"Carrying on with {text} from where an earlier run stopped."
        return f"Stopped part-way through {text}. The next run carries on with it."
    if kind == "archive_counts":
        # Order 0z lane C: "Archive2019.pst: 12,400 Indexed · 3 Failed".
        counts = status_counts_text(_decode_counts(detail))
        return f"{text}: {counts}" if counts else text
    if kind == "finished":
        if text == "stopped":
            return "Stopped. Everything indexed so far is kept."
        return "Finished."
    return text


#: The order the per-item status words are shown in. The words are the keys
#: (`app.extract.progress.STATUS_WORDS`), shown as they are: the owner asked
#: for single words. A word this list does not know is shown after these.
STATUS_ORDER = ("Indexed", "Skipped", "Failed", "TimedOut", "Duplicate", "Held")

#: Between two counts: "12,400 Indexed · 3 Failed".
STATUS_SEPARATOR = " · "


#: Order 0z lane D. Why a picture in mail was left unread - the codes of
#: `app.extract.junk_images.REASONS`, which ride in the counts as
#: `"Skipped:decorative"` - in words, for the brackets after "Skipped".
PICTURE_REASON_WORDS = {
    "decorative": "decorative pictures",
    "repeated": "repeated pictures",
    "few_words": "pictures with under three words",
}


def status_counts_text(counts: Any) -> str:
    """`{"Indexed": 12400, "Failed": 3}` as "12,400 Indexed · 3 Failed". Never raises.

    Zero counts are left out; nothing at all is "". A key `Word:reason`
    (order 0z lane D) is not a word of its own: it is shown in brackets after
    its word - "30 Skipped (24 decorative pictures)".
    """
    try:
        values = {str(word): int(n) for word, n in dict(counts or {}).items()}
    except (TypeError, ValueError):
        return ""
    reasons: dict[str, list[tuple[str, int]]] = {}
    for key in [k for k in values if ":" in k]:
        word, _, reason = key.partition(":")
        reasons.setdefault(word, []).append((reason, values.pop(key)))
    order = [w for w in STATUS_ORDER if w in values] + sorted(
        w for w in values if w not in STATUS_ORDER)
    parts = []
    for word in order:
        if values[word] <= 0:
            continue
        why = ", ".join(f"{n:,} {PICTURE_REASON_WORDS.get(reason, reason)}"
                        for reason, n in reasons.get(word, ()) if n > 0)
        parts.append(f"{values[word]:,} {word}" + (f" ({why})" if why else ""))
    return STATUS_SEPARATOR.join(parts)


def _decode_counts(text: str) -> dict[str, int]:
    # The same format `app.index.activity.encode_counts` writes; decoded here
    # rather than imported, so the presenter stays free of the index layer.
    """`"Indexed=12;Failed=3"` back to a dict; a malformed part is skipped."""
    out: dict[str, int] = {}
    for part in str(text or "").split(";"):
        word, _, number = part.partition("=")
        try:
            if word.strip():
                out[word.strip()] = int(number)
        except ValueError:
            continue
    return out


def activity_line(entry: Any) -> str:
    """`HH:MM:SS  words` for one entry."""
    stamp = clock_time(getattr(entry, "at", None))
    words = activity_text(entry)
    return f"{stamp}{_GAP}{words}" if stamp else words


def activity_lines(entries: Optional[Iterable[Any]]) -> list[str]:
    """One line per entry, oldest first, as they were given."""
    return [activity_line(entry) for entry in (entries or ())]


def timed_notices(stats: Any) -> list[str]:
    """The run's notices, each with the time it was said in front of it.

    Work order 0w §2c. **The notice is not reworded** - the time is put in
    front of it, and a notice with no time (a published record from another
    process, a stats object from before times were kept) is shown exactly as
    it always was.
    """
    notices = [str(line) for line in (getattr(stats, "notices", None) or ())]
    times = list(getattr(stats, "notice_times", None) or ())
    shown: list[str] = []
    for index, notice in enumerate(notices):
        stamp = clock_time(times[index]) if index < len(times) else ""
        shown.append(f"{stamp}{_GAP}{notice}" if stamp else notice)
    return shown


def console_safe(line: str, encoding: Optional[str] = None) -> str:
    """A line the Windows console cannot choke on. Non-negotiable #8's half.

    The phase words carry an ellipsis and some notices a dash; on a console
    left at a legacy code page either one raises `UnicodeEncodeError` in the
    middle of a run. The common punctuation always becomes its ASCII
    look-alike, as the phase words already did. Anything else the console's
    `encoding` cannot show - ASCII when none is given - becomes `?`, which
    loses a letter rather than the run's output; a name with an accent in it
    survives on any console that can show it.
    """
    text = str(line)
    for fancy, plain in _ASCII.items():
        text = text.replace(fancy, plain)
    target = encoding or "ascii"
    try:
        return text.encode(target, "replace").decode(target)
    except LookupError:
        return text.encode("ascii", "replace").decode("ascii")
