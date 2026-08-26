r"""Is this really a checkout somebody works in?

Layer: L3 (pure - it takes counts, not paths)

From `WORKORDER-202626081149-code-tab.md` §3, and the incident in its §1 is the
whole specification. A copy of this project's own `.git` was dragged into
`D:\SearchData`, a document archive of PDFs, PSTs and Visio files. Detection
found the `.git`, adopted the folder, and attributed **1,179 files - 44% of the
corpus** - to a repository. `scope:code` is `repo_id IS NOT NULL`, so "Code
only" then matched the entire archive.

Three signals were present and every one of them was strong:

* 1,179 files, almost none with a code extension;
* a working tree in which every tracked file reported as deleted;
* two of the four repositories holding zero indexed files.

**The application knew and said so in a place nobody was reading.**
`app.cli repos` has printed *"`scope:code` will match your whole corpus rather
than just code"* since repositories were added. Adopt-and-warn is a fine answer;
silent adoption is not, and the warning has to arrive where somebody meets it.

Pure on purpose: it takes the counts, so A7 - *"a directory holding a `.git`
whose whole tree reads as deleted does not silently attribute 1,000 files"* -
is a fixture rather than a machine with a corrupted archive on it.
"""

from __future__ import annotations

from typing import Optional, Sequence

__all__ = [
    "CODE_SHARE_FLOOR", "BIG_ENOUGH_TO_JUDGE", "suspicion", "describe",
]

#: Below this share of code files, a repository is reported rather than assumed.
#:
#: **A tenth, and deliberately low.** A repository legitimately holds READMEs,
#: licences, images and fixtures, and plenty of real checkouts are more
#: documentation than source. This is not "does it look like a tidy project" -
#: it is "is there any code here at all", and the archive that caused this had
#: almost none.
CODE_SHARE_FLOOR = 0.10

#: Below this many indexed files, the share means nothing and nothing is said.
#:
#: A repository with four files that happen to be `.md` is not evidence of
#: anything, and a warning on every small checkout is a warning nobody reads -
#: which is precisely how the existing one was missed.
BIG_ENOUGH_TO_JUDGE = 50


def suspicion(
    *,
    files: int,
    code_files: int,
    tree_deleted: Optional[bool] = None,
) -> str:
    r"""Why this repository looks wrong, or `""` if it does not.

    `tree_deleted` is "git reports its whole tree as deleted", which the caller
    may not know - it costs a subprocess - so `None` means "not asked" and is
    not evidence either way. When it is known and true it is the strongest
    signal there is: a checkout nobody has is not a checkout.
    """
    if tree_deleted:
        return "every tracked file in it reports as deleted"
    if files < BIG_ENOUGH_TO_JUDGE:
        return ""
    share = code_files / files if files else 0.0
    if share < CODE_SHARE_FLOOR:
        return (f"{code_files:,} of its {files:,} indexed files are code "
                f"({share * 100:.0f}%)")
    return ""


def describe(name: str, root: str, reason: str) -> str:
    r"""The sentence a person reads. Empty when there is nothing to say.

    **Says what it did as well as what it saw.** The repository *is* adopted -
    refusing to would be worse, because a real checkout full of documentation
    would silently stop being code - so the notice reports a decision that has
    already been made and names the way to reverse it. A warning with no next
    step is the one that gets ignored.
    """
    if not reason:
        return ""
    return (
        f"{name or root} was detected as a repository, but {reason}. "
        f"'Code only' searches will include all of it. "
        f"If it is not a checkout, run: app.cli repos --forget \"{root}\""
    )


def code_share(extensions: Sequence[str], code_types: Sequence[str]) -> float:
    """What proportion of these files have a code extension.

    Split out so the caller can report the number it judged on rather than
    recomputing it - a warning quoting one figure and a table showing another
    is how somebody stops believing both.
    """
    wanted = {str(one).lstrip(".").lower() for one in code_types or ()}
    if not wanted or not extensions:
        return 0.0
    seen = [str(one).lstrip(".").lower() for one in extensions]
    return sum(1 for one in seen if one in wanted) / len(seen)
