"""What the model is shown, and how much: context economy for small models.

Layer: L8b - no Qt, no network. Work order section 1c.

A 1.5B or 7B model on a CPU does not fail because its window is too small; it
fails because a *stuffed* window makes it slow and vague. Prompt processing is
the cost that grows with what is put in, and a model handed eight documents
answers about none of them. So the engine gives it the fewest passages that could
answer, cut down to the sentences around the question's own words:

* **a budget from the model's real window** (`budget_chars`): what Ollama will
  actually read (`OllamaLLM.context_window`), less room for the instructions and
  the answer - and never more than `CONTEXT_TOKEN_CAP`, because on a CPU the
  prompt is paid for at tens of tokens a second whatever the window allows;
* **one numbered `Source` per document** (`build_sources`), the best-ranked chunk
  or two of each, so a citation names a document a person can open;
* **a passage window per source** (`window_of`): the run of whole sentences
  densest in the question's terms, capped at the per-source share of the budget.
  Never mid-word (`text.sentence_spans` decides where sentences end), and always a
  *slice* of the original chunk - so what the model saw is verbatim.

`Source.pieces` keeps each chunk's **full** text. A receipt's quote is cut from
those, not from the window, which is why a quote can be more than the model saw
but never anything the index does not hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.chat.text import DEFAULT_QUOTE_MAX, content_tokens, sentence_spans, snap_span
from app.chat.types import Receipt

__all__ = [
    "Piece",
    "Source",
    "budget_chars",
    "build_sources",
    "window_of",
    "CONTEXT_TOKEN_CAP",
    "CHARS_PER_TOKEN",
]

#: The most prompt a chat model is given, whatever its window: about 1,800
#: tokens of passages. **A speed decision, measured as one**: a 1.5B model on
#: this machine's CPU reads a prompt at tens of tokens a second, so a 4,000-token
#: prompt is a minute before the first word. The window is the ceiling; this is
#: the budget.
CONTEXT_TOKEN_CAP = 1800

#: Characters per token, for turning a token budget into text. Deliberately
#: pessimistic (English averages about four) because numbers, addresses and
#: mail headers tokenise badly and an over-full prompt silently truncates.
CHARS_PER_TOKEN = 3.2

#: Tokens kept back for the instructions, the worked example and the question.
_PROMPT_OVERHEAD_TOKENS = 420
#: Tokens kept back for the answer itself.
_ANSWER_TOKENS = 300


@dataclass(frozen=True)
class Piece:
    """One chunk of a source, whole."""

    chunk_id: Optional[int]
    text: str
    page: Optional[int] = None
    label: str = ""


@dataclass
class Source:
    """A document, numbered for one answer, with everything a receipt needs."""

    n: int
    file_id: Optional[int]
    path: str
    name: str
    pieces: list[Piece]
    #: What the model reads: a window of the first piece.
    passage: str = ""
    #: Sender, recipients, subject, file name: **evidence for names and numbers
    #: only** (a sender is not in the body of the message, and the sentence
    #: "Priya approved it" is supported by the header, not the text).
    meta: str = ""
    score: float = 0.0

    def full_text(self) -> str:
        return "\n\n".join(piece.text for piece in self.pieces)

    def locator_of(self, piece_index: int) -> str:
        piece = self.pieces[piece_index]
        parts: list[str] = []
        if piece.label:
            parts.append(piece.label)
        elif piece.page is not None:
            parts.append(f"page {piece.page}")
        return ", ".join(parts)

    def receipt(self, piece_index: int, start: int, end: int) -> Receipt:
        """A `Receipt` whose quote is `pieces[piece_index].text[start:end]`.

        The slice is taken here, from the stored text - **the only way a quote
        is ever made**. Nothing upstream hands over a string to be printed.
        """
        piece = self.pieces[piece_index]
        quote = " ".join(piece.text[start:end].split())
        return Receipt(
            file_id=self.file_id, path=self.path, name=self.name, quote=quote,
            locator=self.locator_of(piece_index), chunk_id=piece.chunk_id,
        )

    def best_quote(self, words: Sequence[str], *, max_chars: int = DEFAULT_QUOTE_MAX
                   ) -> tuple[int, int, int]:
        """`(piece, start, end)` of the sentence(s) that best match `words`,
        snapped to sentence boundaries. For a receipt that has no sentence of
        its own to point at (a FIND result)."""
        wanted = set(content_tokens(" ".join(words)))
        best = (0, 0, 0, -1.0)
        for index, piece in enumerate(self.pieces):
            for start, end in sentence_spans(piece.text):
                tokens = set(content_tokens(piece.text[start:end]))
                score = len(tokens & wanted) / (len(wanted) or 1)
                if score > best[3]:
                    best = (index, start, end, score)
        index, start, end, _ = best
        if end <= start and self.pieces:
            end = min(len(self.pieces[0].text), max_chars)
        start, end = snap_span(self.pieces[index].text, start, end, max_chars=max_chars)
        return index, start, end


def budget_chars(window_tokens: int, question: str = "", *, sources: int = 4) -> tuple[int, int]:
    """`(total, per_source)` characters of passage the model may be shown.

    Derived from the model's real window less overhead and the answer, then
    capped at `CONTEXT_TOKEN_CAP`. Never below what one short passage needs, so a
    tiny window degrades to one small source rather than to nothing.
    """
    window = max(512, int(window_tokens or 0))
    usable = window - _PROMPT_OVERHEAD_TOKENS - _ANSWER_TOKENS - int(len(question) / CHARS_PER_TOKEN)
    tokens = max(180, min(usable, CONTEXT_TOKEN_CAP))
    total = int(tokens * CHARS_PER_TOKEN)
    per_source = max(300, total // max(1, int(sources)))
    return total, per_source


def window_of(text: str, terms: Sequence[str], width: int) -> str:
    """The run of whole sentences of `text` densest in `terms`, at most `width`
    characters - a slice of `text`. Whole text when it already fits."""
    source = str(text or "")
    if len(source) <= width:
        return source.strip()
    spans = sentence_spans(source)
    if not spans:
        return source[:width].rsplit(" ", 1)[0]
    wanted = set(content_tokens(" ".join(terms)))
    scores = [len(set(content_tokens(source[s:e])) & wanted) for s, e in spans]

    best_run = (0, 0)
    best_score = -1
    for i in range(len(spans)):
        j = i
        while j + 1 < len(spans) and spans[j + 1][1] - spans[i][0] <= width:
            j += 1
        if spans[j][1] - spans[i][0] > width:
            continue
        score = sum(scores[i:j + 1])
        if score > best_score:
            best_score, best_run = score, (i, j)
    i, j = best_run
    if spans[j][1] - spans[i][0] > width:                # one sentence longer than the width
        from app.search.window import rerank_window

        return rerank_window(source[spans[i][0]:spans[i][1]], terms, width=width)
    return source[spans[i][0]:spans[j][1]]


def _name_of(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or str(path)


def _folders_of(path: str) -> str:
    """The folder names of a path, drive letter left out - people file things
    under "Leeds" or "Tenancy", and that is evidence about what a document is."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/")[:-1] if p and not p.endswith(":")]
    return " ".join(parts[-3:])


def build_sources(
    results: Sequence[Any],
    terms: Sequence[str],
    *,
    max_sources: int,
    window_tokens: int,
    question: str = "",
    metas: Optional[dict[int, dict]] = None,
    first_number: int = 1,
    chunks_per_file: int = 2,
) -> list[Source]:
    """Numbered sources from ranked search results, best document first.

    `results` are `SearchResult`-shaped (`chunk_id`, `file_id`, `path`, `text`,
    `page`, `label`, `rank`). One `Source` per file, up to `chunks_per_file` of
    its chunks; the model is shown a window of the best one.
    """
    metas = metas or {}
    order: list[int] = []
    by_file: dict[int, list[Any]] = {}
    for result in results:
        key = int(getattr(result, "file_id", 0) or 0)
        if key not in by_file:
            if len(order) >= max_sources:
                continue
            order.append(key)
            by_file[key] = []
        if len(by_file[key]) < chunks_per_file:
            by_file[key].append(result)

    _total, per_source = budget_chars(window_tokens, question, sources=len(order) or 1)
    sources: list[Source] = []
    for offset, key in enumerate(order):
        chunk_results = by_file[key]
        head = chunk_results[0]
        pieces = [Piece(chunk_id=int(getattr(r, "chunk_id", 0) or 0) or None,
                        text=str(getattr(r, "text", "") or ""),
                        page=getattr(r, "page", None), label=str(getattr(r, "label", "") or ""))
                  for r in chunk_results]
        meta_row = metas.get(key) or {}
        meta = " ".join(str(x) for x in (
            _name_of(head.path), _folders_of(head.path), meta_row.get("sender", ""),
            meta_row.get("recipients", ""), meta_row.get("subject", "")) if x)
        sources.append(Source(
            n=first_number + offset, file_id=key or None, path=str(head.path),
            name=_name_of(head.path), pieces=pieces,
            passage=window_of(pieces[0].text, terms, per_source), meta=meta,
            score=float(getattr(head, "score", 0.0) or 0.0),
        ))
    return sources
