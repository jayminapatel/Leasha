r"""The mail preview, in words: the header card, the conversation, the original.

Layer: L5. Part of the presenter package; imports no Qt.

Order 0y section 4 ("Mail: a preview people recognise"). Everything here is a
decision about what to say - who a message is from, how its date reads, which
line stands for it in a conversation, what "open the original" means for it -
so it is decided here, where a test can reach it, and drawn by
`app/ui/widgets/mail_card.py`.

**What the index holds decides what the card can say**, and three facts about
it are worth knowing before changing anything:

* `messages.sender` is usually a bare address. The readers keep the address and
  drop the display name, so the card shows the address as the name rather than
  inventing "Dave Smith" out of `dave.smith@...`.
* `messages.recipients` is one list, To first and then Cc. The index does not
  record which is which, so the card shows them as one line.
* Attachment names are not a column. They are in the first lines of the stored
  text, which every mail reader writes through `build_email_document`:
  `Subject:`, `From:`, `To:`, `Attachments:`, a blank line, then the message.
  `split_index_headers` reads that block back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from app.core.row_facts import (  # noqa: F401 - ATTACHMENT_MARKER, attachment_of re-exported
    ATTACHMENT_MARKER, MESSAGE_FILE_EXTS, OUTLOOK_ARCHIVE_EXTS, attachment_of,
)
from app.reports.timeline_words import day_heading

__all__ = [
    "MailCard", "mail_card", "card_from_row", "sent_in_words", "split_index_headers",
    "INDEX_HEADER_LABELS", "NO_SUBJECT", "UNNAMED_ATTACHMENT", "RECIPIENTS_LABEL",
    "mail_terms",
    "ConversationLine", "conversation_lines", "conversation_heading", "first_line",
    "CONVERSATION_SHOWN",
    "OriginalTarget", "original_target", "OPEN_IN_OUTLOOK", "OPEN_IN_OUTLOOK_TIP",
    "THREAD_COLUMN", "fold_conversations", "mail_list", "attachment_of", "ATTACHMENT_MARKER",
]

#: What a message with an empty subject is called - the Mail list's own words
#: (`rows.mail_rows`), so the card and the row beside it agree.
NO_SUBJECT = "(no subject)"

#: The chip for a message the index knows has an attachment but not its name
#: (a message indexed without the `Attachments:` line).
UNNAMED_ATTACHMENT = "Attachment"

#: The label beside the recipients. "To", as the Mail list's column and the
#: plain header block already say; the tooltip says that Cc is in the same list.
RECIPIENTS_LABEL = "To"
RECIPIENTS_TIP = ("Everyone this message was sent to. The index keeps To and Cc "
                  "as one list, so they are shown together.")

#: The lines `build_email_document` writes above a message's text, in its order.
INDEX_HEADER_LABELS: tuple[str, ...] = ("Subject", "From", "To", "Attachments")


@dataclass(frozen=True, slots=True)
class MailCard:
    """What the header card says. Every field may be empty; empty is not drawn."""

    #: The large line: a display name, or the address when that is all there is.
    sender_name: str = ""
    #: Beside the name. Empty when the name line already is the address.
    sender_address: str = ""
    #: To, then Cc - one list, as the index holds them.
    recipients: tuple[str, ...] = ()
    #: "Tuesday 2 January 2024, 09:00", or "" when the message has no date.
    date_words: str = ""
    subject: str = NO_SUBJECT
    #: One chip each.
    attachments: tuple[str, ...] = ()


def sent_in_words(sent_at: Any) -> str:
    """A message's date as a person would say it: *Tuesday 2 January 2024, 09:00*.

    The day is worded by `timeline_words.day_heading` - the one place the
    application already says a day in full - with the time added. Local time,
    like the Mail list's Date column. `""` for a message with no date: a blank
    is honest, and 1 January 1970 is a claim.
    """
    try:
        seconds = int(sent_at)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return ""
    return f"{day_heading(moment)}, {moment:%H:%M}"


def split_index_headers(text: Any) -> tuple[dict[str, str], str]:
    r"""`({label: value}, the message's own text)` from one message's stored text.

    Only the block at the very top is taken, and only lines carrying the labels
    the indexer writes, in the order it writes them - so a message that quotes
    `From: somebody` further down keeps it, and text with no such block comes
    back whole with an empty dictionary.
    """
    body = str(text or "")
    block, separator, rest = body.partition("\n\n")
    found: dict[str, str] = {}
    position = 0
    for line in block.split("\n"):
        label, colon, value = line.partition(": ")
        try:
            index = INDEX_HEADER_LABELS.index(label, position)
        except ValueError:
            return {}, body                  # not the indexer's block after all
        if not colon:
            return {}, body
        found[label] = value.strip()
        position = index + 1
    if not found:
        return {}, body
    return found, (rest if separator else "")


def _split_sender(value: Any) -> tuple[str, str]:
    """`(name, address)`. A bare address is the name, with nothing beside it."""
    text = str(value or "").strip()
    if "<" in text and text.endswith(">"):
        name, _, address = text.partition("<")
        name = name.strip().strip('"').strip()
        address = address[:-1].strip()
        if name and address and name != address:
            return name, address
        return (name or address), ""
    return text, ""


def _recipient_list(value: Any) -> tuple[str, ...]:
    """Every recipient, from the JSON array the store holds. Never raises."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                return (text,)               # not JSON after all; show what is there
        else:
            return (text,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item or "").strip())


def mail_card(message: Optional[Mapping[str, Any]], stored_text: Any = "") -> MailCard:
    """The card for one `messages` row, with attachment names from its stored text."""
    message = message or {}
    name, address = _split_sender(message.get("sender"))
    headers, _body = split_index_headers(stored_text)
    named = tuple(part.strip() for part in headers.get("Attachments", "").split(", ")
                  if part.strip())
    if not named and message.get("has_attach"):
        named = (UNNAMED_ATTACHMENT,)
    return MailCard(
        sender_name=name, sender_address=address,
        recipients=_recipient_list(message.get("recipients")),
        date_words=sent_in_words(message.get("sent_at")),
        subject=str(message.get("subject") or "").strip() or NO_SUBJECT,
        attachments=named,
    )


def card_from_row(row: Any) -> Optional[MailCard]:
    """A first card from the Mail list's own row, before the message is read.

    The row carries the sender, the recipients as the column shows them, the
    date and the subject, which is enough to draw the card the moment the
    selection moves - so arrowing down the list does not flick between a title
    line and a card. The read that follows fills in the rest. `None` for a row
    that is not a Mail row; the pane keeps its title for those.
    """
    if row is None or not getattr(row, "file_id", None):
        return None
    if not hasattr(row, "sender") or not hasattr(row, "subject"):
        return None
    name, address = _split_sender(getattr(row, "sender", ""))
    return MailCard(
        sender_name=name, sender_address=address,
        recipients=_recipient_list(getattr(row, "recipients", "")),
        date_words=sent_in_words(getattr(row, "sent_at", 0)),
        subject=str(getattr(row, "subject", "") or "").strip() or NO_SUBJECT,
        attachments=(UNNAMED_ATTACHMENT,) if getattr(row, "has_attachment", False) else (),
    )


# ---------------------------------------------------------------------------
# 4c - the conversation
# ---------------------------------------------------------------------------

#: How many messages the list under the card holds. A short list by design: it
#: is there to move between the replies of one exchange, and a key shared by
#: hundreds of messages (an archive threaded by subject line) is not that.
CONVERSATION_SHOWN = 25

#: Characters of a message's first line shown in the list.
FIRST_LINE_CHARS = 120


@dataclass(frozen=True, slots=True)
class ConversationLine:
    """One message in the list under the card."""

    #: The Mail list's own row for it (`rows.MailRow`) - what the pane is given
    #: when the line is clicked, so it previews, opens and pins like any other.
    row: Any
    sender: str = ""
    #: As the Mail list's Date column writes it.
    when: str = ""
    first_line: str = ""
    #: The message now on show.
    current: bool = False

    @property
    def text(self) -> str:
        """`Dave Smith · 02 Jan 09:00 — The trip is on Friday.`"""
        head = " · ".join(part for part in (self.sender, self.when) if part)
        if head and self.first_line:
            return f"{head} — {self.first_line}"
        return head or self.first_line


def first_line(stored_text: Any, *, limit: int = FIRST_LINE_CHARS) -> str:
    """The first thing a message says: its first non-empty line, after the
    index's own header lines, cut at a word when it is long."""
    _headers, body = split_index_headers(stored_text)
    for line in body.split("\n"):
        line = " ".join(line.split())
        if not line:
            continue
        if len(line) <= limit:
            return line
        cut = line[:limit]
        space = cut.rfind(" ")
        return (cut[:space] if space > limit // 2 else cut).rstrip() + "…"
    return ""


def conversation_heading(count: int, *, shown: int = CONVERSATION_SHOWN) -> str:
    """`4 messages in this conversation`. `""` for a message on its own - a
    list of one is the message already on show."""
    count = int(count or 0)
    if count < 2:
        return ""
    if count > shown:
        return (f"More than {shown:,} messages in this conversation — "
                f"the newest {shown:,} are listed")
    return f"{count:,} messages in this conversation"


def conversation_lines(rows: Any, current_id: Any, *, shown: int = CONVERSATION_SHOWN,
                       now: Optional[float] = None) -> tuple[ConversationLine, ...]:
    """Store rows (`conversation_messages`, oldest first) to list lines.

    Given one more row than `shown` - how the caller learns there were more -
    the oldest is dropped, so the list is the newest `shown`.
    """
    from app.ui.presenter.rows import mail_rows

    rows = list(rows or ())[-shown:]
    listed = mail_rows(rows, now=now)
    return tuple(
        ConversationLine(
            row=row, sender=row.sender, when=row.sent,
            first_line=first_line(source.get("opening")),
            current=row.file_id == current_id,
        )
        for row, source in zip(listed, rows, strict=True)
    )


# ---------------------------------------------------------------------------
# 4d - the original
# ---------------------------------------------------------------------------

OPEN_IN_OUTLOOK = "Open in Outlook"
OPEN_IN_OUTLOOK_TIP = (
    "Shows this message in Outlook, with everything the preview leaves out. "
    "Outlook opens the archive the message is in and keeps it open.")
OPEN_FILE = "Open"

#: Mail that is one message in one file, which its own program can open.
#: 2026-10-04, code review: `row_facts`'s families - this copy lacked `.mht`.
MESSAGE_FILE_SUFFIXES = tuple(f".{ext}" for ext in MESSAGE_FILE_EXTS)
#: Archives Outlook opens.
OUTLOOK_ARCHIVE_SUFFIXES = tuple(f".{ext}" for ext in OUTLOOK_ARCHIVE_EXTS)


@dataclass(frozen=True, slots=True)
class OriginalTarget:
    """What "open the original" means for one message."""

    #: `"outlook"` - shown by Outlook, by its identifier in an archive;
    #: `"file"` - a message file, opened by whatever program owns it.
    kind: str
    #: The button's words.
    label: str
    entry_id: str = ""
    store_path: str = ""
    path: str = ""


def original_target(message: Optional[Mapping[str, Any]], path: Any) -> Optional[OriginalTarget]:
    """Where the full message can be opened, or `None` when nowhere can.

    A message read out of a `.pst` or `.ost` has an identifier and the archive
    it came from: Outlook can show it. A `.eml`, `.msg` or `.emlx` is a file.
    Anything else - a message inside an mbox or an `.olm` - is neither, and
    gets no button rather than one that fails.
    """
    message = message or {}
    text = str(path or "")
    entry_id = str(message.get("entry_id") or "").strip()
    archive = str(message.get("store_path") or "").strip()
    if entry_id and archive.lower().endswith(OUTLOOK_ARCHIVE_SUFFIXES):
        return OriginalTarget(kind="outlook", label=OPEN_IN_OUTLOOK, entry_id=entry_id,
                              store_path=archive, path=text)
    if text.lower().endswith(MESSAGE_FILE_SUFFIXES) and "://" not in text:
        return OriginalTarget(kind="file", label=OPEN_FILE, path=text)
    return None


# ---------------------------------------------------------------------------
# Order 0z F2 - the Mail list, one row per conversation
# ---------------------------------------------------------------------------

#: How an attachment's path says which message it belongs to:
#: `<the message's path>/attachments/<name>`, written by
#: `archive.attachment_key`. 2026-10-04, code review: one marker and one
#: parser, `row_facts`'s - the Search list's subtitles, the store's
#: `LISTED_FILES` and this list all read them.
#: (`ATTACHMENT_MARKER` and `attachment_of` are imported at the top.)


#: The Mail table's column for a folded row: `(key, heading, attribute on
#: MailRow, right-aligned?)`. Empty - and so not offered - unless folding is on.
THREAD_COLUMN: tuple[str, str, str, bool] = ("messages", "Messages", "thread", True)


def fold_conversations(rows: Any) -> list:
    """The Mail list's rows with each conversation's messages folded into one.

    **The first row of a conversation stands for it**, in the order the list
    came in - so with the list newest first, that is its newest message, and
    the conversation sits where that message sat. The row carries how many of
    the *listed* messages it stands for (`thread`, `thread_count`); the preview
    pane's own list (order 0y 4c) is where every message of it is read, the
    ones beyond this page included.

    A message with no conversation recorded is a conversation of one. Nothing
    is dropped without being counted: the counts add up to the rows given.
    """
    from dataclasses import replace

    order: list[Any] = []
    counts: dict[Any, int] = {}
    first: dict[Any, Any] = {}
    for row in rows or ():
        key = getattr(row, "conversation", "") or ("alone", getattr(row, "file_id", id(row)))
        if key not in counts:
            counts[key] = 0
            first[key] = row
            order.append(key)
        counts[key] += 1
    return [replace(first[key], thread=f"{counts[key]:,}", thread_count=counts[key])
            for key in order]


def mail_list(store_rows: Any, *, fold: bool = False, now: Optional[float] = None) -> tuple[list, str]:
    """`(rows to draw, what to say about folding)` for the Mail table.

    The second value goes in front of the list's summary, which is left to
    count messages exactly as it always has: `Showing 500 of 12,431 messages`
    is still true of a folded list, and `Folded into 212 conversations` says
    what the fold did to those 500. `""` when nothing is folded.
    """
    from app.ui.presenter.rows import mail_rows

    rows = mail_rows(store_rows, now=now)
    if not fold or not rows:
        return rows, ""
    folded = fold_conversations(rows)
    count = len(folded)
    return folded, f"Folded into {count:,} conversation{'s' if count != 1 else ''}  ·  "


def mail_terms(parsed: Any) -> list[str]:
    """The words to highlight in a message previewed from the Mail tab.

    Order 0y section 4b. The Mail list filters on header fields, so its
    "searched words" are the `/subject` value and any plain words typed beside
    the filters - which the list says it ignored, and which are still what the
    person is looking for in the message they open. A sender or a recipient is
    not highlighted: the card already shows who.
    """
    if parsed is None:
        return []
    words = [*(getattr(parsed, "subjects", ()) or ()),
             *(getattr(parsed, "terms", ()) or ()),
             *(getattr(parsed, "phrases", ()) or ())]
    seen: list[str] = []
    for word in words:
        word = str(word or "").strip()
        if word and word not in seen:
            seen.append(word)
    return seen
