"""The words the chat engine says to a model, in one place.

Layer: L8b - pure text. Work order sections 1b, 1c and 2a.

Kept apart from the engine for two reasons. First, a prompt is the part of this
system most likely to be tuned against a real model, and tuning should touch one
file. Second, the test double (`app/chat/testing.py`) has to *read* these prompts
to play a model convincingly - so the format of a source block, and the way to
take one back apart, are defined together here and cannot drift.

**Small models get a short, rigid prompt.** A 1.5B model asked for "a helpful,
well-formed answer citing sources where appropriate" invents a format; asked to
"end every sentence with [n]" and shown one example, it does it. Nothing in these
prompts is trusted for correctness - every sentence that comes back is verified
against the sources it cites (`app/chat/verify.py`) - so the prompt's only job is
to make the verifiable thing likely.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

__all__ = [
    "NOT_FOUND",
    "answer_prompt",
    "extract_prompt",
    "combine_prompt",
    "planner_prompt",
    "parse_sources",
    "parse_question",
    "PROMPT_KINDS",
    "kind_of",
    "PERSONA",
    "CAPABILITIES",
    "chat_system",
    "archive_system",
    "general_system",
    "title_prompt",
    "chat_kind",
    "ARCHIVE_MARK",
    "GENERAL_MARK",
    "WEB_RULES",
    "WEB_MARK",
    "web_system",
]

#: What the model says when the sources do not contain the answer.
NOT_FOUND = "NOT FOUND"

_EXAMPLE = (
    "Example.\n"
    "[1] deposit-letter.docx\n"
    "The deposit of 950 pounds will be returned within 10 days of the tenancy ending.\n\n"
    "Question: How much was the deposit?\n"
    "Answer: The deposit was 950 pounds [1].\n"
)


def _block(sources: Sequence) -> str:
    return "\n\n".join(f"[{s.n}] {s.name}\n{s.passage}" for s in sources)


def answer_prompt(question: str, sources: Sequence, *, strict: bool = False) -> str:
    """Answer from numbered sources, one cited sentence at a time.

    `strict` is the retry: shorter, and tells the model that its first attempt
    contained sentences that could not be checked.
    """
    rules = (
        "Answer the question using ONLY the numbered sources below.\n"
        "- Write short sentences. End every sentence with the number of the source it "
        "comes from, like [1].\n"
        "- Say only what a source says. Copy names, dates and numbers exactly as written.\n"
        f"- If the sources do not answer the question, reply with exactly: {NOT_FOUND}\n"
    )
    if strict:
        rules += (
            "- Your last answer had sentences that the sources do not support. Use the "
            "source's own words as far as you can, and leave out anything you are not sure of.\n"
        )
    return (
        f"{rules}\n{_EXAMPLE}\n"
        f"Sources:\n{_block(sources)}\n\n"
        f"Question: {question}\nAnswer:"
    )


def extract_prompt(question: str, source) -> str:
    """One document, one question: the map step of a synthesis answer."""
    return (
        "Read the source and write what it says that helps answer the question.\n"
        f"- One to three short sentences. End every sentence with [{source.n}].\n"
        "- Say only what the source says. Copy names, dates and numbers exactly.\n"
        f"- If the source has nothing relevant, reply with exactly: {NOT_FOUND}\n\n"
        f"Sources:\n[{source.n}] {source.name}\n{source.passage}\n\n"
        f"Question: {question}\nAnswer:"
    )


def combine_prompt(question: str, extracts: Sequence) -> str:
    """The reduce step: several verified extracts in, one answer out.

    Only used when `ChatSettings.synthesis_combine` is on - see the engine.
    """
    lines = "\n".join(f"[{n}] {text}" for n, text in extracts)
    return (
        "Combine these notes into a short answer to the question.\n"
        "- End every sentence with the number of the note it comes from, like [1].\n"
        "- Say only what the notes say.\n\n"
        f"Sources:\n{lines}\n\nQuestion: {question}\nAnswer:"
    )


def planner_prompt(question: str, tried: Sequence[str]) -> str:
    """Ask for different searches after a thin first round. JSON out."""
    already = "; ".join(tried) if tried else "(none)"
    return (
        "You help search a personal archive of documents and emails.\n"
        "The searches so far found little. Suggest up to 3 different searches, using other "
        "words the documents might use (synonyms, related terms). Plain keywords only.\n"
        'Reply as JSON like {"queries": ["first search", "second search"]}.\n\n'
        f"Question: {question}\nSearches already tried: {already}\nJSON:"
    )


# ---------------------------------------------------------------------------
# The conversation: the assistant's voice, in ONE place
# ---------------------------------------------------------------------------
#
# Owner requirement 2026-09-20: talking to the Chat tab should feel like talking to
# an AI assistant. Everything the model is told about *who it is and how to talk*
# is here, tunable in one edit - and the person's own additions ride in through
# the `CHAT_STYLE_NOTE` setting (`style_note` below), never by editing this text.

PERSONA = (
    "You are the assistant inside Leasha, a program on this person's own computer that "
    "searches the files and email they have kept. You are talking with that one person. "
    "Everything runs on this computer: nothing said here leaves it.\n"
    "\n"
    "How you talk:\n"
    "- Warm, direct and plain-spoken, like a knowledgeable friend. Short paragraphs. Put the "
    "answer in the first sentence.\n"
    "- Never open with \"As an AI\". No lecturing, no repeating the question back, and at most one "
    "apology. When you do not know, say so and say what would help.\n"
    "- Match the length to the question: a sentence for a simple one, more when it needs it. "
    "Use markdown only when it helps - a short list, **bold** for the one thing to notice, a "
    "table to compare, a code block for code.\n"
    "- This is a conversation. Refer back to earlier messages, and when the person tells you "
    "to change your last answer (shorter, in French, simpler, continue) do exactly that.\n"
    "- Never claim to have read, opened or searched a file unless passages from it are shown "
    "to you in this message. Never invent file names, dates, amounts or quotes."
)

CAPABILITIES = (
    "What Leasha can do, if the person asks: find files and mail from a description of what "
    "they remember; answer questions from what those files say, with numbered sources they can "
    "open; count and list what is kept; and talk - explain, write, rewrite, translate, help with "
    "code and sums. It works with the internet off."
)

_NOT_SEARCHING = (
    "\nIn this reply you are not searching their files: it is conversation, or an instruction "
    "about what was already said. If they want something from their files, tell them to ask it "
    "as a question and you will look."
)


def _today_line(today: Any) -> str:
    return f"\nToday is {today:%A %d %B %Y}." if today is not None else ""


def _note_line(style_note: str) -> str:
    note = " ".join(str(style_note or "").split())
    return f"\nThe person also asked: {note[:600]}" if note else ""


def chat_system(*, style_note: str = "", today: Any = None) -> str:
    """The system message for a turn with no retrieval (small talk, an instruction
    about the last answer, a task that needs no file)."""
    return PERSONA + "\n\n" + CAPABILITIES + _NOT_SEARCHING + _today_line(today) + _note_line(style_note)


ARCHIVE_MARK = "Below are passages from the person's own files"
GENERAL_MARK = "The person's files had nothing about this"
WEB_MARK = "Below are passages from web pages"

_ARCHIVE_RULES = (
    ARCHIVE_MARK + ", numbered. Answer from them.\n"
    "- Write flowing prose, the way a person explains something - not a list of quotes. Open "
    "with the answer.\n"
    "- After every sentence that says something a passage says, put that passage's number in "
    "square brackets, like [1]. Use only the numbers shown, and put the number at the end of "
    "the sentence it supports.\n"
    "- Stay close to the passages' own words for names, figures, dates and anything quoted; copy "
    "them exactly. Put quotation marks only round words that are really in the passage.\n"
    "- If the passages answer only part of the question, answer that part and say plainly what "
    f"you did not find in their files. If they do not answer it at all, reply with exactly: {NOT_FOUND}\n"
    "- You may add a short paragraph of general knowledge if it really helps: start it with "
    "\"In general,\", give it no number, and never make it about what is in their files.\n"
    "- The numbered passages are the only things you may say about what is in their files."
)


_ONE_PER_DOCUMENT = (
    "\n- Take the documents one at a time: one short paragraph for each, saying what it says."
)


def archive_system(sources: Sequence, *, style_note: str = "", today: Any = None,
                   web: Sequence = (), combine: bool = True) -> str:
    """The system message for an answer built from retrieved passages.

    The block after `Sources:` is the format `parse_sources` reads back (the test
    double relies on it). `web` is `[(n, title, url, text), ...]` - passages from the
    web, numbered *after* the local ones, offered only when the person switched the
    web on (`app/chat/web.py`); the model is told to keep the two apart."""
    text = PERSONA + "\n\n" + _ARCHIVE_RULES + ("" if combine else _ONE_PER_DOCUMENT)
    text += _today_line(today) + _note_line(style_note)
    text += f"\n\nSources:\n{_block(sources)}"
    if web:
        text += "\n\n" + WEB_RULES + "\n\nWeb passages:\n" + "\n\n".join(
            f"[{n}] {title} ({url})\n{body}" for n, title, url, body in web)
    return text


WEB_RULES = (
    "The person also switched the web on. Passages from web pages follow the numbered file "
    "sources, numbered on from them. What their files say comes first and is never overruled by "
    "the web. Put what the web adds in its own paragraph that starts \"From the web:\", with the "
    "web passage's number after each sentence, and say so plainly if the web and their files "
    "differ."
)


def web_system(web: Sequence, *, style_note: str = "", today: Any = None) -> str:
    """The system message when the files had nothing and the person switched the web on:
    an answer from numbered web passages (each `web` item has `.n`, `.name`, `.passage`
    like a `Source`), kept visibly apart as coming from the web."""
    text = (
        PERSONA + "\n\n" + WEB_MARK + ", numbered. The person's files had nothing about "
        "this, and they have already been told so. Answer from these passages.\n"
        "- Start your answer with \"From the web:\". Write it in plain prose.\n"
        "- After every sentence that says something a passage says, put that passage's number in "
        "square brackets, like [1], at the end of the sentence. Use only the numbers shown.\n"
        "- Stay close to the passages' own words for names, figures and dates.\n"
        f"- If they do not answer it at all, reply with exactly: {NOT_FOUND}"
        + _today_line(today) + _note_line(style_note))
    return text + f"\n\nSources:\n{_block(web)}"


def general_system(*, style_note: str = "", today: Any = None) -> str:
    """The system message for the short general answer offered when the files had
    nothing (the engine has already said so, in its own words)."""
    return (PERSONA + "\n\n" + GENERAL_MARK + ", and the person has already been told so. Give a "
            "short answer from your general knowledge - a few sentences at most. Do not mention "
            "their files or that you searched. Do not make up anything about their own situation."
            + _today_line(today) + _note_line(style_note))


def title_prompt(question: str, answer: str = "") -> str:
    """Ask the small model for a conversation's name. One line out."""
    clipped = " ".join(str(answer or "").split())[:300]
    return (
        "Write a title of at most five words for this conversation. Plain words, no quotation "
        "marks, no full stop.\n\n"
        f"Question: {' '.join(str(question or '').split())[:300]}\n"
        f"Reply: {clipped}\nTitle:"
    )


def chat_kind(messages: Sequence) -> str:
    """Which conversation is this? `"archive"`, `"general"` or `"chat"`. Test-double use."""
    system = str(messages[0].get("content", "")) if messages else ""
    if ARCHIVE_MARK in system:
        return "archive"
    if WEB_MARK in system:
        return "web"
    if GENERAL_MARK in system:
        return "general"
    return "chat"


# --------------------------------------------------------------------------- reading a prompt back

_SOURCES_AT = re.compile(r"^Sources:\s*$", re.M)
_QUESTION_AT = re.compile(r"^Question:\s*(.*)$", re.M)
_HEADER = re.compile(r"^\[(\d+)\] (.*)$")

PROMPT_KINDS = ("answer", "extract", "combine", "planner", "router", "rewrite", "translate", "title", "other")


def kind_of(prompt: str) -> str:
    """Which of this module's prompts is this? For the test double."""
    head = prompt.lstrip()[:160].lower()
    if head.startswith("classify the question"):
        return "router"
    if head.startswith("rewrite the user's sentence as a search query"):
        return "translate"
    if head.startswith("write a title of at most five words"):
        return "title"
    if head.startswith("rewrite the last question"):
        return "rewrite"
    if head.startswith("you help search a personal archive"):
        return "planner"
    if head.startswith("read the source and write"):
        return "extract"
    if head.startswith("combine these notes"):
        return "combine"
    if head.startswith("answer the question using only"):
        return "answer"
    return "other"


def parse_sources(prompt: str) -> dict[int, str]:
    """`{n: passage}` from an answer/extract/combine prompt. Test-double use."""
    at = None
    for match in _SOURCES_AT.finditer(prompt):
        at = match
    if at is None:
        return {}
    body = prompt[at.end():]
    stop = _QUESTION_AT.search(body)
    if stop:
        body = body[:stop.start()]
    found: dict[int, list[str]] = {}
    current = None
    for line in body.splitlines():
        header = _HEADER.match(line)
        if header:
            current = int(header.group(1))
            # In a combine prompt the text is on the header line itself.
            found[current] = [header.group(2)]
            continue
        if current is not None:
            found[current].append(line)
    return {n: "\n".join(lines).strip() for n, lines in found.items()}


def parse_names(prompt: str) -> dict[int, str]:
    """`{n: file name}` from the header line of each source. Test-double use."""
    at = None
    for match in _SOURCES_AT.finditer(prompt):
        at = match
    if at is None:
        return {}
    names: dict[int, str] = {}
    for line in prompt[at.end():].splitlines():
        header = _HEADER.match(line)
        if header:
            names[int(header.group(1))] = header.group(2)
    return names


def parse_question(prompt: str) -> str:
    """The question line of a prompt. Test-double use."""
    match = None
    for match in _QUESTION_AT.finditer(prompt):
        pass
    return match.group(1).strip() if match else ""
