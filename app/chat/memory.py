"""What the model remembers of the conversation, fitted to what it can actually read.

Layer: L8b - pure text, no Qt, no network. Owner requirement 2026-09-20 ("the chat
has to behave like i am talking to ai chat like in claude"), point 2: **real
multi-turn memory**.

The model is given the conversation as role-tagged messages (`/api/chat`), not a
flattened prompt, so "shorter", "and the second one?", "translate that to French",
"why?" and "continue" all work: the earlier turns are simply *there*.

**A window sized from the model's real context.** `pack` is told how many tokens
the model will read (`OllamaLLM.context_window()` - the smaller of what it was
trained for and what Ollama is asked to give it) and how many to leave for the
reply. The newest turns are kept whole; when they do not all fit, older ones go
**whole-turn first** and the one that straddles the edge is cut at a sentence, never
mid-sentence. What was dropped is not silently forgotten: a short digest of it (one
line per dropped turn, the first sentence, clipped at a word) rides in the system
message, so "what did I ask you at the start?" still has an answer.

Token counts are an estimate (`estimate_tokens`, a third of a character - a little
pessimistic for English, about right for code): there is no tokenizer to hand and
the failure mode of guessing high is a slightly shorter memory, while guessing low
is a model that silently loses the start of the conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.chat.text import sentence_spans

__all__ = ["Packed", "pack", "estimate_tokens", "turn_text", "clip_to_sentences", "strip_markers",
           "SAFETY_TOKENS"]

#: Tokens held back for the template's own framing (role tags, separators) that the
#: character count cannot see.
SAFETY_TOKENS = 96

#: Never spend more than this share of the window on the digest of dropped turns.
DIGEST_SHARE = 0.10
DIGEST_MAX_TOKENS = 380

_MARKER = re.compile(r"\s*\[\d{1,3}(?:\s*[,;]\s*\d{1,3})*\]")

#: What the engine itself writes around a model's words - a small model copies whatever it
#: sees in its own earlier replies, so these are not shown back to it: the plain sentence and
#: the label that open a "not from your files" answer, and the closing "could not confirm".
_ENGINE_LEAD = re.compile(r"^I couldn't find that in your files\.\s*(?:\*\*Not from your files:\*\*\s*)?", re.I)
_ENGINE_TAIL = re.compile(r"\s*I could not confirm the rest of that from your files\.\s*$", re.I)


def estimate_tokens(text: str) -> int:
    """A deliberately pessimistic token count: one per three characters."""
    return len(text or "") // 3 + 1


def strip_markers(text: str) -> str:
    """The text without `[n]` source markers. **An old answer's `[1]` means nothing
    to the next one** - each answer numbers its own sources - and a model shown a
    stale `[1]` will happily cite it again."""
    return _MARKER.sub("", text or "").strip()


def turn_text(turn: Any) -> str:
    """What the model should see of one turn. `""` when it should see nothing."""
    role = getattr(turn, "role", "")
    text = str(getattr(turn, "text", "") or "").strip()
    kind = getattr(turn, "kind", "answer")
    if not text or kind == "error":
        return ""
    if role != "assistant":
        return text
    text = strip_markers(text)
    if kind == "general":
        text = _ENGINE_LEAD.sub("", text).strip() or text
    text = _ENGINE_TAIL.sub("", text).strip() or text
    results = getattr(turn, "result_set", None)
    if results and kind in ("find", "aggregate"):
        names = []
        for row in list(results)[:8]:
            path = str(getattr(row, "path", "") or "")
            if path:
                names.append(re.split(r"[\\/]", path)[-1])
        if names:
            text += " (Files shown: " + "; ".join(names) + ".)"
    return text


def clip_to_sentences(text: str, max_tokens: int, *, keep: str = "start") -> str:
    """`text` cut to whole sentences that fit `max_tokens`. **Never mid-sentence**:
    if not even the first sentence fits it is cut at a word and marked with an
    ellipsis. `keep="end"` keeps the last sentences instead (for what somebody
    pasted, where the end is usually the point)."""
    text = (text or "").strip()
    if estimate_tokens(text) <= max_tokens:
        return text
    spans = sentence_spans(text) or [(0, len(text))]
    pieces = [text[a:b].strip() for a, b in spans]
    if keep == "end":
        pieces = list(reversed(pieces))
    out: list[str] = []
    used = 0
    for piece in pieces:
        cost = estimate_tokens(piece) + 1
        if used + cost > max_tokens:
            break
        out.append(piece)
        used += cost
    if keep == "end":
        out.reverse()
    if not out:
        limit = max(20, max_tokens * 3)
        cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
        return cut.rstrip(",.;:") + " ..."
    joined = " ".join(out)
    return joined if keep == "start" else "... " + joined


@dataclass
class Packed:
    """The conversation, ready to send."""

    messages: list[dict[str, str]] = field(default_factory=list)
    #: Turns (a user or an assistant message) shown to the model whole or cut.
    kept: int = 0
    #: Older turns that no longer fit, summarised in the system message.
    dropped: int = 0
    digest: str = ""
    tokens: int = 0
    #: True when the newest turn shown was cut to a sentence boundary to fit.
    clipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"kept": self.kept, "dropped": self.dropped, "tokens": self.tokens,
                "digest": bool(self.digest), "clipped": self.clipped}


def _first_sentence(text: str, limit: int = 130) -> str:
    text = " ".join(text.split())
    spans = sentence_spans(text)
    first = text[spans[0][0]:spans[0][1]] if spans else text
    if len(first) <= limit:
        return first
    return (first[:limit].rsplit(" ", 1)[0] or first[:limit]).rstrip(",.;:") + " ..."


def _digest(dropped: Sequence[tuple[str, str]], budget: int) -> str:
    """One line per dropped message, newest kept first when they do not all fit."""
    lines: list[str] = []
    used = 0
    for role, text in reversed(list(dropped)):
        who = "The person said" if role == "user" else "You replied"
        line = f"- {who}: {_first_sentence(text)}"
        cost = estimate_tokens(line)
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
    lines.reverse()
    if not lines:
        return ""
    more = len(dropped) - len(lines)
    head = "Earlier in this conversation (older messages, shortened"
    head += f"; {more} older still not shown):" if more > 0 else "):"
    return head + "\n" + "\n".join(lines)


def _messages_of(history: Sequence[Any]) -> list[tuple[str, str]]:
    """`(role, text)` pairs the model may see, alternating, starting with the person.

    A turn with nothing to show (an error, an empty reply) is dropped **with the
    person's message it answered**, so the model never sees two questions in a row
    with no answer between them."""
    pairs: list[tuple[str, str]] = []
    for turn in history:
        role = "assistant" if getattr(turn, "role", "") == "assistant" else "user"
        text = turn_text(turn)
        if not text:
            if role == "assistant" and pairs and pairs[-1][0] == "user":
                pairs.pop()
            continue
        if pairs and pairs[-1][0] == role:
            pairs[-1] = (role, pairs[-1][1] + "\n\n" + text)
        else:
            pairs.append((role, text))
    while pairs and pairs[0][0] != "user":
        pairs.pop(0)
    return pairs


def pack(system: str, history: Sequence[Any], question: str, *, window_tokens: int,
         reserve_tokens: int = 700) -> Packed:
    """`system` + as much of `history` as fits + `question`, as chat messages.

    `window_tokens` is what the model will read; `reserve_tokens` is left free for
    its reply. The newest turns are kept, oldest dropped whole (see the module
    docstring), and the straddling one cut at a sentence.
    """
    pairs = _messages_of(history)
    question = str(question or "").strip()
    fixed = estimate_tokens(system) + estimate_tokens(question) + reserve_tokens + SAFETY_TOKENS
    room = max(0, int(window_tokens) - fixed)

    digest = ""
    kept: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    clipped = False
    for _attempt in range(2):
        budget = room - (estimate_tokens(digest) if digest else 0)
        kept, dropped, clipped = [], [], False
        used = 0
        for index in range(len(pairs) - 1, -1, -1):
            role, text = pairs[index]
            cost = estimate_tokens(text) + 4
            if used + cost <= budget:
                kept.append((role, text))
                used += cost
                continue
            left = budget - used - 4
            if not kept or (left >= 120 and not dropped):
                # The turn that straddles the edge: keep what fits of it, whole
                # sentences only - and always keep *something* of the newest.
                shortened = clip_to_sentences(text, max(left, 60),
                                              keep="end" if role == "user" else "start")
                kept.append((role, shortened))
                used += estimate_tokens(shortened) + 4
                clipped = clipped or shortened != text
                dropped = list(pairs[:index])
            else:
                dropped = list(pairs[:index + 1])
            break
        kept.reverse()
        if not dropped:
            digest = ""
            break
        digest = _digest(dropped, min(DIGEST_MAX_TOKENS, int(window_tokens * DIGEST_SHARE)))
        if _attempt == 0 and not digest:
            break
    while kept and kept[0][0] != "user":
        dropped.append(kept.pop(0))          # an answer must never lead: it answers nothing shown

    content = system + ("\n\n" + digest if digest else "")
    messages = [{"role": "system", "content": content}]
    messages += [{"role": role, "content": text} for role, text in kept]
    messages.append({"role": "user", "content": question})
    tokens = sum(estimate_tokens(m["content"]) + 4 for m in messages)
    return Packed(messages=messages, kept=len(kept), dropped=len(dropped), digest=digest,
                  tokens=tokens, clipped=clipped)
