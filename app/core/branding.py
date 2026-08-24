"""What the application is called. One place, so a rename is one edit.

Layer: L0

The name was previously written out in nine files - the window title, the
QApplication name, the CLI banner, argparse's description, the diagnostic
summary header, a SQL comment, the package docstring and two places in
`doctor.py`. Renaming it therefore meant a find-and-replace across the tree,
which is the kind of change that quietly misses one and leaves the old name
staring out of a dialog for a year.

So the name lives here, next to `version.py`, for the same reason the version
does: **anything that appears in front of a person should have exactly one
definition.**

`NAME` is what people call it. `TAGLINE` says what it does, because a name alone
tells a new user nothing - the window title bar is often the only documentation
anybody reads.
"""

from __future__ import annotations

__all__ = ["NAME", "TAGLINE", "SHORT_DESCRIPTION", "window_title", "banner"]

#: What the application is called.
NAME = "Leasha"

#: Shown beside the name where there is room for it.
TAGLINE = "Search everything on this machine"

#: One line, for `--help` and the diagnostic bundle.
SHORT_DESCRIPTION = (
    "Leasha - hybrid keyword and meaning-based search over your files and mail. "
    "Everything stays on this machine."
)


def window_title(suffix: str = "") -> str:
    """The window's title bar. `suffix` names what is being looked at, if
    anything - `Leasha - Settings` rather than a bare name that never changes."""
    return f"{NAME} - {suffix}" if suffix else NAME


def banner(version: str) -> str:
    """The first line the CLI prints, with the version attached."""
    return f"{NAME}  version {version}"
