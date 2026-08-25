r"""The Code box's `/` menu: both catalogues, one list.

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
"""

from __future__ import annotations

from typing import Optional

from app.search.commands import COMMANDS, Command
from app.search.gitquery import GIT_COMMANDS
from app.ui.presenter import CODE_COMMANDS

__all__ = [
    "CODE_CATALOGUE",
    "code_command_for",
    "code_matching",
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


#: Every switch this box accepts, in the order the menu shows them.
CODE_CATALOGUE: tuple[Command, ...] = _merged()

_BY_SPELLING = {
    spelling: command for command in CODE_CATALOGUE
    for spelling in command.spellings
}


def code_command_for(name: str) -> Optional[Command]:
    """The switch for any accepted spelling, or None."""
    return _BY_SPELLING.get(str(name).strip().lower().lstrip("/").rstrip(":"))


def code_matching(prefix: str) -> list[Command]:
    """Switches whose name or alias starts with `prefix`. For the `/` menu."""
    wanted = str(prefix or "").strip().lower().lstrip("/")
    if not wanted:
        return list(CODE_CATALOGUE)
    return [command for command in CODE_CATALOGUE
            if any(spelling.startswith(wanted) for spelling in command.spellings)]
