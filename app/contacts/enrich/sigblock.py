"""Order 1s 3b/3c: where a message's signature block is.

Layer: L7

Pure: lines in, a `Block` (the lines of the signature) out.

**3b, what repeats (D2).** A signature is the part of a person's messages that is
the same every time. Over their messages (each cleaned by 3a), the longest run of
tail lines that two messages share is the signature - no sign-off needed, which is
what the order's "FYI" shape and every unknown sign-off had defeated. The run is
then widened upwards over a line that changed (a new title between two messages)
when a shared line or the name line sits above it, so a promotion does not cut the
name off the block.

**3c, anchors**, for one message or when nothing repeats: in order, the last `-- `
line, the last sign-off from a wider list (any case, several languages), the last
line holding the person's name within the last 20 lines, or the first line holding
their own address or a phone number. The block runs from there to the end of the
cleaned text.

**14 non-blank lines** is the most a block holds: a signature longer than that is
almost always a signature followed by a notice or a quoted message, and what is
past the fourteenth line is not the person's details. Counted without blank lines,
because Outlook's HTML-to-text puts one between every line (the order's shape 3).
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from app.contacts.enrich.sigtail import line_key

__all__ = ["Block", "BLOCK_LINES", "TAIL_LINES", "tail_lines", "anchored_block", "find_blocks", "is_sign_off",
           "holds_name", "SIGN_OFF", "signature_line"]

#: The most non-blank lines a signature block holds (see the module docstring; order 1s 8b).
BLOCK_LINES = 14
#: Only this many of a message's last non-blank lines are looked at: a signature is at the end.
TAIL_LINES = 25
#: A shared run shorter than this is a coincidence ("Thanks"), not a signature.
MIN_RUN = 2

_WORDS = (r"(?:(?:best|kind|warm|warmest|many|with|my)\s+)?(?:regards|wishes|thanks|thank you|cheers|sincerely|"
          r"yours(?:\s+(?:sincerely|faithfully|truly))?|best|rgds|regds|br|cordially|respectfully|all the best|"
          r"take care|many thanks|with thanks|thanks and regards|thanks & regards|best wishes|cordialement|"
          r"mit freundlichen gr(?:\u00fc|ue|u)(?:\u00df|ss)en|saludos(?:\s+cordiales)?|groeten|"
          r"(?:met\s+)?vriendelijke\s+groet(?:en)?|shukran)")
SIGN_OFF = re.compile(rf"^\s*{_WORDS}\s*[,.!]?\s*$", re.I)
#: "Best, Ann" - a sign-off with the name on the same line; the rest becomes the block's first line.
_SIGN_OFF_NAMED = re.compile(rf"^\s*{_WORDS}\s*[,.!\-]\s+(?P<rest>\S.{{0,40}})$", re.I)
_DELIMITER = re.compile(r"^--\s?$")
_PHONEISH = re.compile(r"\+?\d[\d\s().\-]{6,}\d")
_PHONE_ONLY = re.compile(r"^\s*\+?[\d(][\d\s().\-]{6,}\d\s*$")


class Block(list):
    """The lines of one signature block, and how it was found: `repeat` (3b) or `anchor` (3c)."""

    def __init__(self, lines: Sequence[str] = (), how: str = "") -> None:
        super().__init__(lines)
        self.how = how


def tail_lines(text: str) -> list[str]:
    """The last `TAIL_LINES` non-blank lines of `text`, stripped."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-TAIL_LINES:]


def is_sign_off(line: str) -> bool:
    return bool(SIGN_OFF.match(line) or _DELIMITER.match(line))


def holds_name(line: str, tokens: Sequence[str]) -> bool:
    """A short line holding a token of the person's name, and not an address or a bare number."""
    if not tokens or "@" in line or _PHONE_ONLY.match(line) or len(line.split()) > 8:
        return False
    low = line.lower()
    return any(re.search(rf"\b{re.escape(t)}\b", low) for t in tokens)


def _short(line: str) -> bool:
    return len(line.split()) <= 6 and not line.rstrip().endswith((".", "?", "!", ":"))


def _cap(lines: Sequence[str]) -> list[str]:
    return list(lines[:BLOCK_LINES])


def anchored_block(lines: Sequence[str], *, name_tokens: Sequence[str], emails: Sequence[str]) -> Block:
    """3c. Empty when the message has no anchor at all."""
    lines = list(lines)
    n = len(lines)
    for i in range(n - 1, -1, -1):                         # (a) the last "-- " line
        if _DELIMITER.match(lines[i]) and lines[i + 1:]:
            return Block(_cap(lines[i + 1:]), "anchor")
    for i in range(n - 1, -1, -1):                         # (b) the last sign-off
        if SIGN_OFF.match(lines[i]) and lines[i + 1:]:
            return Block(_cap(lines[i + 1:]), "anchor")
        named = _SIGN_OFF_NAMED.match(lines[i])
        if named and holds_name(named.group("rest"), name_tokens):
            return Block(_cap([named.group("rest").strip(), *lines[i + 1:]]), "anchor")
    recent = max(0, n - 20)
    for i in range(n - 1, recent - 1, -1):                 # (c) the last line holding their name
        if holds_name(lines[i], name_tokens):
            return Block(_cap(lines[i:]), "anchor")
    own = [e.lower() for e in emails if e]
    for i in range(recent, n):                             # (d) their own address or a phone number
        low = lines[i].lower()
        if any(e in low for e in own) or _PHONEISH.search(lines[i]):
            start = i
            while start > recent and i - start < 3 and _short(lines[start - 1]) \
                    and not is_sign_off(lines[start - 1]):
                start -= 1
            return Block(_cap(lines[start:]), "anchor")
    return Block((), "")


def signature_line(line: str) -> bool:
    """A line that can be part of a signature: not prose (a notice, a paragraph), which repeats too."""
    return len(line.split()) <= 12 and len(line) <= 90


def _longest_run(a: list[str], b: list[str], ok_a: list[bool]) -> tuple[int, int]:
    """(length, start in `a`) of the longest run of lines `a` and `b` share; ties go to the later start."""
    best = (0, -1)
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if ok_a[i - 1] and a[i - 1] and a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] >= best[0]:
                    best = (cur[j], i - cur[j])
        prev = cur
    return best


def _repeat_block(i: int, tails: list[list[str]], keys: list[list[str]], tokens: Sequence[str]) -> Optional[Block]:
    lines, mine = tails[i], keys[i]
    ok = [signature_line(ln) for ln in lines]
    best = (0, -1)
    for j, other in enumerate(keys):
        if j != i:
            run = _longest_run(mine, other, ok)
            if run[0] > best[0] or (run[0] == best[0] and run[1] > best[1]):
                best = run
    length, start = best
    if length < MIN_RUN:
        return None
    end = start + length
    # A sign-off inside the run: the block is what follows it ("Thanks," and the name both repeat).
    for k in range(end - 1, start - 1, -1):
        if is_sign_off(lines[k]):
            start = k + 1
            break
    else:
        shared = {key for j, other in enumerate(keys) if j != i for key in other}
        k, gap = start - 1, 0
        while k >= 0 and start - k <= 4 and not is_sign_off(lines[k]):
            if mine[k] in shared or holds_name(lines[k], tokens):
                start, gap = k, 0
            elif _short(lines[k]) and gap < 1:
                gap += 1                                   # a line that changed, kept only if a shared one is above
            else:
                break
            k -= 1
    block = _cap(lines[start:])
    return Block(block, "repeat") if block else None


def find_blocks(tails: Sequence[Sequence[str]], *, name_tokens: Sequence[str], emails: Sequence[str]) -> list[Block]:
    """The signature block of each message (3b, else 3c). `tails` are `tail_lines` of cleaned text, one per
    message of the same person; the result is in the same order, an empty `Block` where none was found."""
    tails = [list(t) for t in tails]
    keys = [[line_key(ln) for ln in t] for t in tails]
    out: list[Block] = []
    for i, lines in enumerate(tails):
        block = _repeat_block(i, tails, keys, name_tokens) if len(tails) > 1 else None
        out.append(block if block is not None else anchored_block(lines, name_tokens=name_tokens, emails=emails))
    return out
