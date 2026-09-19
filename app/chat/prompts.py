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
from typing import Sequence

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


# --------------------------------------------------------------------------- reading a prompt back

_SOURCES_AT = re.compile(r"^Sources:\s*$", re.M)
_QUESTION_AT = re.compile(r"^Question:\s*(.*)$", re.M)
_HEADER = re.compile(r"^\[(\d+)\] (.*)$")

PROMPT_KINDS = ("answer", "extract", "combine", "planner", "router", "rewrite", "translate", "other")


def kind_of(prompt: str) -> str:
    """Which of this module's prompts is this? For the test double."""
    head = prompt.lstrip()[:160].lower()
    if head.startswith("classify the question"):
        return "router"
    if head.startswith("rewrite the user's sentence as a search query"):
        return "translate"
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
