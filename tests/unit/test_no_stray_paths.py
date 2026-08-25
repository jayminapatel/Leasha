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
