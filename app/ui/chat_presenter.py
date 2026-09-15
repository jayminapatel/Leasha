r"""Chat tab decisions, kept Qt-free - the same split `presenter.py` is for
search, kept separate because chat is its own large surface.

Layer: L5 — `WORKORDER-202626270611-chat-tab.md` §3.

**Numbering is the one thing this module has to get exactly right.**
`render_citations` puts a superscript number after each sentence;
`sources_for` lists the passages a Sources pane shows. Both have to agree
on which passage is "1" - the whole point of a superscript is that clicking
it and reading row 1 of the pane land on the same document - so both are
built from the one shared numbering pass (`_citation_numbers`) rather than
two callers each counting for themselves.
"""

from __future__ import annotations

import html
from typing import Sequence

from app.chat.verify import VerifiedSentence

__all__ = ["render_citations", "sources_for"]


def _citation_numbers(citations: Sequence[VerifiedSentence]) -> dict[int, int]:
    """`{chunk marker: display number}`, assigned in first-mention order
    across the whole answer - not by raw chunk index. Two sentences both
    citing chunk 4 and nothing else show source "1" both times, the first
    (and only) passage actually cited, matching §4e-2's superscript-
    numbering rule ("assigned in first-mention order... never renumbers").
    """
    numbers: dict[int, int] = {}
    for citation in citations:
        for marker in citation.markers:
            if marker not in numbers:
                numbers[marker] = len(numbers) + 1
    return numbers


def render_citations(citations: Sequence[VerifiedSentence]) -> str:
    """The answer, as HTML: each sentence followed by its superscript
    source number(s) - "The deposit was 500 pounds.<sup>1</sup>" - for a
    `QTextBrowser`/label with rich text switched on.

    Every piece of sentence text is escaped - it is the model's own
    generated wording, and however carefully verified, it is still text
    from outside this application reaching a rich-text renderer.
    """
    numbers = _citation_numbers(citations)
    parts = []
    for citation in citations:
        shown = sorted({numbers[marker] for marker in citation.markers})
        superscript = "".join(f"<sup>{n}</sup>" for n in shown)
        parts.append(f"{html.escape(citation.text)}{superscript}")
    return " ".join(parts)


def sources_for(citations: Sequence[VerifiedSentence], passages: Sequence[str]) -> list[tuple[int, str]]:
    """`(display_number, passage_text)`, in the same first-mention order
    `render_citations` numbers by - so superscript "2" in the prose and row
    2 of the Sources pane are always the same passage, never a coincidence
    of two separate counts happening to agree.
    """
    numbers = _citation_numbers(citations)
    by_number = {number: passages[marker - 1] for marker, number in numbers.items()
                if 1 <= marker <= len(passages)}
    return sorted(by_number.items())
