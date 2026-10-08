"""The words of the Mail archives box in Settings.

Layer: L5. Part of the presenter package; imports no Qt.

2026-10-07, the owner: "need a way for each pst file it can be configured how
to index outlook or direct ... there should be a reindex button on those
files". One line per `.pst` or `.ost` in the index: how many messages it gave,
where it stands, and how it is read - the setting above it, or a choice of its
own, named for what it is (2026-10-08). Two ways to read one again: over the
top, or cleared first.

**Every status-bar sentence names an amount**, as `folders_removed_message`
does: "Done" with no number leaves somebody wondering whether anything
happened at all.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any, Mapping, Optional

__all__ = [
    "ARCHIVE_CHOICES", "OUTLOOK_ONLY_TIP", "ARCHIVE_CHOICE_TIP", "archive_choices",
    "archive_default_label", "archive_name", "messages_words",
    "archive_status_words", "is_outlook_only", "mail_archives_empty_text",
    "clear_archive_confirmation", "read_again_message", "clearing_archive_message",
    "archive_cleared_message", "archive_choice_saved_message",
]

#: What a line's first choice says, by the setting above it (`ui:pst_backend`).
#:
#: 2026-10-08, the owner: "the text use the setting above makes no sense it
#: should display the actual option". The first choice is no choice of its own
#: - the line follows "How to read archives" - so it names the way that
#: setting reads archives *now*, and changes with it (`MailArchivesBox.
#: set_default_backend`). "(as set above)" is what tells it from the same way
#: chosen for this archive alone, which does not follow the setting.
DEFAULT_LABELS: dict[str, str] = {
    "auto": "Automatic - direct if possible (as set above)",
    "libpff": "Direct file reading (as set above)",
    "outlook": "Through Outlook (as set above)",
}


def archive_default_label(default_backend: Any = "auto") -> str:
    """A line's first choice: the way "How to read archives" reads it now."""
    key = str(default_backend or "auto").strip().lower()
    return DEFAULT_LABELS.get(key, DEFAULT_LABELS["auto"])


def archive_choices(default_backend: Any = "auto") -> tuple[tuple[str, str], ...]:
    """`(label, backend)` for each line's drop-down, in order. "auto" is no
    choice of its own and is named after the setting above; the other two are
    that drop-down's own words, so one setting reads the same in both places."""
    return (
        (archive_default_label(default_backend), "auto"),
        ("Direct file reading (no Outlook needed)", "libpff"),
        ("Through Outlook (MAPI)", "outlook"),
    )


#: The drop-down's choices while the setting above is Automatic, its default.
ARCHIVE_CHOICES: tuple[tuple[str, str], ...] = archive_choices("auto")

#: Said on every line's drop-down but an `.ost`'s.
ARCHIVE_CHOICE_TIP = (
    "How this archive is read.\n\n"
    "The first choice follows \"How to read archives\" above and names the way "
    "it is set now. Choose one of the others when this archive needs a way of "
    "its own - one Outlook cannot open, say. It applies from the next index run.")

#: Said on an `.ost` line's drop-down, which cannot be changed.
OUTLOOK_ONLY_TIP = (
    "This is Outlook's own copy of a mailbox (.ost). Only Outlook can read it, "
    "so it is always read through Outlook.")


def archive_name(path: Any) -> str:
    """The file's own name, from a Windows or a forward-slash path."""
    text = str(path or "")
    return PureWindowsPath(text).name or text


def is_outlook_only(path: Any) -> bool:
    """An `.ost`: Outlook's offline copy, which nothing else can open."""
    return str(path or "").lower().endswith(".ost")


def messages_words(count: Any) -> str:
    """"1 message", "12,345 messages", "No messages"."""
    count = int(count or 0)
    if count <= 0:
        return "No messages"
    return f"{count:,} message{'s' if count != 1 else ''}"


def _reason(skip_code: Any) -> str:
    """The error catalogue's sentence for a reader's code, or the code itself
    when the sentence needs details this line does not have."""
    code = str(skip_code or "").strip()
    if not code:
        return "the reason was not recorded"
    try:
        from app.core.errors import ERROR_REGISTRY
    except Exception:                                  # noqa: BLE001 - words only
        return code
    spec = ERROR_REGISTRY.get(code)
    message = str(getattr(spec, "message", "") or "")
    if not message or "{" in message:
        return code
    return message.rstrip(".")


def archive_status_words(status: Any, skip_code: Any = None, messages: Any = 0) -> str:
    """Where one archive stands, in plain words.

    `INDEXED` (or `PARTIAL`) is "Read". A row still waiting with messages
    already in the index is one whose read stopped part way - Outlook busy, or
    the window closed - and is picked up on the next run.
    """
    word = str(status or "").upper()
    if word in ("INDEXED", "PARTIAL"):
        return "Read"
    if word in ("SKIPPED", "FAILED"):
        return f"Skipped: {_reason(skip_code)}"
    if int(messages or 0) > 0:
        return "Partly read - the next run carries on"
    return "Not read yet"


def mail_archives_empty_text() -> str:
    """The box's one line while the index holds no mail archive."""
    return ("No mail archives in the index yet. Add a folder that holds .pst "
            "files, or one .pst with \"Add file…\", and they appear here once "
            "an index run has found them.")


def clear_archive_confirmation(path: Any, count: int) -> tuple[str, str]:
    """What "Clear and read again" asks first. `(title, body)`."""
    name = archive_name(path)
    items = "item" if count == 1 else "items"
    title = f"Clear {name} and read it again?"
    body = (
        f"This removes {count:,} {items} read from {name} out of Leasha's index - "
        "its messages and their attachments - and then reads the archive again "
        "from the start.\n\n"
        "Until it has been read again, its mail cannot be found. "
        "Your file is not touched.")
    return title, body


def read_again_message(path: Any, messages: Optional[int] = None) -> str:
    """The status line when "Read again" starts a run."""
    name = archive_name(path)
    if messages:
        return (f"Reading {name} again from the start - the {messages_words(messages)} "
                "already in the index stay while it is read.")
    return f"Reading {name} again from the start."


def clearing_archive_message(path: Any, count: int) -> str:
    """The status line while an archive's items are being removed."""
    items = "item" if count == 1 else "items"
    return f"Removing {count:,} {items} of {archive_name(path)} from the index…"


def archive_cleared_message(path: Any, count: Any) -> str:
    """The status line once the archive has been cleared and its read started."""
    count = int(count or 0)
    items = "item" if count == 1 else "items"
    return (f"{count:,} {items} of {archive_name(path)} taken out of the index; "
            "reading it again now. Nothing on disk was touched.")


def archive_choice_saved_message(path: Any, backend: str,
                                 choices: Optional[Mapping[str, str]] = None,
                                 default_backend: Any = "auto") -> str:
    """The status line after a line's drop-down changes. Names how many
    archives now have a choice of their own; a line put back to follow the
    setting above says which way that is."""
    labels = dict((value, label) for label, value in archive_choices(default_backend))
    own = len(choices or {})
    archives = ("archive has a setting of its own" if own == 1
                else "archives have a setting of their own")
    how = labels.get(str(backend or "auto"), labels["auto"])
    return (f"{archive_name(path)}: {how}. {own:,} {archives}; it applies from "
            "the next index run.")
