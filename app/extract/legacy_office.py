r"""What `doc.py` and `ppt.py` share: one refusal, one clean-up, one fall-back.

Layer: L2

Both readers parse an OLE2 binary by hand, and both have the same honest limit:
some files are encrypted, some are older than the structure they understand, and
some are damaged. The rule for all of them is the one non-negotiable 12 implies
for a library reader that cannot finish - **never return empty text, never
guess.** Say so with `LegacyOfficeUnreadable`, and `fall_back` then does the
only correct thing: give the file to the LibreOffice route that used to read
every `.doc` and `.ppt`, if it is switched on, and raise the ordinary
`ERR_FILE_CORRUPT` if it is not.
"""

from __future__ import annotations

import re
import struct
import threading
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator

from app.core.errors import AppErrorException, raise_error
from app.core.logging import logger

__all__ = ["LegacyOfficeUnreadable", "clean_text", "fall_back", "note_unread_embedded",
           "records", "shape_property_text", "take_fallback_summary",
           "take_unread_embedded_summary"]

log = logger.bind(component="extract.legacy_office")


#: Files the in-process readers handed to the converter this run, by reason.
#: Extraction runs in threads of one process, so a lock is all this needs.
_fallbacks: Counter[str] = Counter()
_fallbacks_lock = threading.Lock()


def take_fallback_summary() -> str:
    """One line for the end-of-run log, and reset. Empty when nothing fell back.

    Nothing fails silently: a file the fast reader would not vouch for was read
    by LibreOffice instead, which is slower, and the person who tuned the run
    should be able to see how often that happened and why.
    """
    with _fallbacks_lock:
        counts = _fallbacks.copy()
        _fallbacks.clear()
    total = sum(counts.values())
    if not total:
        return ""
    worst = "; ".join(f"{why} ({count:,})" for why, count in counts.most_common(3))
    noun = "file" if total == 1 else "files"
    return f"{total:,} legacy Office {noun} went to the slower reader: {worst}"


#: Files indexed with words left inside an embedded object, by kind of object.
_unread_embedded: Counter[str] = Counter()


def note_unread_embedded(path: Path, kinds: Iterable[str]) -> None:
    """Record that `path` was indexed with an embedded object's words unread.

    **Why this counts rather than declines (lead decision, 2026-09-20).** The
    honest options for text this reader cannot reach are "read it", "hand the
    file to LibreOffice" and "say so". Measured on real files, LibreOffice's
    `.doc` text export does not contain embedded-object text either: two real
    documents with an embedded workbook scored recall 1.000 against LibreOffice
    *without* the workbook being read at all. Handing those files over would
    cost 5-10 seconds each and return the same words, so the file is indexed at
    full speed and its loss is counted here instead - the shape
    `ERR_PST_PARTIAL` already uses for a mail archive read in part. Nothing
    fails silently: the absence is named, counted and reported once per run.
    """
    for kind in sorted(set(kinds)):
        with _fallbacks_lock:
            _unread_embedded[kind] += 1
    log.warning("{} holds text inside an embedded object that was not read: {}",
                path.name, ", ".join(sorted(set(kinds))))


def take_unread_embedded_summary() -> str:
    """One line for the end-of-run log, and reset. Empty when nothing was lost."""
    with _fallbacks_lock:
        counts = _unread_embedded.copy()
        _unread_embedded.clear()
    total = sum(counts.values())
    if not total:
        return ""
    worst = "; ".join(f"{kind} ({count:,})" for kind, count in counts.most_common(4))
    noun = "file" if total == 1 else "files"
    return (f"{total:,} {noun} hold text inside embedded objects that was not read: {worst}")


def _reason_class(reason: str) -> str:
    """The reason with its numbers removed, so 'slide 4 ...' and 'slide 9 ...' count together."""
    # Sixty characters keeps the top-three summary on one log line.
    return re.sub(r"\d+", "N", reason)[:60]


class LegacyOfficeUnreadable(Exception):
    """The in-process reader will not vouch for this file. Not an error to show."""


#: Everything below 0x20 that is not a tab or a newline, plus the private-use
#: and non-character code points Word uses as placeholders.
_CONTROL = re.compile("[\x00-\x08\x0e-\x1f\x7f￾￿]")


def clean_text(raw: str) -> str:
    r"""Legacy-Office text as plain lines.

    `\r` is a paragraph end in both formats, `\x0b` a soft line break and `\x0c`
    a page or section break. The non-breaking hyphen (`\x1e`) is a hyphen and
    the optional hyphen (`\x1f`) is nothing a person would search for.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x0b", "\n").replace("\x0c", "\n\n")
    text = text.replace("\x1e", "-").replace("\x1f", "")
    text = text.replace(" ", "\n").replace(" ", "\n")
    text = _CONTROL.sub("", text)
    return text


#: Escher property tables (`OfficeArtFOPT` and its two variants) carry the text of
#: WordArt - text drawn as geometry, which is in no text stream at all. Both
#: formats store it the same way, because both store Escher the same way.
ESCHER_OPT = (0xF00B, 0xF121, 0xF122)
#: `gtextUNICODE` ([MS-ODRAW] 2.3.22.1): the characters a WordArt shape draws.
PROP_GTEXT_UNICODE = 0x00C0

_RECORD_HEADER = struct.Struct("<HHI")             # ver/instance, type, length


def records(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int, int, int]]:
    """`(version, instance, type, body_start, body_end)` for each record in a span.

    The record header of `.ppt` and of an Escher drawing are the same eight
    bytes, which is why one walker serves both readers. Stops quietly at a
    header that does not fit, and raises when a record claims to run past the
    end of its parent: a length that lies means everything after it is misread,
    which is worse than not reading.
    """
    position = start
    while position + 8 <= end:
        ver_instance, record_type, length = _RECORD_HEADER.unpack_from(data, position)
        body_start = position + 8
        body_end = body_start + length
        if body_end > end:
            raise LegacyOfficeUnreadable(
                f"a record at offset {position} runs past the end of its parent")
        yield ver_instance & 0xF, ver_instance >> 4, record_type, body_start, body_end
        position = body_end


def shape_property_text(data: bytes, start: int, end: int, count: int) -> list[str]:
    """WordArt text: the `gtextUNICODE` property of a shape's property table.

    A table is `count` six-byte entries (id, value); an entry with the complex
    bit set has its value as a byte length into the data that follows the whole
    table, in order. Chemical-plant P&ID style decks, anything titled with
    WordArt, and a Word document's Fontwork banner keep their words here and
    nowhere else.
    """
    found: list[str] = []
    table_end = start + count * 6
    if table_end > end:
        return found
    cursor = table_end
    for index in range(count):
        ident, value = struct.unpack_from("<HI", data, start + index * 6)
        if not ident & 0x8000:
            continue
        size = value
        if cursor + size > end:
            break
        if ident & 0x3FFF == PROP_GTEXT_UNICODE:
            found.append(
                data[cursor:cursor + (size & ~1)].decode("utf-16-le", "replace").rstrip("\x00"))
        cursor += size
    return found


def fall_back(path: Path, component: str, reason: str) -> Iterator[object]:
    """Read `path` through the converter route, or raise `ERR_FILE_CORRUPT`.

    A generator so it can be `yield from`-ed in place of the reader's own
    documents. Imported lazily: `converter.py` is the one module that runs a
    program, and nothing should reach it merely by importing a reader.
    """
    with _fallbacks_lock:
        _fallbacks[_reason_class(reason)] += 1
    rule = None
    try:
        from app.core.formats import load_rules

        rule = load_rules().converter_for(path.suffix.lower())
    except Exception:                                     # noqa: BLE001 - bad config
        rule = None                                       # is `formats`' to report

    if rule is not None and getattr(rule, "enabled", False):
        from app.extract.converter import extract_via_converter

        try:
            yield from extract_via_converter(path, rule)
            return
        except AppErrorException as exc:
            # LibreOffice missing or failing is the reason worth reporting, but
            # it does not say why the library reader stood down. Keep both.
            log.debug("{} fell back for {} ({}) and the converter failed: {}",
                      component, path.name, reason, exc)
            raise

    raise_error(
        "ERR_FILE_CORRUPT", component, path=str(path),
        details=f"{reason}, and no converter is switched on to try instead",
    )
