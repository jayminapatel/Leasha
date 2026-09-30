r"""Searching a repository's history, in the same `/` grammar as everything else.

Layer: L4 (Qt-free, git-free - it builds commands, it does not run them)

`GitSearch.txt` asks for a repository intelligence platform: 22 use cases, a
dozen search modes, a dozen methods, and filters for files, branches, commits,
authors and dates. The owner's instruction was *"all the functionality in this
should be available as switches … the style should be the / style we have in the
app"*.

**Two things had to be reconciled to do that honestly.**

*This is a second search domain, not a filter on the first.* Everything the `/`
menu did until now narrowed the **index** - SQLite rows describing files that
were read once, at index time. A repository's history is not in the index and
cannot be: it is in `.git`, it is thousands of versions of files that no longer
exist, and reaching it means running git. So the grammar is shared and the
engine is not.

*And it is slow.* Measured on this project: `git log -S` over 75 commits takes
1.59s where `git grep` over one revision takes 0.33s, and the cost of the first
is proportional to history. The first non-negotiable in this application is that
no unbounded work sits behind the Enter key. So a history search is an explicit,
cancellable action on the Code tab - never a keystroke, and never the search
box's Enter.

**What this module is.** The catalogue, the parser, and the translation from a
parsed query into the exact `git` argument list that answers it. It runs no
subprocess and imports no Qt, so every switch in the specification can be tested
by asserting on the command it produces - on a machine with no repository, and
without waiting for git.

**Only switches that actually run are offered.** The project's own rule is that
a list is not an affordance if half of it does nothing: a menu full of commands
that quietly fail is worse than no menu, because it is discovered one
disappointment at a time. `--compare`, `--branch-timeline`, `--references`,
`--team` and the security-artifact scanners in the specification are real work
that git does not do in one invocation, and they are absent here rather than
present and broken. `docs/WORKORDER-git-search-ui.md` records which and why.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

from app.search.commands import Command

__all__ = [
    "GIT_COMMANDS",
    "GitPlan",
    "GitQuery",
    "KIND_GREP",
    "KIND_LOG",
    "KIND_NAME_STATUS",
    "KIND_PATCH",
    "SYMBOL_PATTERNS",
    "build",
    "git_command_for",
    "git_matching",
    "parse_git_query",
]

#: One revision's tree, searched with `git grep`. Cost is the size of the
#: checkout and nothing to do with history.
KIND_GREP = "grep"
#: `git log -S` / `-G`: which *commits* touched the pattern. Cost is
#: proportional to history times changed files.
KIND_LOG = "log"
#: The same, with the diff, so the matching lines can be shown.
KIND_PATCH = "patch"
#: `git log --name-status`: a file's life rather than its contents.
KIND_NAME_STATUS = "name-status"


#: Loose patterns for the "search for a declaration" switches.
#:
#: **Deliberately language-agnostic and deliberately approximate.** A real
#: answer needs a parser per language; this is a regex that finds the line where
#: something is declared in the C family, Python, Java, C#, Go, Rust and
#: TypeScript, and it will occasionally match a comment. That is the right trade
#: for a search box - the alternative is twelve parsers or nothing - but it is
#: stated here rather than implied, because a tool that is quietly approximate
#: is one whose empty results get believed.
SYMBOL_PATTERNS: dict[str, str] = {
    "class": r"\b(class|record|struct)\s+{name}\b",
    "interface": r"\b(interface|protocol|trait)\s+{name}\b",
    "function": r"\b(def|fn|func|function|sub)\s+{name}\b|"
                r"\b{name}\s*\([^)]*\)\s*(\{{|:|=>)",
    "symbol": r"\b{name}\b",
    "endpoint": r"[\"'`/]{name}[\"'`/]|"
                r"(Route|Path|Get|Post|Put|Delete|Patch|Mapping)[^\n]*{name}",
    "config": r"^\s*[\"']?{name}[\"']?\s*[:=]",
}


@dataclass(frozen=True, slots=True)
class GitQuery:
    """One repository search, as the grammar read it.

    Frozen, because it is handed to a worker: a query that can be edited after
    it has been dispatched is a result that cannot be attributed to anything.
    """

    #: What to look for. Empty is legal for the switches that ask a question
    #: about a *file* rather than about content - `/file-history` for one.
    text: str = ""

    # -- where ---------------------------------------------------------------
    #: "files" | "branch" | "all-branches" | "remote-branches" | "history" |
    #: "commit" | "range" | "tag" | "lifetime"
    scope: str = "files"
    branches: tuple[str, ...] = ()
    commits: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    #: `a..b`, from `/range` or from `/between-tags`.
    rev_range: str = ""
    #: Which repository to search. **Read here and never put in the command.**
    #: git is *run inside* a checkout, so this is the caller's business - but it
    #: has to be consumed by this parser rather than left in the free text, or
    #: `/repo leasha CustomerId /history` searches for the literal string
    #: "/repo leasha CustomerId".
    repo: str = ""

    # -- how -----------------------------------------------------------------
    #: "text" (fixed string) | "regex" | "word"
    method: str = "text"
    ignore_case: bool = False
    #: One of `SYMBOL_PATTERNS`, or "" for a plain content search.
    declaration: str = ""
    #: The name being looked for as a declaration. Held apart from `text`
    #: because both spellings must work - `/class OrderService` puts it on the
    #: switch, `OrderService /class` leaves it in the free text - and folding
    #: them together made the pattern the whole line, which then matched
    #: nothing and looked like a class that was not there.
    declaration_name: str = ""

    # -- which files ---------------------------------------------------------
    paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    exclude_files: tuple[str, ...] = ()

    # -- who and when --------------------------------------------------------
    author: str = ""
    committer: str = ""
    #: `/message`: words from the commit's own message, for `git log --grep`.
    #: Held apart from `text` because they search different things - `text`
    #: becomes a pickaxe over the diff, this becomes a match on the header -
    #: and a query may legitimately carry both.
    message: str = ""
    since: str = ""
    until: str = ""
    #: "any" | "only" | "none"
    merges: str = "any"

    # -- what happened -------------------------------------------------------
    #: "" | "introduced" | "removed" | "changed" | "lifecycle" | "file-history"
    lifecycle: str = ""
    #: Restrict the diff to added or deleted lines only.
    added_only: bool = False
    removed_only: bool = False
    #: Only commits that deleted a file, for "where did that file go".
    deleted_files_only: bool = False
    track_renames: bool = True
    #: How many commits to look back through. **Always set to something.** The
    #: whole point of the cost measurement was that unbounded history search is
    #: not affordable behind an interface; a caller may raise it deliberately.
    depth: int = 2000

    def wants_history(self) -> bool:
        """Whether this reaches past the checked-out tree.

        The expensive half, and the one the UI must run explicitly. Kept as a
        method so the answer is in one place: three call sites deciding for
        themselves is how one of them ends up deciding differently.
        """
        return bool(
            self.scope in ("history", "lifetime", "range")
            or self.lifecycle
            or self.added_only or self.removed_only or self.deleted_files_only
            or self.rev_range
            # **Asking who or when *is* asking about history.** These are
            # properties of a commit, and `git grep` has nowhere to put them -
            # so a query carrying them and planned as a grep would drop them
            # and answer a wider question than the one typed, without saying
            # so. That is the failure this whole module is careful about.
            or self.author or self.committer or self.since or self.until
            # A commit message is a property of a commit, so `git grep` has
            # nowhere to put it either - the same reason `/author` is here.
            or self.message
            or self.merges != "any"
        )

    def is_message_only(self) -> bool:
        """True when this asks about commit messages and nothing in a diff.

        **The one history search that is not expensive.** `--grep` reads the
        commit header; `-S` and `-G` diff every commit they walk, which is the
        1.59s-for-75-commits that made history a separate, explicit job. A
        message search has none of that cost and must not be billed as though
        it does, or it will sit behind a confirmation it does not need.
        """
        return bool(self.message) and not (
            self.text or self.declaration or self.lifecycle
            or self.added_only or self.removed_only or self.deleted_files_only
        )


@dataclass
class GitPlan:
    """The exact git invocation, and what reads its output."""

    kind: str
    argv: list[str] = field(default_factory=list)
    #: One sentence saying what was asked for, in the words a person used.
    #: Shown above the results, because a search whose scope is invisible is a
    #: result nobody can judge - "no matches" means nothing until you know
    #: whether it looked at one commit or nine thousand.
    explain: str = ""
    #: True when the plan reads history, so the caller knows to show progress
    #: and offer to cancel rather than blocking.
    slow: bool = False
    #: Keep only the first row. `/introduced` wants the oldest matching commit
    #: and cannot get it from git alone - see the comment in `_history_plan`.
    first_only: bool = False
    #: "+", "-" or "". Which side of a diff to keep when reading a patch.
    #:
    #: **git has no switch for this.** `--diff-filter` selects *files*, not
    #: lines, so "only the lines that were deleted" is done by the reader - and
    #: saying so here is what stops the caller assuming git already did it and
    #: showing added lines under a heading that says removed.
    line_filter: str = ""


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

def _c(name: str, aliases: tuple[str, ...], summary: str, example: str,
       hint: str, icon: str, values: tuple[str, ...] = (),
       source: str = "", scoped_by: tuple[str, ...] = ()) -> Command:
    """One switch. `source` is a key `gitsearch.repo_values` answers, so the
    menu can offer the branches, tags and authors this repository actually
    has - the same reason the index's menu reads the index."""
    return Command(name=name, aliases=aliases, summary=summary, example=example,
                   value_hint=hint, icon=icon, values=values, source=source,
                   scoped_by=scoped_by)


#: Every switch the repository search accepts, in the shape the `/` menu
#: already knows how to draw. Ordered as somebody would ask: where, how, which
#: files, who, when, and what happened to it.
GIT_COMMANDS: tuple[Command, ...] = (
    # -- where ---------------------------------------------------------------
    _c("repo", ("repository", "project"), "Which repository to search",
       "/repo leasha", "a repository name, as listed in the Code tab", "⌥",
       source="repo"),
    _c("branch", ("b",), "Search these branches instead of the checkout",
       "/branch develop", "a branch name, or several: develop,release", "⑂",
       source="branch",
       # The order's headline example: `repo:leasha branch:` offers leasha's
       # branches, not every branch in every checkout on the machine.
       scoped_by=("repo",)),
    _c("all-branches", ("allbranches",), "Search every local branch head",
       "/all-branches", "no value needed", "⑂", ("yes",)),
    _c("remote-branches", ("remote",), "Search every remote branch head",
       "/remote-branches", "no value needed", "⑂", ("yes",)),
    _c("history", ("hist",), "Search every commit, not just the current files",
       "/history", "no value needed - this is the slow one", "◷", ("yes",)),
    _c("commit", ("sha", "commits"), "Search one commit, or several",
       "/commit a1b2c3d", "a commit id, or several: a1b2c3d,b2c3d4e", "◆", source="commit", scoped_by=("repo",)),
    # **2026-09-27, order 0x §6a (decision D2): `between` is no longer a
    # spelling of `/range`.** It is `/date` under another name now, in every
    # box including this one. Left here, it would have done two wrong things
    # at once: `widgets/code_commands._merged` drops a git switch whose
    # spelling the index already claims, so `/range` itself would have
    # vanished from the Code and Search menus; and `presenter.code_route`
    # would have sent `/between 2024-03 and 2024-06` to git as a commit
    # range. `/range v5.0..v6.0` is unchanged.
    _c("range", (), "Search between two commits or tags",
       "/range v5.0..v6.0", "a..b - commits or tags, either way round", "↔"),
    _c("tag", ("release", "tags"), "Search one tag or release",
       "/tag v5.1", "a tag name, or several: v5.0,v6.0", "⌂", source="tag", scoped_by=("repo",)),
    _c("lifetime", ("everything",), "Every branch, every commit, every tag",
       "/lifetime", "no value needed - the widest and slowest search", "∞",
       ("yes",)),

    # -- how -----------------------------------------------------------------
    _c("regex", ("re",), "Read the search text as a regular expression",
       "/regex Order[A-Z]+", "no value needed", "⁂", ("yes",)),
    _c("word", ("whole-word", "wholeword"), "Match whole words only",
       "/word", "no value needed", "⁝", ("yes",)),
    _c("ignore-case", ("i", "nocase"), "Ignore capitals",
       "/ignore-case", "no value needed", "⇕", ("yes",)),
    _c("class", ("struct", "record"), "Find where a class is declared",
       "/class OrderService", "a class name", "◫"),
    _c("interface", ("protocol", "trait"), "Find where an interface is declared",
       "/interface IOrder", "an interface name", "◫"),
    _c("function", ("method", "func"), "Find where a function is declared",
       "/function calculateTotal", "a function or method name", "ƒ"),
    _c("symbol", ("identifier",), "Find a name used as a whole word",
       "/symbol CustomerId", "any identifier", "§"),
    _c("endpoint", ("api", "route"), "Find an API route or endpoint",
       "/endpoint /api/orders", "a route, with or without slashes", "⇢"),
    _c("config", ("setting",), "Find a configuration key being set",
       "/config ConnectionString", "a key name", "⚙"),

    # -- which files ---------------------------------------------------------
    _c("path", ("dir", "folder"), "Only inside this folder",
       "/path src/services", "a folder, or several: src,tests", "▸"),
    _c("exclude-path", ("not-path",), "Skip this folder",
       "/exclude-path node_modules", "a folder, or several", "⊘"),
    _c("extension", ("ext", "type"), "Only files of this type",
       "/extension cs", "cs, ts, sql - or several: cs,ts", "▤"),
    _c("file", ("files",), "Only these files",
       "/file OrderService.cs", "a name or pattern: *.cs", "▫"),
    _c("exclude-file", ("not-file",), "Skip these files",
       "/exclude-file *.generated.cs", "a name or pattern", "⊘"),

    # -- who and when --------------------------------------------------------
    _c("author", ("by",), "Only commits by this person",
       "/author dave", "part of a name or email address", "✎", source="author", scoped_by=("repo",)),
    _c("committer", (), "Only commits committed by this person",
       "/committer dave", "part of a name or email address", "✎", source="author", scoped_by=("repo",)),
    # **The commit message, which nothing could search.** `-S` and `-G` search
    # the *content* of a diff; `--grep` searches what the commit said about
    # itself, and they answer different questions - "when did this string
    # appear" against "which commit claimed to fix the licence bug". Only the
    # first had a switch. Raised by the owner: *"the code does not search
    # ability to search through the search commit messages"*, and it is
    # `GitSearch.txt` UC-020, of which only `/author` had been built.
    _c("message", ("msg", "subject"), "Only commits whose message says this",
       "/message licence", "any words from a commit message", "✉",
       source="commit", scoped_by=("repo",)),
    _c("since", ("after",), "Only commits on or after this date",
       "/since 2025-01-01", "2025-01-01, last month, 30 days ago", "◷"),
    _c("until", ("before",), "Only commits on or before this date",
       "/until 2025-12-31", "2025-12-31, yesterday", "◶"),
    _c("merges", (), "Include, exclude or show only merge commits",
       "/merges none", "any, only, none", "⑃", ("any", "only", "none")),

    # -- what happened -------------------------------------------------------
    _c("introduced", ("added-in",), "The commit where this first appeared",
       "/introduced", "no value needed", "⊕", ("yes",)),
    _c("removed", ("deleted-in",), "The commit where this was deleted",
       "/removed", "no value needed", "⊖", ("yes",)),
    _c("changed", ("evolution",), "Every commit that changed this",
       "/changed", "no value needed", "≈", ("yes",)),
    _c("lifecycle", ("story",), "Created, changed and removed, in order",
       "/lifecycle", "no value needed", "⟳", ("yes",)),
    _c("file-history", ("filehistory",), "What happened to a file, including renames",
       "/file-history src/Order.cs", "a file path", "⌸"),
    _c("added-only", ("added",), "Only lines that were added",
       "/added-only", "no value needed", "＋", ("yes",)),
    _c("removed-only", ("deleted",), "Only lines that were deleted",
       "/removed-only", "no value needed", "－", ("yes",)),
    _c("deleted-files", ("gone",), "Only commits that deleted a file",
       "/deleted-files", "no value needed", "␡", ("yes",)),
    _c("depth", ("limit",), "How many commits back to look",
       "/depth 5000", "a number; the default is 2000", "⇣",
       ("500", "2000", "10000", "50000")),
)

_BY_SPELLING = {
    spelling: command for command in GIT_COMMANDS
    for spelling in command.spellings
}

#: Switches that are on or off. Written `/history` with no value, so the parser
#: must not swallow the next word as one.
FLAGS = frozenset({
    "all-branches", "remote-branches", "history", "lifetime", "regex", "word",
    "ignore-case", "introduced", "removed", "changed", "lifecycle",
    "added-only", "removed-only", "deleted-files",
})


def git_command_for(name: str) -> Optional[Command]:
    """The switch for any accepted spelling, or None."""
    return _BY_SPELLING.get(str(name).strip().lower().lstrip("/").rstrip(":"))


def git_matching(prefix: str) -> list[Command]:
    """Switches whose name or alias starts with `prefix`. For the `/` menu."""
    wanted = str(prefix or "").strip().lower().lstrip("/")
    if not wanted:
        return list(GIT_COMMANDS)
    return [command for command in GIT_COMMANDS
            if any(spelling.startswith(wanted) for spelling in command.spellings)]


#: Switches only the repository engine can answer. **Deliberately not "every git
#: switch"**: `/repo`, `/type`, `/path` and `/file` mean the same thing in both
#: catalogues, and routing on those would send `type:cs` - the commonest thing
#: anybody types - into a git subprocess instead of a 3ms index lookup.
#:
#: Defined here rather than in the presenter, where it began, because two
#: callers now need it and neither should have to import a UI module to ask a
#: question about grammar. `app/ui/presenter.py` re-exports the name.
GIT_ONLY: frozenset[str] = frozenset({
    "history", "lifetime", "branch", "all-branches", "remote-branches",
    "commit", "range", "tag", "author", "committer", "message", "since", "until",
    "merges", "introduced", "removed", "changed", "lifecycle", "file-history",
    "added-only", "removed-only", "deleted-files", "depth", "regex", "word",
    "ignore-case", "class", "interface", "function", "symbol", "endpoint",
    "config",
})

#: `/name` or `name:` at a word boundary. **Named apart from `_TOKEN`**, which
#: already exists further down this module and is anchored to a whole word: the
#: first version of this reused that name, was silently shadowed by it, and
#: `wants_git` answered "no git switches here" for every line ever typed. A
#: feature that quietly never runs is the worst way for one to fail.
_SWITCH = re.compile(r"(?:(?<=\s)|^)(?:/([A-Za-z-]+)|([A-Za-z-]+):)")


def wants_git(raw: str) -> tuple[str, ...]:
    r"""The git-only switches in this line, or `()` if there are none.

    **Pure, and that is the requirement.** This is what decides whether a search
    is worth a subprocess, so it is asked on every full-tier search - including
    ones that will never touch git. It resolves aliases through
    `git_command_for` and touches nothing else: no parse, no plan, no `git`.

    Returns the names rather than a bool so the caller can say *why* it is about
    to be slow, which is the difference between a search that seems to have hung
    and one that told you it was reading history.
    """
    from app.search.commands import COMMANDS

    # **A spelling the index catalogue claims is never a git switch here.**
    #
    # `after` and `before` are aliases of git's `/since` and `/until`, so
    # `after:2024` - an ordinary date filter, one of the commonest things
    # anybody types - resolved to a repository switch and would have forked a
    # subprocess on every search carrying a date. The same precedence rule
    # `widgets/code_commands._merged` applies to the menu applies here to the
    # routing, and for the same reason: the cheap path must stay cheap, and a
    # word that means the same thing to both engines belongs to the fast one.
    claimed = {spelling for command in COMMANDS for spelling in command.spellings}

    found: list[str] = []
    for slash, colon in _SWITCH.findall(str(raw or "")):
        spelling = (slash or colon).lower()
        if spelling in claimed:
            continue
        command = git_command_for(spelling)
        if command is not None and command.name in GIT_ONLY:
            found.append(command.name)
    return tuple(sorted(set(found)))


# ---------------------------------------------------------------------------
# Reading what somebody typed
# ---------------------------------------------------------------------------

#: `/name`, `/name=value` or `name:value`.
#:
#: **The value may not start with `/`, and that is not a detail.** `api` is an
#: alias of `endpoint`, so without it `/api/orders` - which is a route somebody
#: is plainly searching for - parsed as the `/api` switch carrying `/orders`,
#: and the search silently became something else. A path is the single most
#: likely thing to be typed here after a word, and this grammar lives on the
#: Code tab. So a switch ends at the token, at an `=`, or at a space; anything
#: with another slash in it is text.
#:
#: Anchored at both ends, which is what does the work: `[\w-]*` cannot match a
#: slash, so `/api/orders` never matches the switch form at all and falls
#: through to text. An explicit `/endpoint=/api/orders` is unambiguous and is
#: read as the switch, because the `=` says so.
_TOKEN = re.compile(r"^(?:/([A-Za-z][\w-]*)(?:=(.*))?|([A-Za-z][\w-]*):(.*))$")


def _split(value: str) -> tuple[str, ...]:
    """`a,b , c` as three values. Commas, because that is what the
    specification uses and what the rest of this grammar already accepts."""
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def parse_git_query(text: str, *, depth: int = 2000) -> GitQuery:
    """Read a line of `/switches` and free text into a `GitQuery`.

    **Unknown `/words` are left as search text, not rejected.** Somebody
    searching for `/api/orders` is searching for a path, and a grammar that
    swallowed it would find nothing and say nothing. The same rule the index's
    parser follows, for the same reason: a search box that silently rewrites
    what was typed is one people stop trusting.

    Quoting works: `/author "John Smith"` is one author.
    """
    try:
        tokens = shlex.split(str(text or ""), posix=True)
    except ValueError:
        # An unbalanced quote is a half-typed query, not an error.
        tokens = str(text or "").split()

    words: list[str] = []
    seen: dict[str, Any] = {
        "branches": [], "commits": [], "tags": [], "paths": [],
        "exclude_paths": [], "extensions": [], "files": [], "exclude_files": [],
    }
    flags: set[str] = set()
    single: dict[str, str] = {}

    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        match = _TOKEN.match(token)
        if match is None:
            words.append(token)
            continue

        name = (match.group(1) or match.group(3) or "").lower()
        inline = match.group(2) if match.group(2) is not None else (match.group(4) or "")
        command = git_command_for(name)
        if command is None:
            words.append(token)              # not ours: it is search text
            continue

        key = command.name
        if key in FLAGS:
            flags.add(key)
            if inline:
                words.append(inline)
            continue

        value = inline
        if not value and index < len(tokens) and not _TOKEN.match(tokens[index]):
            value = tokens[index]
            index += 1
        if not value:
            # **Present but valueless is not the same as absent.** `/class`
            # after the name - `OrderService /class` - is a perfectly natural
            # way to type it, and dropping the switch made it a plain text
            # search that quietly answered a different question.
            flags.add(key)
            continue

        if key == "branch":
            seen["branches"] += _split(value)
        elif key == "commit":
            seen["commits"] += _split(value)
        elif key == "tag":
            seen["tags"] += _split(value)
        elif key == "path":
            seen["paths"] += _split(value)
        elif key == "exclude-path":
            seen["exclude_paths"] += _split(value)
        elif key == "extension":
            seen["extensions"] += tuple(v.lstrip(".").lower()
                                        for v in _split(value))
        elif key == "file":
            seen["files"] += _split(value)
        elif key == "exclude-file":
            seen["exclude_files"] += _split(value)
        else:
            single[key] = value

    return _assemble(words, seen, flags, single, depth)


def _assemble(words, seen, flags, single, depth) -> GitQuery:
    """Turn the raw pieces into the query. Split out because the *precedence*
    between overlapping switches is a decision, and a decision in the middle of
    a parsing loop is a decision nobody can find."""
    declaration = ""
    declaration_name = ""
    for kind in ("class", "interface", "function", "symbol", "endpoint",
                 "config"):
        if kind in single or kind in flags:
            declaration = kind
            # Both spellings work: the name on the switch, or in the free text
            # with a bare switch. Requiring one of them would make the grammar
            # feel like a form, and guessing wrong makes the pattern the whole
            # line - which matches nothing and reads as an absent class.
            declaration_name = single.get(kind) or " ".join(words)
            break

    scope = "files"
    rev_range = single.get("range", "")
    if "lifetime" in flags:
        scope = "lifetime"
    elif "history" in flags:
        scope = "history"
    elif rev_range:
        scope = "range"
    elif seen["commits"]:
        scope = "commit"
    elif seen["tags"]:
        scope = "tag"
    elif "all-branches" in flags:
        scope = "all-branches"
    elif "remote-branches" in flags:
        scope = "remote-branches"
    elif seen["branches"]:
        scope = "branch"

    lifecycle = ""
    for kind in ("lifecycle", "introduced", "removed", "changed"):
        if kind in flags:
            lifecycle = kind
            break
    if "file-history" in single:
        lifecycle = "file-history"

    method = "text"
    if "regex" in flags:
        method = "regex"
    elif "word" in flags:
        method = "word"

    merges = (single.get("merges", "any") or "any").strip().lower()
    if merges not in ("any", "only", "none"):
        merges = "any"

    files = tuple(seen["files"])
    if lifecycle == "file-history" and single.get("file-history"):
        files = (single["file-history"], *files)

    try:
        wanted_depth = int(single.get("depth", depth))
    except (TypeError, ValueError):
        wanted_depth = depth

    return GitQuery(
        text=" ".join(words).strip(),
        scope=scope,
        branches=tuple(seen["branches"]),
        commits=tuple(seen["commits"]),
        tags=tuple(seen["tags"]),
        rev_range=rev_range,
        repo=single.get("repo", ""),
        method=method,
        ignore_case="ignore-case" in flags,
        declaration=declaration,
        declaration_name=declaration_name.strip(),
        paths=tuple(seen["paths"]),
        exclude_paths=tuple(seen["exclude_paths"]),
        extensions=tuple(seen["extensions"]),
        files=files,
        exclude_files=tuple(seen["exclude_files"]),
        author=single.get("author", ""),
        committer=single.get("committer", ""),
        message=single.get("message", ""),
        since=single.get("since", ""),
        until=single.get("until", ""),
        merges=merges,
        lifecycle=lifecycle,
        added_only="added-only" in flags,
        removed_only="removed-only" in flags,
        deleted_files_only="deleted-files" in flags,
        depth=max(1, wanted_depth),
    )


# ---------------------------------------------------------------------------
# Turning it into a command
# ---------------------------------------------------------------------------

def _pattern(query: GitQuery) -> str:
    """The text git is asked to match, after any declaration switch."""
    if query.declaration:
        template = SYMBOL_PATTERNS[query.declaration]
        return template.format(name=re.escape(query.declaration_name))
    return query.text


def _pathspecs(query: GitQuery) -> list[str]:
    """The `-- <pathspec>` tail: what to look at and what to skip.

    Passed as separate arguments and never through a shell, so a folder with a
    space in it needs no quoting and cannot be split.
    """
    specs: list[str] = []
    specs += [f"{path.rstrip('/')}/**" if "*" not in path else path
              for path in query.paths]
    specs += [f"*.{ext}" for ext in query.extensions]
    specs += [name if ("*" in name or "/" in name) else f"**/{name}"
              for name in query.files]
    # Exclusions come last and are magic pathspecs; git applies them whatever
    # the order, but reading them last is how somebody expects to see them.
    specs += [f":(exclude){path.rstrip('/')}/**" for path in query.exclude_paths]
    specs += [f":(exclude){name}" if ("*" in name or "/" in name)
              else f":(exclude)**/{name}" for name in query.exclude_files]
    return (["--", *specs] if specs else [])


def _revisions(query: GitQuery) -> list[str]:
    """Which revisions the search covers, in git's own words."""
    if query.scope == "commit":
        return list(query.commits)
    if query.scope == "tag":
        return list(query.tags)
    if query.scope == "range":
        return [query.rev_range]
    if query.scope == "branch":
        return list(query.branches)
    if query.scope == "all-branches":
        return ["--branches"]
    if query.scope == "remote-branches":
        return ["--remotes"]
    if query.scope == "lifetime":
        return ["--all"]
    return []


def _explain(query: GitQuery) -> str:
    """What was searched, in a sentence, for the line above the results.

    **Not decoration.** "No matches" means nothing until you know whether it
    looked at one commit or nine thousand, and the commonest way a repository
    search misleads is by answering a narrower question than the one asked.
    """
    # **A history plan whose scope is "files" is still history.** `/lifecycle`
    # and `/file-history` reach into the past without naming a revision, and a
    # line saying "the current checkout" above nine thousand commits of results
    # is the exact kind of wrong that nobody double-checks.
    scope = query.scope
    if scope == "files" and query.wants_history():
        scope = "history"

    where = {
        "files": "the current checkout",
        "branch": "branch " + ", ".join(query.branches),
        "all-branches": "every local branch",
        "remote-branches": "every remote branch",
        "history": f"the last {query.depth:,} commits",
        "commit": "commit " + ", ".join(query.commits),
        "range": query.rev_range,
        "tag": "tag " + ", ".join(query.tags),
        "lifetime": f"every branch and tag, {query.depth:,} commits deep",
    }.get(scope, scope)

    asked = {
        "introduced": "the commit that first added it",
        "removed": "the commits that deleted it",
        "changed": "every commit that touched it",
        "lifecycle": "everything that happened to it",
        "file-history": "what happened to the file",
    }.get(query.lifecycle, "")

    parts = [f"{asked}, in {where}"] if asked else [f"in {where}"]
    if query.declaration:
        parts.append(f"for {query.declaration_name or '(nothing)'} "
                     f"as a {query.declaration} declaration")
    elif query.method == "regex":
        parts.append("as a regular expression")
    elif query.method == "word":
        parts.append("whole words only")
    if query.message:
        # **Said before the author and the dates**, because it is the strongest
        # narrowing in the line and because a message search reads nothing in a
        # diff - somebody judging "no matches" needs to know which of the two
        # questions was asked.
        parts.append(f'whose message mentions "{query.message}"')
    if query.author:
        parts.append(f"by {query.author}")
    if query.since or query.until:
        parts.append(f"between {query.since or 'the beginning'} "
                     f"and {query.until or 'now'}")
    if query.extensions:
        parts.append("in ." + ", .".join(query.extensions))
    if query.added_only:
        parts.append("added lines only")
    if query.removed_only:
        parts.append("deleted lines only")
    if query.deleted_files_only:
        parts.append("commits that deleted a file")
    return ", ".join(parts)


def build(query: GitQuery) -> GitPlan:
    """The exact `git` argument list that answers this query.

    Returned rather than run, so every switch in the specification can be
    checked by asserting on the command - without git, without a repository,
    and without waiting.
    """
    if query.wants_history():
        return _history_plan(query)
    return _grep_plan(query)


def _grep_plan(query: GitQuery) -> GitPlan:
    """`git grep` over one or more revisions: what the code says *now*."""
    argv = ["git", "grep", "-I", "-n", "--no-color"]
    if query.ignore_case:
        argv.append("-i")
    if query.declaration or query.method == "regex":
        argv.append("--extended-regexp")
    else:
        argv.append("--fixed-strings")
    if query.method == "word":
        argv.append("--word-regexp")

    argv += ["-e", _pattern(query)]
    revisions = _revisions(query)
    argv += revisions or []
    argv += _pathspecs(query)
    return GitPlan(kind=KIND_GREP, argv=argv, explain=_explain(query),
                   slow=False)


def _history_plan(query: GitQuery) -> GitPlan:
    """`git log`: which commits touched this, and optionally the lines.

    **The expensive one.** `-S` makes git diff every commit it walks, so the
    cost is history times changed files - 1.59s for 75 commits on this project.
    `--max-count` is always present for that reason.
    """
    argv = ["git", "log", "--no-color", f"--max-count={query.depth}",
            "--date=short",
            # A stable, parseable header per commit. Tab-separated because a
            # commit subject can contain anything except a newline, and a
            # separator that can appear in the data is a parser that is wrong
            # on somebody's repository and right on yours.
            #
            # **`tformat`, not `format`** (order 0y section 3a). `format` puts the
            # newline *between* commits, so a row's line is only complete when
            # git finds the next one - the last row of a search arrived when git
            # ended (measured: at 4.2 s of 4.2 s, against 1.1 s of 4.5 s with
            # `tformat`, which ends every row with its own newline).
            "--pretty=tformat:%H\t%ad\t%an\t%s"]

    if query.track_renames:
        argv.append("--find-renames")
    if query.merges == "only":
        argv.append("--merges")
    elif query.merges == "none":
        argv.append("--no-merges")
    if query.author:
        argv.append(f"--author={query.author}")
    if query.committer:
        argv.append(f"--committer={query.committer}")
    if query.message:
        # **`--grep` is the message; `-S` below is the diff.** Both can be
        # present, and then git ANDs them, which is the useful reading of
        # `/message licence CustomerId`: the commit said "licence" *and*
        # touched "CustomerId".
        argv.append(f"--grep={query.message}")
    if query.since:
        argv.append(f"--since={query.since}")
    if query.until:
        argv.append(f"--until={query.until}")
    if query.ignore_case:
        argv.append("--regexp-ignore-case")

    kind = KIND_LOG
    if query.lifecycle == "file-history":
        # A file's life, not its contents: renames followed, one line per
        # change. `--follow` needs exactly one pathspec, which is why the file
        # goes in as the only one.
        argv += ["--follow", "--name-status"]
        kind = KIND_NAME_STATUS
    elif query.text:
        # `-G` is "the diff itself matches this regex"; `-S` is "the number of
        # occurrences changed". `-S` is the right default - it answers "when did
        # this appear or disappear" rather than "when was a line mentioning it
        # touched" - and `/changed` is exactly the request for the other one.
        flag = "-G" if (query.lifecycle == "changed" or query.method == "regex"
                        or query.declaration) else "-S"
        argv.append(f"{flag}{_pattern(query)}")

    if query.deleted_files_only:
        argv.append("--diff-filter=D")
    elif query.lifecycle == "introduced":
        # The oldest matching commit is the one that introduced it, so the walk
        # is reversed and the first row kept.
        #
        # **`--max-count=1 --reverse` returns nothing at all.** Measured, not
        # assumed: `git log -Snotice_line --reverse --max-count=1` on this
        # repository prints no output, while the same command without the count
        # prints the commit that introduced it. git applies the limit during
        # the traversal and reverses afterwards, and with the pickaxe the two
        # do not compose. Keeping one row is therefore this application's job -
        # which is what `first_only` says - and shipping the obvious spelling
        # would have been a switch that silently found nothing.
        argv.append("--reverse")
    elif query.lifecycle == "removed":
        argv.append("--diff-filter=D")

    if query.added_only or query.removed_only:
        # The lines themselves, so "which line was deleted" has an answer. This
        # is what makes a history search useful rather than a list of commit
        # ids, and it is also what makes it slower - the diff has to be
        # generated, not merely walked.
        argv += ["--patch", "--unified=0"]
        kind = KIND_PATCH

    argv += _revisions(query)
    argv += _pathspecs(query)
    return GitPlan(
        kind=kind, argv=argv, explain=_explain(query),
        # See `is_message_only`: a header scan is not a diff walk, and marking
        # it slow would put a confirmation in front of a search that does not
        # need one.
        slow=not query.is_message_only(),
        first_only=query.lifecycle == "introduced",
        line_filter="+" if query.added_only else ("-" if query.removed_only else ""),
    )
