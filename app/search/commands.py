"""The filters, described - so they can be discovered rather than memorised.

Layer: L4

Every filter here already worked. `type:pdf from:dave after:2024` has parsed
correctly since Layer 4 was built. The problem was that **nothing anywhere told
anybody they existed**, so in practice the search box was a bag of words and the
operators were a feature only their author could use.

That is a common and expensive shape of failure: the capability is built, tested
and shipped, and delivers nothing because it is invisible.

So this module is the catalogue. One list, in code, feeding three things that
must never disagree:

  * the `/` dropdown in the search box,
  * `app.cli commands`,
  * the prompt that Layer 8a gives the model, which needs to know precisely which
    operators exist and what a valid value looks like.

The third is why this is here in `app/search/` rather than in the UI. A model
told about an operator the parser does not have will invent queries that silently
match nothing - so the grammar the model is shown and the grammar the parser
accepts have to come from the same place.

**`/type pdf` and `type:pdf` are the same query.** The slash is a UI affordance -
it opens the list - and is rewritten to a colon before parsing. Nothing in the
parser knows about slashes, and nothing needs to: one grammar, one set of tests,
one thing to get right.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "Command",
    "COMMANDS",
    "command_for",
    "matching",
    "expand_slashes",
    "help_lines",
    "grammar_for_model",
]


@dataclass(frozen=True, slots=True)
class Command:
    """One filter, as a person needs to understand it."""

    #: The canonical name, as typed after the slash or before the colon.
    name: str
    #: Other spellings the parser accepts. Shown so nobody has to guess.
    aliases: tuple[str, ...]
    #: What it does, in one line, in plain words.
    summary: str
    #: A real example somebody could type today.
    example: str
    #: What a valid value looks like - for the dropdown hint and the model prompt.
    value_hint: str
    #: True when the value is a date, which has its own forgiving syntax.
    is_date: bool = False

    @property
    def spellings(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


#: Every filter the parser accepts. Kept in the order somebody would want them:
#: what kind of thing, who it came from, when, and where it lives.
#:
#: **This list must match `_FIELD_ALIASES` in `query.py`.** A test asserts it,
#: because a filter offered here that the parser rejects is worse than one that
#: is merely undocumented - it is a promise the application breaks.
COMMANDS: tuple[Command, ...] = (
    Command(
        name="type",
        aliases=("ext", "kind"),
        summary="Only this kind of file",
        example="/type pdf",
        value_hint="pdf, docx, xlsx, email, code - or several: pdf,docx",
    ),
    Command(
        name="from",
        aliases=("sender",),
        summary="Only email from this person",
        example="/from dave",
        value_hint="part of a name or address; dave matches dave.smith@acme.com",
    ),
    Command(
        name="to",
        aliases=("recipient", "cc"),
        summary="Only email sent to this person",
        example="/to priya",
        value_hint="part of a name or address; matches the To and Cc lines",
    ),
    Command(
        name="subject",
        aliases=("title", "re"),
        summary="Only email whose subject contains this",
        example="/subject licence",
        value_hint='any part of the subject; quote it for several words: "licence renewal"',
    ),
    Command(
        name="has",
        aliases=(),
        summary="Only email with (or without) an attachment",
        example="/has attachment",
        value_hint="attachment, or no-attachment",
    ),
    Command(
        name="after",
        aliases=("since",),
        summary="Only things changed on or after this date",
        example="/after 2024-06-01",
        value_hint="2024-06-01, 2024, last month, 30d",
        is_date=True,
    ),
    Command(
        name="before",
        aliases=("until",),
        summary="Only things changed on or before this date",
        example="/before 2025-03-01",
        value_hint="2025-03-01, 2025, yesterday, 2y",
        is_date=True,
    ),
    Command(
        name="path",
        aliases=("folder", "dir"),
        summary="Only inside folders whose path contains this",
        example="/path projects/leeds",
        value_hint="any part of a folder path",
    ),
)

#: Not filters, but the other two things the search box understands. Listed in
#: the dropdown because a person looking for "how do I search" wants all of it
#: in one place, and these are the two most useful and least guessable.
EXTRAS: tuple[tuple[str, str, str], ...] = (
    ('"exact phrase"', "Words in this exact order", '"site survey report"'),
    ("-word", "Leave out anything containing this word", "-draft"),
)

_BY_SPELLING = {
    spelling: command for command in COMMANDS for spelling in command.spellings
}

#: `/name value` at a word boundary. The value is optional so a half-typed
#: `/from` still resolves while somebody is still choosing.
_SLASH = re.compile(r"(?:(?<=\s)|^)/([A-Za-z]+)(?=[\s:]|$)")


def command_for(name: str) -> Optional[Command]:
    """The command for any accepted spelling, or None."""
    return _BY_SPELLING.get(name.strip().lower().lstrip("/").rstrip(":"))


def matching(prefix: str) -> list[Command]:
    """Commands whose name or alias starts with `prefix`. For the dropdown.

    An empty prefix returns everything, which is what typing a bare `/` should
    show: the point is discovery, so the first keystroke reveals the whole set.
    """
    cleaned = prefix.strip().lower().lstrip("/")
    if not cleaned:
        return list(COMMANDS)
    return [
        command for command in COMMANDS
        if any(spelling.startswith(cleaned) for spelling in command.spellings)
    ]


def expand_slashes(text: str) -> str:
    """Rewrite `/type pdf` to `type:pdf`, leaving everything else untouched.

    Called once before `parse_query`, so the parser never learns about slashes.
    One grammar, one set of tests.

    An unknown `/word` is **left exactly as it is** rather than stripped or
    guessed at. Somebody searching for a Unix path, a URL fragment or a date
    written `12/03` must get what they typed; silently deleting part of a query
    is the one behaviour that would make the box untrustworthy.

    >>> expand_slashes("/type pdf leeds")
    'type: pdf leeds'
    >>> expand_slashes("report about /var/log")
    'report about /var/log'
    """
    def replace(match: re.Match[str]) -> str:
        command = command_for(match.group(1))
        return f"{command.name}:" if command else match.group(0)

    # The space between `/type` and `pdf` is collapsed by the parser's own
    # tokeniser, so `type: pdf` and `type:pdf` are the same query to it.
    expanded = _SLASH.sub(replace, text)
    return re.sub(r"\b([a-z]+):\s+(?=\S)", r"\1:", expanded)


def help_lines() -> list[str]:
    """The catalogue as plain text, for `app.cli commands` and `--help`."""
    width = max(len(command.example) for command in COMMANDS)
    out = ["Filters you can type in the search box:", ""]
    for command in COMMANDS:
        out.append(f"  {command.example.ljust(width)}   {command.summary}")
        out.append(f"  {' ' * width}   {command.value_hint}")
        others = ", ".join(f"/{alias}" for alias in command.aliases)
        if others:
            out.append(f"  {' ' * width}   also: {others}")
        out.append("")
    out.append("Also:")
    for syntax, summary, example in EXTRAS:
        out.append(f"  {syntax.ljust(width)}   {summary}  ({example})")
    out += [
        "",
        "Combine them freely, with or without the slash:",
        '  /type pdf /from dave /after 2024-01-01 "site survey" -draft',
        "  type:pdf from:dave after:2024-01-01",
        "",
        "Everything not part of a filter is searched for normally - by keyword",
        "and by meaning at the same time.",
    ]
    return out


def grammar_for_model() -> str:
    """The operator grammar, for Layer 8a's translation prompt.

    Generated from the same list the parser and the dropdown use, so a model can
    never be told about an operator that does not exist. A model given a
    plausible-but-wrong operator produces queries that match nothing and give no
    hint why - which is exactly the failure that makes AI search untrustworthy.
    """
    lines = ["Available operators (use only these):"]
    for command in COMMANDS:
        lines.append(f"  {command.name}:<value>   {command.summary}. Value: {command.value_hint}")
    lines += [
        '  "quoted phrase"   words in this exact order',
        "  -word             exclude anything containing this word",
        "",
        "Dates must be written as YYYY-MM-DD.",
        "Anything not covered by an operator stays as plain search words.",
    ]
    return "\n".join(lines)
