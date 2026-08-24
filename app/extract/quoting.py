r"""Strip quoted reply chains and signatures, so a thread is indexed once.

Layer: L2

A twelve-message thread currently puts the first message into the index twelve
times: once as itself, and eleven more times quoted inside the replies. The cost
is paid three times over.

* **The index inflates** roughly with the square of the thread depth.
* **Embedding time is spent on text already embedded** - and at the throughput
  this project actually measures, that is the dominant cost of a mail corpus.
* **Results fill with near-duplicates.** Ten hits from one conversation, each
  showing the same paragraph, crowd out the ten different documents that should
  have been on the page. This is the one a person notices, and they experience
  it as "search is bad" rather than as "the index is redundant".

**The first occurrence is kept.** Not the newest, not the longest - the first,
in file order. A reply quoting a message that is not otherwise in the corpus
(sent from a phone, deleted from the archive, from before indexing began) is the
only copy of that text there is, and dropping it would lose it entirely. That is
why this is deduplication *within a message*, and never across the corpus.

**Nothing is deleted from disk, ever.** This shapes what goes into the index. The
original file is untouched, so a bad rule costs a re-index and nothing more.

The markers are matched conservatively. A false positive truncates a real
message and silently loses content, which is far worse than a missed quote
leaving some duplication behind - so every pattern here is anchored to the start
of a line and specific enough that ordinary prose cannot trigger it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["StripResult", "strip_quoted", "QUOTE_MARKERS", "SIGNATURE_MARKERS"]


#: Where a quoted chain begins. Everything from here to the end of the message
#: is a copy of something else, so the message is cut at the first one found.
#:
#: Anchored with `^` under `re.M`: `-----Original Message-----` inside a
#: sentence is prose, at the start of a line it is Outlook.
QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    # Outlook, every localisation that keeps the English marker.
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.M | re.I),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.M | re.I),
    # Outlook's HTML divider before a quoted reply: a line of underscores.
    re.compile(r"^_{20,}\s*$", re.M),
    # "On Tuesday, 3 June 2025 at 14:22, Dave Smith <dave@acme.com> wrote:"
    # The trailing "wrote:" is what makes this safe - it is not a sentence
    # anybody writes by accident, and it may wrap onto the following line.
    re.compile(r"^\s*On\s.{0,200}?\bwrote:\s*$", re.M | re.I | re.S),
    # Gmail and Apple Mail, same idea, different wording.
    re.compile(r"^\s*Le\s.{0,200}?\ba écrit\s*:\s*$", re.M | re.I | re.S),
    # The header block Outlook writes above a forward.
    re.compile(r"^\s*From:\s.+\n\s*Sent:\s.+\n\s*To:\s.+", re.M | re.I),
    re.compile(r"^\s*From:\s.+\n\s*Date:\s.+\n\s*To:\s.+", re.M | re.I),
)

#: Where a signature begins. Cut, because a signature repeats on every message
#: somebody has ever sent and carries no information about *this* one.
#:
#: `-- ` on its own line is the RFC 3676 delimiter, and the trailing space is
#: part of the standard; some clients strip it, so both are accepted.
SIGNATURE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^--\s?$", re.M),
    re.compile(r"^\s*Sent from my \w[\w ]{0,30}$", re.M | re.I),
    re.compile(r"^\s*Get Outlook for (iOS|Android)\s*$", re.M | re.I),
    re.compile(r"^\s*This e?-?mail (and any attachments )?(is|are) confidential",
               re.M | re.I),
    re.compile(r"^\s*The information (contained )?in this e?-?mail", re.M | re.I),
)

#: Lines beginning with `>` are quoted text in every mail client there is.
_QUOTED_LINE = re.compile(r"^\s*>.*$", re.M)

#: A cut leaving less than this is treated as a false positive, and the message
#: is kept whole.
#:
#: **Deliberately tiny, and it started at 40, which was a real bug.** The guard
#: is meant to catch a marker matching at position zero - a message that *opens*
#: with a signature delimiter, where cutting would leave nothing at all. But
#: replies in a thread are short by their nature: "Approved." is nine
#: characters, "Yes fine, buy it." is seventeen, and those are precisely the
#: messages this module exists to separate from the chain they are quoting. A
#: floor of 40 silently kept every one of them whole, doing nothing while
#: reporting success.
#:
#: Specificity of the markers is what prevents false positives. This is only the
#: last resort for a cut that leaves an empty message.
MIN_KEPT_CHARS = 2


@dataclass(frozen=True, slots=True)
class StripResult:
    """What was kept, and how much was removed - so the effect is measurable."""

    text: str
    original_chars: int
    #: Named for what it is: the reason the cut was made, or "" if nothing was.
    reason: str = ""

    @property
    def removed_chars(self) -> int:
        return max(0, self.original_chars - len(self.text))

    @property
    def changed(self) -> bool:
        return bool(self.reason)


def _earliest_match(text: str, patterns: tuple[re.Pattern[str], ...]) -> tuple[int, str]:
    """The position of the first marker in `text`, and which kind it was."""
    best = len(text)
    which = ""
    for pattern in patterns:
        match = pattern.search(text)
        if match is not None and match.start() < best:
            best = match.start()
            which = pattern.pattern[:40]
    return best, which


def strip_quoted(text: str, *, strip_signatures: bool = True) -> StripResult:
    """Cut a message at its first quoted chain and signature.

    Returns the original text unchanged when nothing matches, or when cutting
    would leave almost nothing - a marker at the very top of a message is much
    more likely to be a false positive than a real one-line reply, and this
    errs towards keeping too much.

    >>> strip_quoted("Yes, agreed.\n\n-----Original Message-----\nblah").text
    'Yes, agreed.'
    """
    original = len(text)
    if not text.strip():
        return StripResult(text, original)

    cut_at, reason = _earliest_match(text, QUOTE_MARKERS)

    if strip_signatures:
        sig_at, sig_reason = _earliest_match(text, SIGNATURE_MARKERS)
        if sig_at < cut_at:
            cut_at, reason = sig_at, f"signature: {sig_reason}"
        elif reason:
            reason = f"quoted: {reason}"
    elif reason:
        reason = f"quoted: {reason}"

    kept = text[:cut_at].rstrip()

    # Whatever survives, drop any `>`-prefixed lines still in it. A reply
    # interleaved with quotes has no single cut point, so the quoted lines are
    # removed individually.
    without_quotes = _QUOTED_LINE.sub("", kept)
    if without_quotes.strip() and len(without_quotes.strip()) >= MIN_KEPT_CHARS:
        if without_quotes != kept:
            reason = reason or "quoted: inline > lines"
        kept = without_quotes

    kept = re.sub(r"\n{3,}", "\n\n", kept).strip()

    if not reason:
        return StripResult(text, original)

    if len(kept) < MIN_KEPT_CHARS:
        # Cutting here would leave nothing worth indexing, which means the
        # marker almost certainly matched something that was not a quote.
        # Keeping the whole message costs some duplication; the alternative
        # loses a real message with no way to notice.
        return StripResult(text, original)

    return StripResult(kept, original, reason)
