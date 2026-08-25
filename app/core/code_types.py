r"""Which file types count as *code*, for the Code tab.

Layer: L0

**This decides what is shown, never what is read.** Two questions are easy to
conflate and must not be:

| Question | Decided by | Cost of getting it wrong |
|---|---|---|
| Do we read this file at all? | the file-types editor, `extractors.toml` | the file becomes unsearchable |
| Is this file *code*? | here | it is missing from one list, and still fully searchable |

Unticking `.pdf` in the file-types editor stops PDFs being indexed. Unticking
`Documentation and data` here removes `.md` from the Code tab and changes
nothing else - the main search still finds every one of them. That asymmetry is
the whole design, and it is why this is a view preference in `index_state`
rather than a setting in `.env`.

**Why it was needed.** `repo_files` selects `WHERE repo_id = ? AND source_kind =
'file'` - there is no type condition at all - so the Code tab listed everything
*located* in a repository: the PDFs, spreadsheets, logs and images that happen
to live in the folder. `app.cli repos` has warned about this since repositories
were added ("`scope:code` will match your whole corpus rather than just code");
it was reported from the window as *"its bringing files which are not code"*.

**A preset, with the groups behind it.** One click covers what nearly everybody
means, and the sixteen ecosystem groups in `source_types.py` are there for
anybody who means something else - the same shape as the three-way OCR choice,
and for the same reason: two checkboxes would have a fourth state that means
nothing.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from app.extract.source_types import BY_ECOSYSTEM, NAMED_FILES

__all__ = [
    "PRESETS",
    "PRESET_LABELS",
    "DEFAULT_PRESET",
    "SOURCE_GROUPS",
    "BUILD_GROUPS",
    "DOC_GROUPS",
    "STATE_KEY",
    "group_names",
    "groups_for",
    "extensions_for",
    "parse_choice",
    "dump_choice",
    "describe",
]

#: Where the choice lives. A view preference, beside the column widths and the
#: density - not `.env`, which is configuration somebody maintains.
STATE_KEY = "ui:code_types"

#: The groups that hold a programming language. Everything somebody would call
#: "the source".
SOURCE_GROUPS: tuple[str, ...] = (
    "Microsoft .NET and C++",
    "PowerShell and Windows scripting",
    "SQL Server and Microsoft BI",
    "Oracle PL/SQL",
    "Oracle tooling and E-Business Suite",
    "IBM mainframe and IBM i",
    "Java and the JVM",
    "SAP",
    "Other databases",
    "Web",
    "Languages and shells",
    "Statistics and scientific computing",
    "Industrial control and hardware description",
)

#: Project files, build scripts and infrastructure. `.csproj`, `.tf`,
#: `Dockerfile`, `Makefile` - not a language, and unmistakably part of the code
#: of a project. The line most people mean by "the code".
BUILD_GROUPS: tuple[str, ...] = (
    "Azure, installers and Dynamics",
    "Infrastructure and build",
)

#: `.md`, `.csv`, `.json`, `.cfg`. **The honest boundary case**, and off by
#: default: a README is arguably code and a 200MB CSV export plainly is not,
#: and they are the same group. Left visible in the editor rather than decided
#: silently.
DOC_GROUPS: tuple[str, ...] = ("Documentation and data",)

#: preset -> the groups it turns on. `all` is a sentinel: it means "no type
#: filter at all", which is not the same as "every group", because a file type
#: nobody has classified would be excluded by the second and kept by the first.
PRESETS: dict[str, tuple[str, ...]] = {
    "source": SOURCE_GROUPS,
    "build": SOURCE_GROUPS + BUILD_GROUPS,
    "docs": SOURCE_GROUPS + BUILD_GROUPS + DOC_GROUPS,
    "all": (),
}

PRESET_LABELS: dict[str, str] = {
    "source": "Source code only",
    "build": "Source, config and build files",
    "docs": "Source, config, build and documentation",
    "all": "Everything in the repository",
    "custom": "Chosen types…",
}

#: What a new install gets, and what "the code of this project" usually means.
DEFAULT_PRESET = "build"


def group_names() -> tuple[str, ...]:
    """Every ecosystem group, in the order `source_types.py` declares them.

    Declaration order rather than alphabetical: the groups were written to be
    read by somebody who knows one ecosystem and not the others, and shuffling
    them into alphabetical order would scatter the Microsoft ones.
    """
    return tuple(BY_ECOSYSTEM)


def groups_for(preset: str, chosen: Optional[Iterable[str]] = None) -> tuple[str, ...]:
    """The groups a choice turns on.

    `custom` uses `chosen`; every other preset ignores it, so switching to a
    preset and back does not lose what was ticked.
    """
    if preset == "custom":
        known = set(BY_ECOSYSTEM)
        return tuple(name for name in BY_ECOSYSTEM if name in (set(chosen or ()) & known))
    return PRESETS.get(preset, PRESETS[DEFAULT_PRESET])


def extensions_for(preset: str, chosen: Optional[Iterable[str]] = None) -> Optional[frozenset[str]]:
    r"""Extensions the Code tab should show, or `None` for "do not filter".

    **`None` is not the empty set and the difference matters.** `None` means the
    caller adds no `WHERE ext IN (...)` clause at all, so a file type nobody has
    classified is still listed. An empty set would mean "show nothing", which is
    what a preset resolving to no groups would otherwise silently produce.

    Extensions come back **without the leading dot**, because that is the shape
    `files.ext` stores and the shape `type:` accepts. Returning `.cs` here and
    comparing it against `cs` in SQL is a filter that matches nothing and looks
    like an empty repository.

    `NAMED_FILES` are folded in for the build groups: `Makefile` and
    `Dockerfile` are stored under their own name - see
    `source_types.indexed_ext` - and are exactly what somebody switching on
    "config and build" means.
    """
    if preset == "all":
        return None
    names = groups_for(preset, chosen)
    if not names:
        return None
    found = {
        extension.lstrip(".").lower()
        for name in names
        for extension in BY_ECOSYSTEM.get(name, ())
    }
    if set(names) & set(BUILD_GROUPS):
        found |= {name.lstrip(".").lower() for name in NAMED_FILES}
    return frozenset(found)


def parse_choice(raw: str) -> tuple[str, tuple[str, ...]]:
    """`(preset, chosen groups)` from the stored value. Never raises.

    Anything unreadable is the default, which is the answer that shows too much
    rather than too little - a Code tab that has silently hidden a language is
    much harder to notice than one showing a stray PDF.
    """
    import json

    if not raw:
        return DEFAULT_PRESET, ()
    try:
        record = json.loads(raw)
        preset = str(record.get("preset", DEFAULT_PRESET))
        chosen = tuple(str(name) for name in record.get("groups", ()))
    except Exception:                            # noqa: BLE001 - see the docstring
        return DEFAULT_PRESET, ()
    if preset not in PRESET_LABELS:
        return DEFAULT_PRESET, ()
    return preset, chosen


def dump_choice(preset: str, chosen: Iterable[str] = ()) -> str:
    import json

    return json.dumps({"preset": preset, "groups": list(chosen)}, sort_keys=True)


def describe(preset: str, chosen: Optional[Iterable[str]] = None) -> str:
    """One line for the panel: what this choice means, in files not groups."""
    if preset == "all":
        return ("Every file in the repository, including documents and images. "
                "This is what the tab did before the setting existed.")
    extensions = extensions_for(preset, chosen) or frozenset()
    groups = groups_for(preset, chosen)
    return (f"{len(extensions):,} file types across {len(groups)} group(s). "
            f"Everything else stays indexed and searchable - this only decides "
            f"what the Code tab lists.")


def choice_from(store: Any) -> tuple[str, tuple[str, ...]]:
    """The stored choice, defaulting on any failure. **Worker thread.**"""
    try:
        return parse_choice(store.get_state(STATE_KEY, "") or "")
    except Exception:                            # noqa: BLE001
        return DEFAULT_PRESET, ()
