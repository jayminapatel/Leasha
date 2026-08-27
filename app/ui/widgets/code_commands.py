r"""Both catalogues, one list - for the Code box and for the search box.

Layer: L5

The Code tab is one search box over two engines - the index for files, git for
history - so its menu has to offer both sets of switches. It cannot simply
concatenate them: `/repo`, `/type`, `/path` and `/file` exist in **both**
catalogues, meaning the same thing to a person and resolving through different
parsers, and a list showing each of them twice is a list that looks broken.

**The index catalogue wins every shared spelling.** Two reasons, and the second
decides it:

*They are the same question.* `/type cs` means "only C# files" whichever engine
answers, and `parse_git_query` accepts `type` as an alias of `extension`. So one
entry serves both, and the router in `presenter.code_route` decides who reads
it.

*Routing must never go wrong towards git.* `/type` is the commonest thing
anybody types here. If it resolved to the git catalogue the router would treat
it as a git-only switch and send a 3ms index lookup into a subprocess that diffs
every commit. Preferring the index entry is what keeps the cheap path cheap.

The result is a menu where the first eleven rows are the ones used constantly
and the rest are the history switches, each with the icon its own catalogue gave
it - so the two halves are told apart by shape rather than by a heading.

**The search box now uses the same list**, which is why this module is no longer
named for one tab. It was written for Code because Code was the only box that
could reach both engines; the main search reaches both now too - the index
directly, and git through `app/search/federate.py` - so *"Search should have all
switches"* is answered by the list that already existed rather than by a second
one built beside it. `CODE_CATALOGUE` remains as an alias, because that is the
name the Code tab imports and renaming it would be churn for nothing.
"""

from __future__ import annotations

from typing import Any, Optional

from app.search.commands import ACTIONS, COMMANDS, Command
from app.search.gitquery import GIT_COMMANDS
from app.ui.presenter import CODE_COMMANDS

__all__ = [
    "git_values",
    "ALL_CATALOGUE",
    "catalogue_command_for",
    "catalogue_matching",
    "CODE_CATALOGUE",
    "code_command_for",
    "code_matching",
    "SEARCH_CATALOGUE",
    "search_command_for",
    "search_matching",
]


def _merged() -> tuple[Command, ...]:
    """The index's code switches first, then every git switch it does not
    already claim - by *any* spelling, not just its name, or `/ext` would
    resolve to one catalogue and `/type` to the other for the same word."""
    index = [command for command in COMMANDS if command.name in CODE_COMMANDS]
    taken = {spelling for command in index for spelling in command.spellings}

    out = list(index)
    for command in GIT_COMMANDS:
        if any(spelling in taken for spelling in command.spellings):
            continue
        out.append(command)
        taken |= set(command.spellings)
    return tuple(out)


#: Every switch there is, in the order a menu should show them: the eleven used
#: constantly, then the history ones.
ALL_CATALOGUE: tuple[Command, ...] = _merged()

#: The name the Code tab imports. Same list.
CODE_CATALOGUE: tuple[Command, ...] = ALL_CATALOGUE

_BY_SPELLING = {
    spelling: command for command in ALL_CATALOGUE
    for spelling in command.spellings
}


def catalogue_command_for(name: str) -> Optional[Command]:
    """The switch for any accepted spelling, or None."""
    return _BY_SPELLING.get(str(name).strip().lower().lstrip("/").rstrip(":"))


#: The Code tab's spelling of the same function.
code_command_for = catalogue_command_for


def catalogue_matching(prefix: str) -> list[Command]:
    """Switches whose name or alias starts with `prefix`. For the `/` menu."""
    wanted = str(prefix or "").strip().lower().lstrip("/")
    if not wanted:
        return list(ALL_CATALOGUE)
    return [command for command in ALL_CATALOGUE
            if any(spelling.startswith(wanted) for spelling in command.spellings)]


def git_values(repos: Any, text: str, kind: str, prefix: str, limit: int) -> list:
    """Branches, tags and authors, from whichever repository the box names.

    **The `/` menu's other half.** The index answers `/repo` and `/type`
    through the store; only git can answer these, and only for one repository
    at a time - so the value in `/repo` decides which. With none named and
    exactly one repository known, that is the one meant; with several it is a
    question nobody has answered, and offering the first would be a guess
    presented as a fact.

    Here rather than in `code_view.py`, which is at the 250-line guard, and
    because it belongs beside the catalogue it fills. Qt-free, so the rule
    about *which* repository is testable without a window.
    """
    from app.search.gitsearch import repo_values
    from app.ui.presenter import code_route, repo_root_for

    root = repo_root_for(repos, code_route(text).repo)
    if not root:
        return []
    return repo_values(root, kind, prefix=prefix, limit=limit)


#: The Code tab's spelling of the same function.
code_matching = catalogue_matching


# ---------------------------------------------------------------------------
# Adoptions §3 — the search box offers one row the Code box does not
# ---------------------------------------------------------------------------

#: `ALL_CATALOGUE`, plus the actions. **The search box only.**
#:
#: A saved search carries a scope and re-runs through the main engine, and the
#: Code box has neither - so offering `/saved` there would be a menu row that
#: quietly does nothing, which is the exact failure the note at the top of
#: `command_popup.py` exists to prevent and the reason `_only` exists at all.
#:
#: Kept here rather than in `commands.py` because this is the *merged* list -
#: index switches, git switches and now actions - and there is one place that
#: merges.
SEARCH_CATALOGUE: tuple[Command, ...] = ALL_CATALOGUE + ACTIONS

_SEARCH_BY_SPELLING = dict(_BY_SPELLING)
_SEARCH_BY_SPELLING.update(
    {spelling: command for command in ACTIONS
     for spelling in command.spellings})


def search_command_for(name: str) -> Optional[Command]:
    """`catalogue_command_for`, and the actions as well."""
    return _SEARCH_BY_SPELLING.get(
        str(name).strip().lower().lstrip("/").rstrip(":"))


def search_matching(prefix: str) -> list[Command]:
    """`catalogue_matching`, over the list that includes the actions."""
    wanted = str(prefix or "").strip().lower().lstrip("/")
    if not wanted:
        return list(SEARCH_CATALOGUE)
    return [command for command in SEARCH_CATALOGUE
            if any(spelling.startswith(wanted) for spelling in command.spellings)]
