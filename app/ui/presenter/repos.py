"""Repositories as rows, and the Code tab's repository filter.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.ui.presenter.facts import date_words
from app.ui.presenter.formatting import format_count, shorten_path

# ---------------------------------------------------------------------------
# Repositories, for the Code tab
# ---------------------------------------------------------------------------

#: The backend's `repos.kind` values, in words somebody reads. Modelled on
#: `kind_tag`: the store keeps a short token, the person sees a noun.
REPO_KINDS = {
    "work": "Repository",
    "submodule": "Submodule",
    "worktree": "Worktree",
}


@dataclass(frozen=True, slots=True)
class RepoRow:
    """One repository, formatted for the Code table."""

    name: str
    files: str          # "48,301"
    kind: str           # "Repository" | "Submodule" | "Worktree"
    seen: str           # "2 hours ago"
    path: str           # shortened, for the column
    root: str           # the full path, for the menu and the tooltip
    #: The unformatted originals, so the table can sort on real values rather
    #: than on what it displays - "9" must sort below "10", and a date must
    #: sort chronologically rather than alphabetically. `SortableItem` reads
    #: these; see `widgets/sortable_item.py` for what happens without them.
    file_count: int = 0
    seen_at: int = 0
    #: The store's row id, so a repository's files can be fetched by the column
    #: that is indexed rather than by matching path strings.
    repo_id: int = 0


def repo_rows(
    rows: Iterable[Mapping[str, Any]], *, now: Optional[float] = None
) -> list[RepoRow]:
    """Store rows to display rows for the Code list.

    `now` is a parameter for the same reason `format_when` takes one: a
    relative time is untestable otherwise.
    """
    out: list[RepoRow] = []
    for row in rows:
        root = str(row.get("root_path", ""))
        # The store already orders by file count; the name falls back to the
        # folder so a repository with no recorded name is still identifiable.
        name = str(row.get("name", "")) or root.replace("\\", "/").rstrip("/").rpartition("/")[2]
        seen_at = _as_ns(row.get("last_seen"))
        count = int(row.get("files", 0) or 0)
        out.append(RepoRow(
            name=name,
            files=format_count(count),
            kind=REPO_KINDS.get(str(row.get("kind", "")), "Repository"),
            # 2026-10-04: the date the Search tab's way, as every list.
            seen=date_words(seen_at, now=now),
            path=shorten_path(root, limit=60),
            root=root,
            file_count=count,
            seen_at=seen_at,
            repo_id=int(row.get("id", 0) or 0),
        ))
    return out


def _as_ns(value: Any) -> int:
    """`last_seen` as nanoseconds, whatever shape the store gave it.

    Accepts an ISO string, seconds, or nanoseconds, because a column read
    through `dict(row)` arrives as whatever SQLite stored and a preview panel
    is not the place to discover a type mismatch.
    """
    if not value:
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        # Anything this small is seconds; nanoseconds since 1970 are ~1e18.
        return number * 1_000_000_000 if number < 1e12 else number

    # Imported here, as the other date helper in this module does: `presenter`
    # is deliberately import-light because every view depends on it.
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0
    return int(moment.timestamp() * 1e9)


def repo_summary(repos: int, files: int) -> str:
    """The line above the table. Says nothing clever when there is nothing."""
    if not repos:
        return ""
    return (
        f"{format_count(repos)} repositor{'y' if repos == 1 else 'ies'}, "
        f"{format_count(files)} file{'s' if files != 1 else ''} indexed"
    )


def repo_empty_state(anything_indexed: bool) -> str:
    """What to say when the Code tab has no repositories to show.

    **Two different questions, two different answers.** A generic "no results"
    would waste the one that matters: somebody with an indexed corpus and no
    repositories needs to be told what a repository is here - a folder holding
    `.git` - and that adding its parent as an indexed root is what makes it
    appear. Somebody with nothing indexed at all needs a route to the Indexing
    tab instead, and being told about `.git` would be noise.
    """
    if anything_indexed:
        return (
            "No code repositories found under your indexed folders.\n\n"
            "A repository is any folder containing a .git folder. If one is "
            "missing from this list, its folder is not inside an indexed root - "
            "add the folder above it in Settings, then index again."
        )
    return (
        "Nothing is indexed yet, so there is nothing to show here.\n\n"
        '<a href="#index">Go to the Indexing tab</a> to add a folder and start.'
    )


def repo_tree_summary(matching: Optional[set], total: int) -> str:
    """`2 of 4 repositories match`, or `""` when nothing was asked.

    Said out loud because a tree that has silently shrunk is the same fault as
    one that silently shows everything - the person cannot tell whether three
    repositories are missing or were never there.
    """
    if matching is None or total <= 0:
        return ""
    count = len(matching)
    if count == total:
        return f"All {total} repositories match"
    if count == 0:
        return f"No repositories match — {total} indexed"
    return f"{count} of {total} repositories match"


@dataclass(frozen=True, slots=True)
class RepoFilter:
    """What the Code tab's box asked for, once the grammar has read it."""

    #: `repo:` values, lower-cased. Empty means "any repository".
    names: tuple[str, ...] = ()
    #: Free text, matched against what is on screen.
    text: str = ""
    #: `type:` values, lower-cased and without dots. Honoured because the tree
    #: lists files: before it did, this was in `ignored`, and offering `/type`
    #: on a list with no file in it would have been an offer nothing kept.
    exts: tuple[str, ...] = ()
    #: Operators this list cannot answer, named so the tab can say so rather
    #: than filtering to nothing and looking broken.
    ignored: tuple[str, ...] = ()

    def matches(self, name: str, haystack: str) -> bool:
        """A repository row. `exts` is deliberately not applied here.

        Which extensions a repository contains is not known until its files are
        loaded, and hiding it on a guess would remove the row somebody was about
        to expand. `type:` narrows the children instead, and the summary says
        so - see `matches_file`.
        """
        if self.names and name.lower() not in self.names:
            return False
        return not self.text or self.text in haystack

    def matches_file(self, ext: str, haystack: str) -> bool:
        """A file row under a repository that has already passed `matches`."""
        if self.exts and (ext or "").lower().lstrip(".") not in self.exts:
            return False
        return not self.text or self.text in haystack


def repo_visibility(
    chosen: "RepoFilter",
    repo_name: str,
    repo_haystack: str,
    files: Sequence[tuple[str, str]] = (),
) -> tuple[bool, list[bool]]:
    """Which repository row and which of its file rows survive the filter.

    Returns `(show the repository, one flag per file)`. Pure, so the rule can be
    argued with in a test rather than inferred from a tree on screen.

    Three decisions are worth stating, because each has an obvious-looking
    alternative that reads worse:

    * **`repo:` hides the whole subtree.** Naming a repository is a statement
      about which repository you want, so the others go entirely.
    * **A repository whose name does not match stays if one of its files does.**
      Typing "readme" should find the README, and hiding its parent would hide
      the answer. This is what makes the tree searchable rather than merely
      collapsible.
    * **A repository that matches shows all its files.** Having found `leasha`
      by typing "leasha", being shown only the files with "leasha" in the name
      is a second filter nobody asked for.
    """
    if chosen.names and repo_name.lower() not in chosen.names:
        return False, [False] * len(files)

    matched_name = not chosen.text or chosen.text in repo_haystack
    if matched_name:
        # Only `type:` narrows the children now; the text has done its work.
        flags = [not chosen.exts or (ext or "").lower().lstrip(".") in chosen.exts
                 for ext, _haystack in files]
        return True, flags

    flags = [chosen.matches_file(ext, haystack) for ext, haystack in files]
    return any(flags), flags


def repo_filter(text: str) -> RepoFilter:
    """Read the Code tab's filter box through the one grammar.

    **The same parser as everywhere else**, so `/repo leasha` means here what it
    means in the search box, and nothing had to be invented for this tab.

    A repository list cannot answer `from:` or `after:`, so those are reported
    rather than silently dropped: a filter that quietly ignores half of what was
    typed produces an empty list and no explanation, which reads as the tab
    being broken.
    """
    from app.search.commands import expand_slashes
    from app.search.query import parse_query

    parsed = parse_query(expand_slashes(text or ""))

    ignored = tuple(sorted({
        name for name, value in (
            ("from", parsed.senders), ("to", parsed.recipients),
            ("subject", parsed.subjects),
            ("path", parsed.paths), ("after", parsed.after),
            ("before", parsed.before), ("size", parsed.sizes),
            ("has", parsed.has_attachment),
        ) if value
    }))
    # `name:` is free text here. The tree matches on the name column already,
    # so `/name utils` and typing `utils` should not behave differently.
    words = " ".join((*parsed.terms, *parsed.names)).strip() or (parsed.text or "")
    return RepoFilter(
        names=tuple(name.lower() for name in parsed.repos),
        text=words.strip().lower(),
        exts=tuple(str(e).lower().lstrip(".") for e in parsed.ext),
        ignored=ignored,
    )


def repo_filter_summary(
    chosen: "RepoFilter", *, shown: int, total: int, files: int
) -> str:
    """The line above the Code table, whatever the filter is doing.

    **An ignored operator is named, not swallowed.** `from:dave` cannot mean
    anything to a list of repositories; filtering to nothing and saying "no
    matches" would send somebody looking for a repository that is right there.
    Mail says the same thing about free text for the same reason.
    """
    note = ""
    if chosen.ignored:
        spelled = ", ".join(f"{name}:" for name in chosen.ignored)
        note = (
            f"  ·  {spelled} ignored - this list has repositories and their "
            "files. Press Enter on a repository to search inside it."
        )
    if chosen.exts:
        # Said plainly, because a repository stays visible under `type:` even
        # when none of its files match - the tree cannot know until it is
        # expanded. Without this line an empty repository would look like a bug.
        kinds = ", ".join(f".{ext}" for ext in chosen.exts)
        note += f"  ·  expanding a repository shows its {kinds} files only"

    if not chosen.names and not chosen.text:
        return repo_summary(total, files) + note
    if not shown:
        return f"No repository matches that.{note}"
    return f"{format_count(shown)} of {format_count(total)} repositories{note}"
