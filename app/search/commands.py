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
    "examples_for_model",
    "EXAMPLES",
    "RELATIVE_DATES",
]


#: Date spellings worth offering, because they are easier to pick than to
#: remember. `query.py` accepts all of these; the list is short on purpose -
#: a dropdown of thirty ways to say "recently" is not a shortcut.
RELATIVE_DATES: tuple[str, ...] = (
    "today", "yesterday", "7d", "30d", "90d", "last month", "1y",
)


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
    #: A one-character glyph for the dropdown, so the list can be scanned by
    #: shape rather than read line by line. **A character, not an image**: no
    #: asset to ship, no second copy to redraw for dark mode, and it is painted
    #: in the palette's own colour by the widget - see `widgets/command_icon.py`.
    icon: str = "•"
    #: Values worth offering that are fixed by the grammar rather than found in
    #: anybody's index. `/has` has exactly two; a date has a handful of useful
    #: spellings that are easier to pick than to remember.
    values: tuple[str, ...] = ()
    #: Where *real* values come from, when they can be read from the index.
    #: One of the keys `SqliteStore.distinct_values` accepts, or "" for none.
    #:
    #: **The point of the whole feature.** `/type <type>` tells you a filter
    #: exists; `/type` offering `pdf`, `docx`, `msg` tells you what is actually
    #: in there - and the difference is between a filter you can use and one you
    #: have to guess at. A value that returns nothing is the commonest way a
    #: working filter looks broken.
    source: str = ""

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
        value_hint="an extension, or a kind: excel, mail, code - or several: pdf,docx",
        icon="▤",
        source="ext",
        # **The kind words `_EXT_GROUPS` expands, listed because they parse.**
        #
        # Every one of these has worked since Layer 4 and not one was ever
        # offered: `/type` carries a `source`, so the menu was filled from the
        # index alone and `files.ext` has no row saying "excel". The hint above
        # even named two of them, so the menu advertised values it would not
        # complete - which is the exact failure the note on `COMMANDS` warns
        # about, arrived at from the value end instead of the operator end.
        #
        # **This tuple must match `_EXT_GROUPS` in `query.py`.** A test asserts
        # it, for the same reason one asserts the alias table: a value offered
        # here that the parser does not expand matches nothing, and a value the
        # parser knows that is missing here stays invisible.
        values=("word", "excel", "sheet", "slides", "powerpoint",
                "mail", "email", "text", "code", "doc", "xls", "ppt"),
    ),
    Command(
        name="from",
        aliases=("sender",),
        summary="Only email from this person",
        example="/from dave",
        value_hint="part of a name or address; dave matches dave.smith@acme.com",
        icon="✉",
        source="sender",
    ),
    Command(
        name="to",
        aliases=("recipient", "cc"),
        summary="Only email sent to this person",
        example="/to priya",
        value_hint="part of a name or address; matches the To and Cc lines",
        icon="✉",
    ),
    Command(
        name="subject",
        aliases=("title", "re"),
        summary="Only email whose subject contains this",
        example="/subject licence",
        value_hint='any part of the subject; quote it for several words: "licence renewal"',
        icon="≡",
    ),
    Command(
        name="has",
        aliases=(),
        summary="Only email with (or without) an attachment",
        example="/has attachment",
        value_hint="attachment, or no-attachment",
        icon="↧",
        values=("attachment", "no-attachment"),
    ),
    Command(
        name="after",
        aliases=("since",),
        summary="Only things changed on or after this date",
        example="/after 2024-06-01",
        value_hint="2024-06-01, 2024, last month, 30d",
        is_date=True,
        icon="◷",
        values=RELATIVE_DATES,
    ),
    Command(
        name="before",
        aliases=("until",),
        summary="Only things changed on or before this date",
        example="/before 2025-03-01",
        value_hint="2025-03-01, 2025, yesterday, 2y",
        is_date=True,
        icon="◶",
        values=RELATIVE_DATES,
    ),
    Command(
        name="path",
        aliases=("folder", "dir"),
        summary="Only inside folders whose path contains this",
        example="/path projects/leeds",
        value_hint="any part of a folder path",
        icon="▸",
        source="folder",
    ),
    Command(
        name="repo",
        aliases=("repository", "project"),
        summary="Only files in this code repository",
        example="/repo leasha",
        value_hint="a repository name, as shown in the Code tab - or several: leasha,tools",
        icon="⌥",
        source="repo",
    ),
    Command(
        name="name",
        aliases=("filename", "file"),
        summary="Only files whose NAME contains this",
        example="/name invoice",
        value_hint="part of a filename - not the folder, which is /path",
        icon="▫",
    ),
    Command(
        name="sort",
        aliases=("newest", "latest", "oldest"),
        summary="Newest first, instead of best match first",
        example="/newest",
        value_hint="no value needed - or /oldest for the other direction",
        icon="↓",
        values=("newest", "oldest"),
    ),
    Command(
        name="size",
        aliases=("bigger", "smaller"),
        summary="Only files above or below a size",
        example="/size >1mb",
        value_hint=">1mb, <500kb, >=10mb; a bare 1mb means at least that",
        icon="⚖",
        values=(">1mb", ">10mb", ">100mb", "<100kb", "<1mb"),
    ),
)

#: Not filters, but the other two things the search box understands. Listed in
#: the dropdown because a person looking for "how do I search" wants all of it
#: in one place, and these are the two most useful and least guessable.
EXTRAS: tuple[tuple[str, str, str], ...] = (
    ('"exact phrase"', "Words in this exact order", '"site survey report"'),
    ("-word", "Leave out anything containing this word", "-draft"),
    ("A OR B", "Either one. MUST BE CAPITALS", "pump OR valve"),
    ("A AND B", "Both. This is the default, so rarely needed", "pump AND valve"),
    ("NOT word", "Same as -word", "NOT draft"),
    ("word*", "Starts with", "install*"),
)

#: **Spellings** that carry their meaning in their own name and take no
#: argument. `git`'s catalogue has the same idea and calls it `FLAGS`.
#:
#: Keyed on the spelling rather than on the command, because the two differ
#: here: `/newest` needs no value and `/sort date` does, and they are the same
#: command. Keying on the name made `/sort date` expand to `sort:sort` and the
#: value it was given became a search term.
VALUELESS = frozenset({"newest", "latest", "oldest"})

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
        if command is None:
            return match.group(0)
        if match.group(1).lower() in VALUELESS:
            # **A switch with no value must not eat the next word.** `/newest
            # pump` expanded to `newest:` and the collapse below then glued
            # `pump` on as its argument - so the sort switch consumed the
            # search term and the query became a sort of nothing. The spelling
            # already carries the value; there is nothing to supply.
            return f"{match.group(1).lower()}:{match.group(1).lower()} "
        return f"{command.name}:"

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
        # **The canonical name, when the example does not contain it.**
        # `/newest` is the natural example for `sort`, and rendering only that
        # left the word `sort` nowhere in the help at all - so `sort:date`,
        # which the parser accepts, was undiscoverable.
        spellings = list(command.aliases)
        if command.name not in command.example:
            spellings.insert(0, command.name)
        others = ", ".join(f"/{alias}" for alias in spellings)
        if others:
            out.append(f"  {' ' * width}   also: {others}")
        out.append("")
    out.append("Operators:")
    for syntax, summary, example in EXTRAS:
        out.append(f"  {syntax.ljust(width)}   {summary}  ({example})")
    out += [
        "",
        "AND, OR and NOT must be CAPITALS. Lowercase 'and' and 'or' are searched",
        "for as ordinary words, because 'salt and pepper' is a real thing to look",
        "for and breaking it would be worse than the feature is worth.",
        "",
        "Combine them freely, with or without the slash:",
        '  /type pdf /from dave /after 2024-01-01 "site survey" -draft',
        "  /name invoice /size >1mb",
        "  pump OR valve /path leeds",
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
    # **The hints are for the dropdown, not for the model.** They exist to help
    # a person remember what a value looks like, and they made this prompt 1,900
    # characters - most of it explanation the model does not need, and all of it
    # paid for on every single translation. The examples below teach the format
    # far better than prose does, and they cost a fraction of the tokens.
    from app.search.query import _EXT_GROUPS

    # **Closed sets are spelled out; open ones are not.**
    #
    # Shortening this prompt, I dropped every value hint - and the model
    # immediately produced `type:project` for "a project schedule file".
    # `project` is not a file extension, the parser accepted it as one, and the
    # search then matched nothing while looking entirely deliberate.
    #
    # `type:` and `has:` take one of a fixed list. A model cannot guess a closed
    # set and must not be asked to. `from:`, `subject:` and the rest take
    # arbitrary text, where listing examples costs tokens and teaches nothing.
    types = sorted({*_EXT_GROUPS, "pdf", "docx", "xlsx", "pptx", "txt", "md", "csv"})
    closed = {
        "type": ", ".join(types),
        "has": "attachment, no-attachment",
    }

    lines = ["Operators (use only these):"]
    for command in COMMANDS:
        allowed = closed.get(command.name)
        if allowed:
            lines.append(f"  {command.name}:<value>  {command.summary}. "
                         f"ONLY one of: {allowed}")
        else:
            lines.append(f"  {command.name}:<value>  {command.summary}")
    lines += [
        '  "quoted phrase"  exact order',
        "  -word  exclude it",
        "  A OR B  either (capitals)",
        "",
        "Dates as YYYY-MM-DD. Anything with no operator stays as plain words.",
    ]
    return "\n".join(lines)


#: Worked examples for the translation prompt.
#:
#: **A small model needs these far more than it needs rules.** With rules only,
#: qwen2.5:1.5b echoed the sentence back unchanged - which the application then
#: reported as "nothing to interpret", about a sentence that plainly said "from
#: chris". Rules describe the format; examples demonstrate it, and a 1.5B model
#: pattern-matches much better than it reasons.
#:
#: Chosen to cover one case each: a sender, a date and a file type, an
#: exclusion, and - the one most often missed - a sentence with no constraints
#: at all, so the model learns that returning bare words is a correct answer
#: rather than a failure to find an operator.
EXAMPLES: tuple[tuple[str, str], ...] = (
    ("emails from dave about the contract renewal", "from:dave contract renewal"),
    ("the pdf about pump maintenance from last March", "type:pdf pump maintenance after:2025-03-01 before:2025-04-01"),
    ("spreadsheets Priya sent me with attachments", "type:xlsx from:priya has:attachment"),
    ("notes on the leeds site but not the survey", "leeds site -survey"),
    ("quarterly revenue figures", "quarterly revenue figures"),
)


def examples_for_model() -> str:
    """The worked examples, formatted like the real request that follows them."""
    lines = ["Examples:"]
    for sentence, query in EXAMPLES:
        lines.append(f"Sentence: {sentence}")
        lines.append(f"Query: {query}")
    return "\n".join(lines)
