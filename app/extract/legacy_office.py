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
import threading
from collections import Counter
from pathlib import Path
from typing import Iterator

from app.core.errors import AppErrorException, raise_error
from app.core.logging import logger

__all__ = ["LegacyOfficeUnreadable", "clean_text", "fall_back", "take_fallback_summary"]

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


def _reason_class(reason: str) -> str:
    """The reason with its numbers removed, so 'slide 4 ...' and 'slide 9 ...' count together."""
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
