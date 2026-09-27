"""Outlook for Mac exports: `.olm`, read one message at a time from inside the zip.

Layer: L2

Work order 0x, section 8b ("mail files from a Mac"). Outlook for Mac has no
`.pst`; its File > Export writes an `.olm` file instead. An `.olm` is an
ordinary zip archive, and each email inside it is its own small XML file. So,
like ODF and EPUB before it (non-negotiable #12), it needs no library at all:
`zipfile` opens the container and `xml.etree.ElementTree` reads each message.

## The assumed layout - **(UNCONFIRMED)**

Microsoft does not publish the format. Everything below comes from public
descriptions written by people who unpacked real exports, and has **not** been
checked against a real `.olm` from here. The parser is written so that being
wrong about any one detail costs that detail, not the message: a missing
element is simply empty, an element it does not know is ignored, and tag
names are compared without any XML namespace.

    Accounts/<account>/com.microsoft.__Messages/<folder>/message_00001.xml

A message member is any `.xml` whose path contains a
`com.microsoft.__Messages` folder, or whose file name starts `message_`.
Inside one, the element that holds the message (usually `<email>` under a
root `<emails>`, but whichever element first has `OPFMessageCopy...` children
is used) may contain:

| Element | Used as |
|---|---|
| `OPFMessageCopySubject` | subject |
| `OPFMessageCopyFromAddresses` | sender (first address) |
| `OPFMessageCopySenderAddress` | sender, if there is no From |
| `OPFMessageCopyToAddresses`, `...CCAddresses` | recipients, To then Cc (as `.eml`) |
| `OPFMessageCopySentTime`, else `OPFMessageCopyReceivedTime` | date, ISO 8601 |
| `OPFMessageCopyBody` | body (stripped as HTML if it looks like HTML) |
| `OPFMessageCopyHTMLBody` | body, when there is no plain one |
| `OPFMessageCopyAttachmentList` | attachment names (`OPFAttachmentName`) |
| `OPFMessageCopyReferences`, `...InReplyTo`, `...MessageID` | thread key, as `.eml` |

An address is an `<emailAddress>` element with `OPFContactEmailAddressAddress`
(the address) and `OPFContactEmailAddressName` (the display name) attributes;
plain text inside the element is accepted too. A date with no time zone is
taken to be UTC **(UNCONFIRMED)**. Attachment *contents* live in other members
of the zip and are not read here - only their names are indexed, which is what
`.eml` does for a message's attachments too.

## Streaming, and why it matters

A ten-year mailbox exports to many gigabytes. The zip's table of contents is
read once (it lists every member's name and size without decompressing
anything), then messages are decompressed **one at a time** straight from the
archive into the XML parser, never written to disk and never all held in
memory. Only one message is in memory at once.

## Resume, exactly like mbox

`supports_resume = True`, and `extract(path, resume_from=N)` skips the first
`N` entries of the message list **without opening them** - no decompression,
no XML parse. The list is every message member's name, sorted, which is a
fixed order for an unchanged file (and the pipeline keys the saved position on
the file's content hash, so a changed file never resumes into stale
positions). Each document reports its own position as
`meta["mbox_index"]` - the key name `app/index/pipeline.py` reads
(`RESUME_POSITION_META_KEY`) for every resumable reader, mbox being the first -
and gets a stable `virtual_path` of `{olm}/{member path}`, so a resumed run
writes the same keys an uninterrupted run would have.

## The zip guards, reused rather than copied

Every per-member guard comes from `app/extract/archive.py`, not a second copy
of it: `safe_member_name` refuses a traversal name (`../../evil.xml`), an
absolute path or a drive letter; `_refusal` refuses an encrypted member, one
over the per-member size cap, or one whose declared expansion is a zip bomb -
all decided from the zip's table of contents before a byte is decompressed.
The declared member count is checked with archive.py's `_member_count` before
`zipfile` builds its table, so an archive that claims millions of members
costs nothing to decline.

**Two archive-level limits are deliberately different from a plain `.zip`**,
because an `.olm` is a mailbox, not a folder someone zipped:

* The member-count ceiling is `index/scan.py`'s `ARCHIVE_MEMBER_LIMIT`
  (200,000), not archive.py's 20,000 - a real mailbox export holds a member
  per message *and* per attachment, and 20,000 would refuse an ordinary one.
* archive.py's shared 512MB total budget is not applied. That budget exists
  because each zip member becomes a temp file and a full document; here a
  message is parsed in memory and discarded, the per-member cap bounds the
  memory, and a total budget would silently stop a real mailbox part way.
  The "archive over N MB" Settings switch is for zips too and does not apply.

Every refused or unreadable message is counted, and the total rides as one
`ERR_MAIL_PARTIAL` warning on the last message yielded (`with_closing_warning`,
the shape `.pst` uses), so the run summary says how many were missed and why.
One bad message never stops the rest (non-negotiable #3); a file that is not a
zip at all is a structured `ERR_FILE_CORRUPT` skip.

**Read-only (non-negotiable #10).** The archive is opened for reading only.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Optional
from xml.etree import ElementTree

from app.core.errors import AppError, make_error, raise_error
from app.core.logging import logger
from app.extract.archive import _member_count, _refusal, safe_member_name
from app.extract.base import (
    Document,
    SourceKind,
    normalise_whitespace,
    register,
    with_closing_warning,
)
from app.extract.email_files import build_email_document, html_to_text

__all__ = ["OlmExtractor", "RESUME_POSITION_META_KEY", "message_members"]

_log = logger.bind(component="extract.olm")

#: The `Document.meta` key the pipeline reads a resumable reader's position
#: from. It must equal `app.index.pipeline.RESUME_POSITION_META_KEY`; it is
#: repeated here rather than imported because importing the pipeline would
#: drag the whole indexer into a file parser. A unit test asserts the two
#: match, so they cannot drift. The name says "mbox" because mbox was the first
#: reader to resume; for an `.olm` it is the position in the message list.
RESUME_POSITION_META_KEY = "mbox_index"


def _member_limit() -> int:
    """Members one `.olm` may declare before it is refused outright.

    The scanner's `ARCHIVE_MEMBER_LIMIT`, which is the project's existing
    answer to "how big a table of contents is worth building" - see the module
    docstring for why archive.py's smaller `MAX_MEMBERS` would refuse ordinary
    mailboxes. Imported when first needed, the way archive.py reaches into the
    scanner, so importing this parser never imports the indexer's modules.
    """
    from app.index.scan import ARCHIVE_MEMBER_LIMIT

    return int(ARCHIVE_MEMBER_LIMIT)

#: The folder name that marks where messages live inside an export.
_MESSAGES_FOLDER = "com.microsoft.__messages"

#: The prefix every message element's name starts with. Finding the element
#: that holds children with this prefix is how the message is located without
#: depending on the root element's name. (UNCONFIRMED)
_MESSAGE_PREFIX = "OPFMessageCopy"


# ---------------------------------------------------------------------------
# Choosing the members
# ---------------------------------------------------------------------------

def _looks_like_message(name: str) -> bool:
    """Is this zip member one message's XML? Decided by its name alone.

    Both separators are accepted because a zip written on one system and read
    on another may use either, and a `Path` on a Mac does not split on `\\`.
    """
    normal = str(name or "").replace("\\", "/")
    lowered = normal.lower()
    if not lowered.endswith(".xml"):
        return False
    parts = [piece for piece in lowered.split("/") if piece]
    if not parts:
        return False
    return _MESSAGES_FOLDER in parts[:-1] or parts[-1].startswith("message_")


def message_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Every message member in `archive`, sorted by name: the resume order.

    **Sorted by the raw name, and including members that will later be
    refused.** The position of a message in this list is what gets saved as
    the resume cursor, so the list has to come out the same on every run of
    an unchanged file. Deciding refusals *after* numbering means a refused
    member still holds its slot, and the numbers never shift under it.
    """
    members = [entry for entry in archive.infolist() if _looks_like_message(entry.filename)]
    members.sort(key=lambda entry: entry.filename)
    return members


# ---------------------------------------------------------------------------
# Reading one message's XML
# ---------------------------------------------------------------------------

def _local(tag: Any) -> str:
    """An element's name without any `{namespace}` in front of it."""
    text = str(tag or "")
    return text.rsplit("}", 1)[-1]


def _message_element(root: ElementTree.Element) -> Optional[ElementTree.Element]:
    """The element holding the `OPFMessageCopy...` fields, or None.

    Usually `<email>` inside `<emails>`, but the root itself or any deeper
    element is accepted - whichever first has such children. None means the
    member is not a message (a folder description, say) and is passed over.
    """
    for element in root.iter():
        if any(_local(child.tag).startswith(_MESSAGE_PREFIX) for child in element):
            return element
    return None


def _child(message: ElementTree.Element, name: str) -> Optional[ElementTree.Element]:
    """The direct child called `name` (namespace ignored), or None."""
    for child in message:
        if _local(child.tag) == name:
            return child
    return None


def _text(message: ElementTree.Element, name: str) -> str:
    """All the text inside child `name`, stripped; "" when it is absent."""
    element = _child(message, name)
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _addresses(message: ElementTree.Element, name: str) -> list[str]:
    """The email addresses listed under child `name`.

    Each `<emailAddress>` gives its `OPFContactEmailAddressAddress` attribute;
    one without it falls back to its own text. If the element has no address
    children at all, its own text is used (some exports are said to write a
    bare address). Duplicates are kept out, order is kept.
    """
    element = _child(message, name)
    if element is None:
        return []
    found: list[str] = []
    for child in element.iter():
        if child is element:
            continue
        address = (child.get("OPFContactEmailAddressAddress") or "").strip()
        if not address:
            address = (child.text or "").strip()
        if address and address not in found:
            found.append(address)
    if not found:
        bare = "".join(element.itertext()).strip()
        if bare:
            found.append(bare)
    return found


def _iso_to_unix(text: str) -> Optional[int]:
    """An ISO 8601 date as Unix seconds, or None if it cannot be read.

    A trailing `Z` is accepted (Python 3.11+ reads it). A date with no zone
    is taken as UTC - **(UNCONFIRMED)** what Outlook for Mac means by it.
    """
    value = (text or "").strip()
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return int(moment.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _looks_like_html(text: str) -> bool:
    """Cheap sniff: does this "plain" body actually hold markup?

    Some descriptions of the format say `OPFMessageCopyBody` carries HTML for
    HTML mail. Stripping tags from real plain text would be harmless but for
    the odd `<address>` in a signature, so it is only done when the text
    plainly is a document. (UNCONFIRMED)
    """
    head = text[:2048].lower()
    return head.lstrip().startswith("<") and any(
        marker in head for marker in ("<html", "<body", "<div", "<p>", "<p ", "<br", "<table"))


def _body(message: ElementTree.Element) -> str:
    """The best body available: plain text, else the HTML body as text.

    Uses `email_files.html_to_text`, the same stripper every other mail path
    uses, so an HTML-only message reads the same whichever format it came in.
    """
    plain = _text(message, "OPFMessageCopyBody")
    if plain:
        return html_to_text(plain) if _looks_like_html(plain) else normalise_whitespace(plain)
    markup = _text(message, "OPFMessageCopyHTMLBody")
    return html_to_text(markup) if markup else ""


def _attachment_names(message: ElementTree.Element) -> list[str]:
    """Names of the listed attachments (their contents are not read).

    `OPFAttachmentName` is the expected attribute; any attribute whose name
    ends in `Name` is accepted as a fallback, then the element's text.
    """
    listing = _child(message, "OPFMessageCopyAttachmentList")
    if listing is None:
        return []
    names: list[str] = []
    for item in listing:
        name = (item.get("OPFAttachmentName") or "").strip()
        if not name:
            name = next((str(value).strip() for key, value in item.attrib.items()
                         if _local(key).endswith("Name") and str(value).strip()), "")
        if not name:
            name = "".join(item.itertext()).strip()
        if name:
            names.append(name)
    return names


def _conversation(message: ElementTree.Element) -> Optional[str]:
    """The thread root, chosen the way `email_files._conversation_key` does.

    First entry of References, else In-Reply-To, else the message's own id -
    so replies group with what they reply to, whichever format each arrived
    in. Element names are (UNCONFIRMED).
    """
    references = _text(message, "OPFMessageCopyReferences").split()
    if references:
        return references[0]
    return (_text(message, "OPFMessageCopyInReplyTo")
            or _text(message, "OPFMessageCopyMessageID") or None)


def _read_message(
    archive: zipfile.ZipFile, entry: zipfile.ZipInfo,
) -> Optional[ElementTree.Element]:
    """Decompress and parse one member, streamed into the parser.

    Returns the message element, or None when the member is valid XML but
    not a message. Raises on damage - the caller counts it and carries on.

    `archive.open()` decompresses as the parser reads, so the member is never
    written to disk. Python's bundled expat refuses the "billion laughs"
    entity-expansion attack by itself, and ElementTree never fetches external
    entities, so a hostile XML member cannot reach the network or the disk.
    A module-level function so the resume test can count how often it runs.
    """
    with archive.open(entry) as stream:
        root = ElementTree.parse(stream).getroot()
    return _message_element(root)


def _document(path: Path, member: str, index: int, message: ElementTree.Element) -> Document:
    """One parsed message as the standard mail `Document`.

    Built by `build_email_document`, the one function every mail format goes
    through, so quote-stripping and the header lines are identical to `.eml`.
    `source_kind`, `virtual_path` and the position key are set exactly as
    `email_mbox` sets them, so the pipeline treats an `.olm` like an mbox.
    """
    senders = (_addresses(message, "OPFMessageCopyFromAddresses")
               or _addresses(message, "OPFMessageCopySenderAddress"))
    sent_at = _iso_to_unix(_text(message, "OPFMessageCopySentTime"))
    if sent_at is None:
        sent_at = _iso_to_unix(_text(message, "OPFMessageCopyReceivedTime"))
    document = build_email_document(
        path,
        subject=_text(message, "OPFMessageCopySubject"),
        sender=senders[0] if senders else "",
        recipients=(_addresses(message, "OPFMessageCopyToAddresses")
                    + _addresses(message, "OPFMessageCopyCCAddresses")),
        sent_at=sent_at,
        conversation=_conversation(message),
        body=_body(message),
        attachments=_attachment_names(message),
        source_kind=SourceKind.EML,
    )
    # `{olm}/{member}`: stable for an unchanged file whatever position this
    # run started from - see "Resume" in the module docstring.
    document.virtual_path = f"{path}/{member}"
    document.meta[RESUME_POSITION_META_KEY] = index
    document.meta["olm_member"] = member
    # The mail folder the message was exported from ("Inbox", "Sent Items"),
    # under the same key the libpff reader uses for a `.pst` folder.
    parts = PurePosixPath(member).parts
    lowered = [part.lower() for part in parts]
    if _MESSAGES_FOLDER in lowered:
        folder = parts[lowered.index(_MESSAGES_FOLDER) + 1:-1]
        document.meta["folder_path"] = "/".join(folder)
    return document


# ---------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------

class _Tally:
    """Messages that could not be read, for the one closing warning.

    Kept as counts plus the first few reasons: a damaged export can lose
    thousands of messages, and a warning listing every one would be larger
    than the mail it describes.
    """

    __slots__ = ("refused", "damaged", "examples", "yielded")

    def __init__(self) -> None:
        self.refused = 0
        self.damaged = 0
        self.examples: list[str] = []
        #: Documents handed on. Zero at the end, with misses counted, means
        #: nothing at all was readable - see `closing`.
        self.yielded = 0

    def note(self, kind: str, detail: str) -> None:
        if kind == "refused":
            self.refused += 1
        else:
            self.damaged += 1
        if len(self.examples) < 3:
            self.examples.append(detail)

    def warning(self, path: Path) -> Optional[AppError]:
        """`ERR_MAIL_PARTIAL` summarising the misses, or None if there were none."""
        if not (self.refused or self.damaged):
            return None
        pieces = []
        if self.damaged:
            pieces.append(f"{self.damaged} message{'s' if self.damaged != 1 else ''} "
                          "could not be read")
        if self.refused:
            pieces.append(f"{self.refused} member{'s' if self.refused != 1 else ''} "
                          "refused by the archive safety checks")
        return make_error("ERR_MAIL_PARTIAL", "extract.olm", path=str(path),
                          reason=" and ".join(pieces),
                          details="; ".join(self.examples))

    def closing(self, path: Path) -> Optional[AppError]:
        """What `with_closing_warning` attaches once the loop is done.

        **Nothing readable at all is a skip, not a warning.** A warning needs
        a document to ride on; with none, it would be dropped and the generic
        dispatcher would report `ERR_NO_TEXT_LAYER` - "opened fine, held no
        text" - which is the wrong story for an export whose every message was
        damaged or refused. Raised as `ERR_FILE_CORRUPT` instead, carrying the
        same counts, so the skip ledger says what actually happened.
        """
        warning = self.warning(path)
        if warning is not None and self.yielded == 0:
            raise_error("ERR_FILE_CORRUPT", "extract.olm", path=str(path),
                        details=f"{warning.context.get('reason', '')}: {warning.details or ''}")
        return warning


class OlmExtractor:
    """Every message in an Outlook for Mac `.olm` export, one document each."""

    name = "olm"
    extensions = frozenset({".olm"})
    # Read from the file's own bytes, never through Outlook - so hashing it
    # for change detection is meaningful. See `Extractor.reads_externally`.
    reads_externally = False

    #: See `app/extract/base.py`'s `Extractor` Protocol docstring: lets the
    #: generic `extract()` pass a `resume_from` position into `extract()`.
    supports_resume = True

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path, *, resume_from: int = 0) -> Iterable[Document]:
        """Yield one document per message, starting at position `resume_from`.

        `resume_from` is an index into `message_members()`; every member
        before it is skipped without being decompressed or parsed. `0` reads
        the whole export.
        """
        tally = _Tally()
        yield from with_closing_warning(
            self._messages(path, max(int(resume_from or 0), 0), tally),
            lambda: tally.closing(path),
        )

    def _messages(self, path: Path, resume_from: int, tally: _Tally) -> Iterator[Document]:
        """The message loop. Raises only for a file that is not a readable zip."""
        # The member-count guard runs before `zipfile` builds its table of
        # contents - that table is what costs memory. See the module docstring.
        declared = _member_count(path)
        limit = _member_limit()
        if declared is not None and declared > limit:
            raise_error(
                "ERR_ARCHIVE_TOO_LARGE", "extract.olm", member=path.name, path=str(path),
                reason=f"it declares {declared:,} members, over the "
                       f"{limit:,} that are read from one export")
            return

        try:
            archive = zipfile.ZipFile(path)
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.olm", path=str(path), details=str(exc))
            return
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            # Not a zip, truncated, or gone since the walk: a structured skip,
            # never an exception that ends the run.
            raise_error("ERR_FILE_CORRUPT", "extract.olm", path=str(path),
                        details=str(exc) or exc.__class__.__name__)
            return

        with archive:
            for index, entry in enumerate(message_members(archive)):
                if index < resume_from:
                    # Already finished by an earlier run. Not opened, not
                    # decompressed, not parsed - that is the whole saving.
                    continue

                # Guards first, from the table of contents alone - see
                # archive.py. A refused member keeps its index (see
                # `message_members`) but produces no document.
                member = safe_member_name(entry.filename)
                if member is None:
                    _log.warning("refused a member named {!r} in {}", entry.filename, path.name)
                    tally.note("refused", f"unsafe name {entry.filename!r}")
                    continue
                refused = _refusal(entry, path, int(entry.file_size or 0),
                                   int(entry.compress_size or 0))
                if refused is not None:
                    tally.note("refused", refused.message)
                    continue

                try:
                    message = _read_message(archive, entry)
                except Exception as exc:                # noqa: BLE001 - one bad message
                    _log.debug("could not read {} in {}: {}", member, path.name, exc)
                    tally.note("damaged", f"{member}: {exc}")
                    continue
                if message is None:
                    continue                            # valid XML, not a message

                try:
                    document = _document(path, member, index, message)
                except Exception as exc:                # noqa: BLE001 - one bad message
                    _log.debug("could not build {} in {}: {}", member, path.name, exc)
                    tally.note("damaged", f"{member}: {exc}")
                    continue
                if not document.is_empty:
                    tally.yielded += 1
                    yield document


register(OlmExtractor())
