r"""Nothing in the project folder is named like a path from another platform.

Layer: L0 (repo hygiene)

Reported by the owner: *"i just noticed a folder DKnowledgeGraphData in our
search project folder it was like the bug before"*. There was - a directory
literally named `D:\KnowledgeGraphData`, with `cache`, `fts`, `models`, `state`
and `vectors` inside it, sitting in the checkout.

**A backslash and a colon are legal filename characters on Linux and macOS.**
So `Path("D:\\KnowledgeGraphData").mkdir(parents=True)` there does not fail and
does not warn: it creates a folder whose *name* is a drive letter, a colon and a
path, in whatever directory happened to be current. The application then indexes
into it, reporting the configured path back correctly the whole time.

`config._refuse_foreign_path` guards `load_settings`, and it works - it refuses
this `.env` on Linux today. But it guards one door, and there is more than one
thing in this application that creates a directory. This test guards the
*outcome* instead: it does not care which code path made the folder, only that
the folder is not there.

That is the right shape for this failure. It is silent, it is created by a path
nobody would think to check, and the only reliable symptom is the folder itself.
It has now happened at least twice, and the same family of mistake - a Windows
path treated as a POSIX one - has been found three separate times in this
project's own source.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: `C:\...`, `\\server\share`, or any name carrying a drive-letter colon.
#: Deliberately broad: the point is to catch a *name* that was meant to be a
#: path, and there is no legitimate reason for one in this repository.
FOREIGN = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)|[\\]")


def _entries() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.iterdir():
        try:
            path.stat()
        except OSError:
            continue                     # unreadable, and not ours to judge
        found.append(path)
    return found


def test_no_entry_is_named_like_a_windows_path():
    """**The tripwire.** It would have caught this the moment it happened,
    rather than when somebody noticed the folder weeks later."""
    offenders = [path.name for path in _entries() if FOREIGN.search(path.name)]

    assert not offenders, (
        f"{offenders} are named like paths rather than being paths. Something "
        f"created a directory from a Windows path string on a platform where "
        f"a backslash is an ordinary character - see "
        f"`config._refuse_foreign_path`, and check what else calls mkdir.")


def test_the_index_did_not_land_inside_the_source_tree():
    """The other half of the same accident, and the more expensive one.

    An index built inside the checkout is one the walker will then find and
    index - `own_paths` exists precisely to stop that - and one that `stage.py`
    would happily copy into a release.
    """
    #: **Two of them, not one.** A lone `models/` is a HuggingFace cache and a
    #: normal thing for a machine that has run an embedding model; `fts/` beside
    #: `vectors/` beside `state/` is DATA_PATH having resolved inside the
    #: checkout, which is the accident worth shouting about. A tripwire that
    #: fires on the innocent case is one people learn to ignore.
    found = [name for name in ("vectors", "fts", "state", "cache", "models")
             if (ROOT / name).exists()]

    assert len(found) < 2, (
        f"{found} are index folders and they are in the source tree. "
        f"DATA_PATH resolved to somewhere inside the checkout - the walk will "
        f"then index the index, and `stage.py` would ship it.")


def test_the_guard_that_should_have_stopped_it_still_exists():
    """Belt and braces: this test checks the outcome, and that one checks the
    intent. Losing either quietly would leave the other looking sufficient."""
    from app.core.config import _refuse_foreign_path

    assert callable(_refuse_foreign_path)


def test_the_guard_refuses_a_windows_path_off_windows(monkeypatch):
    import app.core.config as config
    from app.core.errors import AppErrorException

    monkeypatch.setattr(config.sys, "platform", "linux")

    try:
        config._refuse_foreign_path("DATA_PATH", Path(r"D:\Data"))
    except AppErrorException as exc:
        assert "Windows path" in exc.error.render()
    else:
        raise AssertionError("a Windows path was accepted on Linux")


def test_the_guard_leaves_windows_alone(monkeypatch):
    """On Windows these are the only paths there are. A guard that fired there
    would refuse every correct configuration."""
    import app.core.config as config

    monkeypatch.setattr(config.sys, "platform", "win32")

    config._refuse_foreign_path("DATA_PATH", Path(r"D:\Data"))   # must not raise


# --- the same mistake, one layer up: a Windows path inside a docstring -------

def test_no_module_compiles_with_a_syntax_warning():
    r"""**Third sighting of the same family, so it becomes a test.**

    Reported by the owner: every `app.cli` invocation printed

        app\cli.py:1371: SyntaxWarning: invalid escape sequence '\P'
          app.cli gitsearch --repo D:\Project "CustomerId /history"

    before doing anything. The offending line was a *docstring* - a usage
    example showing a Windows repository path - in a plain triple-quoted
    string rather than a raw one, so Python read `\P` as an escape sequence
    and warned at import time. Nothing was broken; the noise simply sat in
    front of every command this application offers, which is its own kind of
    broken.

    Compiling is the whole test. `compile()` raises nothing for this - it warns
    - so the warning has to be *captured*, which is why this does not just
    import the tree. Importing would also run module-level code and drag in
    Qt; compiling reads bytes and produces an AST.

    The fix is one character and the guard is this, because in this codebase a
    mistake found twice becomes a test. `evaluate.py`, `translate.py` and
    `query.py` already open with raw docstrings for exactly this reason - the
    convention existed and one module had drifted off it.

    **Both categories, and that is not belt-and-braces.** Python raised
    `DeprecationWarning` for an invalid escape sequence until 3.12, when it
    became `SyntaxWarning`. Written for `SyntaxWarning` alone, this test passed
    on 3.10 with the offending docstring put back - a guard that agrees with
    you on the machine you run it on and reports the bug on nobody's. So the
    category is matched by *message* as well, which is the part that has been
    stable across both.
    """
    import warnings

    def _is_escape_warning(entry) -> bool:
        if issubclass(entry.category, SyntaxWarning):
            return True
        return (issubclass(entry.category, DeprecationWarning)
                and "invalid escape sequence" in str(entry.message))

    offenders: list[str] = []
    for source in sorted((ROOT / "app").rglob("*.py")):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                compile(source.read_text(encoding="utf-8"), str(source), "exec")
            except SyntaxError as exc:       # a real one, and worth failing on
                offenders.append(f"{source.relative_to(ROOT)}: {exc}")
                continue
        offenders.extend(
            f"{source.relative_to(ROOT)}:{w.lineno}: {w.message}"
            for w in caught if _is_escape_warning(w)
        )

    assert offenders == [], (
        "these modules warn when Python reads them, and the warning is printed "
        "to the user before any command runs:\n  " + "\n  ".join(offenders)
    )
