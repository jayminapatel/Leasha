"""Every tracked document carries a version header.

The specs drive the build. A spec that changed silently is a spec nobody can
trust, and "which version of the plan were we working to?" has to be answerable
from the file itself. `docs/VERSIONING.md` defines the header; this enforces it,
so a new document cannot be added without one.

Deliberately stdlib-only, so it runs even when the app's dependencies are not
installed - this is a repo-hygiene check, not an application test.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The exact shape from docs/VERSIONING.md:
#   **Doc version:** 2.1 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.1
HEADER = re.compile(
    r"^\*\*Doc version:\*\* (?P<doc>\d+\.\d+) · "
    r"\*\*Updated:\*\* (?P<updated>\d{4}-\d{2}-\d{2}) · "
    r"\*\*Applies to:\*\* app v(?P<applies>\d+\.\d+\.\d+)$",
    re.MULTILINE,  # the anchors must bind to the header line, not the whole file
)

# Directories that are not ours to version. Pruned during the walk, not filtered
# afterwards: `rglob` descends into venv/Lib/site-packages first and only then
# discards the result, which on a mounted drive takes long enough to look like a
# hang. Never walk a tree you are going to throw away.
EXCLUDED = {
    "venv", ".venv", ".git", "node_modules", ".pytest_cache", "__pycache__",
    "logs", "build", "dist", ".mypy_cache", ".ruff_cache",
    # **Worktrees are this same repository checked out again**, so every
    # document inside one is a second copy of a file already checked at its
    # real path. Walking them does not widen the contract - it reports the
    # same fault several times over, against paths like
    # `.worktrees/lane-c/ACTIVE_WORK.md` that nobody can fix, because fixing
    # the real file leaves an old checkout still holding the old text.
    #
    # This is scope, not leniency: no document stops being checked. Parallel
    # agents each need their own worktree (they collide otherwise), so the
    # count of these varies run to run - which on its own made the suite's
    # failure count unreproducible.
    ".worktrees", ".claude",
}

# Paths (relative to the project root) whose markdown is **generated, not
# written**. `_Knowledge/prompt_log/views/*.md` are session ledgers rendered from
# the `.jsonl` ledgers by the plugin's `promptlog.py md`; each says "Regenerate
# it; never edit it", none is tracked, and the generator lives outside this
# repository - so no header could be added here that the next render would not
# remove. They failed fifteen checks a day for as long as they existed (found
# 2026-09-19). The `.jsonl` ledgers they are drawn from are the record.
GENERATED = {"_Knowledge/prompt_log/views"}

# **pytest's own basetemp, not ours to version either.** `pyproject.toml`'s
# addopts points `--basetemp` at `.pytest_tmp` inside the project (see the
# comment there on why - a OneDrive junction otherwise breaks the default
# location), so fixture markdown a *different* test writes mid-run - a fake
# git repo's README.md, a `docs/b.md` an indexing test builds - lands inside
# the tree this file walks. Caught twice live: those fixture files have no
# version header, so a suite run that hits both files at once (or a basetemp
# left over from an interrupted prior run) fails this one on somebody else's
# scratch data, not a real doc. Matched by prefix, not exact name, so a
# custom `--basetemp=.pytest_tmp_adhoc` from another session or worktree is
# excluded the same way.
_BASETEMP_PREFIX = ".pytest_tmp"


def _tracked_markdown() -> list[Path]:
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(PROJECT_ROOT):
        here = Path(directory).relative_to(PROJECT_ROOT).as_posix()
        subdirectories[:] = [
            name for name in subdirectories
            if name not in EXCLUDED and not name.startswith(("D:", "E:"))
            and not name.startswith(_BASETEMP_PREFIX)
            and (f"{here}/{name}" if here != "." else name) not in GENERATED
        ]
        found.extend(
            Path(directory) / name for name in filenames if name.lower().endswith(".md")
        )
    return sorted(found)


def _ids(paths: list[Path]) -> list[str]:
    return [str(p.relative_to(PROJECT_ROOT)).replace("\\", "/") for p in paths]


DOCS = _tracked_markdown()


def test_there_are_documents_to_check() -> None:
    """Guards against the glob silently matching nothing and the suite passing."""
    assert len(DOCS) >= 7, f"expected the project's docs, found {_ids(DOCS)}"


def test_pytest_basetemp_fixture_markdown_is_excluded(tmp_path, monkeypatch) -> None:
    r"""Reproduced live twice: `--basetemp=.pytest_tmp` (`pyproject.toml`)
    means another test's fixture markdown - a fake git repo's README.md, a
    `docs/b.md` an indexing test writes - lands inside this file's own walk
    when it runs mid-suite, or when a basetemp from an interrupted prior run
    is still on disk. Those files have no version header and are not a real
    project doc; the walk must prune them the same way it already prunes
    `.worktrees`. Matched by prefix (`.pytest_tmp*`), not exact name, so a
    differently-configured `--basetemp` from another session or worktree -
    `.pytest_tmp_adhoc` - is caught the same way.
    """
    (tmp_path / ".pytest_tmp" / "fake_repo").mkdir(parents=True)
    (tmp_path / ".pytest_tmp" / "fake_repo" / "README.md").write_text(
        "not a real doc, no header", encoding="utf-8")
    (tmp_path / ".pytest_tmp_adhoc" / "docs").mkdir(parents=True)
    (tmp_path / ".pytest_tmp_adhoc" / "docs" / "b.md").write_text(
        "also not a real doc", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "REAL.md").write_text(
        "# Real\n\n**Doc version:** 1.0 · **Updated:** 2026-01-01 · "
        "**Applies to:** app v0.3.3\n", encoding="utf-8")

    monkeypatch.setattr(sys.modules[__name__], "PROJECT_ROOT", tmp_path)
    found = _ids(_tracked_markdown())

    assert "docs/REAL.md" in found
    assert not any(".pytest_tmp" in path for path in found), (
        f"pytest's own basetemp fixture markdown leaked into the walk: {found}"
    )


@pytest.mark.parametrize("doc", DOCS, ids=_ids(DOCS))
def test_document_has_a_version_header(doc: Path) -> None:
    """The header is line 3: H1, blank, header."""
    lines = doc.read_text(encoding="utf-8").splitlines()
    assert lines, f"{doc.name} is empty"
    assert lines[0].startswith("# "), f"{doc.name} must open with an H1"

    header_line = next((line for line in lines[1:6] if line.startswith("**Doc version:")), None)
    assert header_line is not None, (
        f"{doc.name} has no version header. Add, directly under the H1:\n"
        f"    **Doc version:** 1.0 · **Updated:** {date.today().isoformat()} "
        f"· **Applies to:** app v{_app_version()}\n"
        f"See docs/VERSIONING.md."
    )
    assert HEADER.match(header_line), (
        f"{doc.name} header does not match the format in docs/VERSIONING.md.\n"
        f"  found:    {header_line}\n"
        f"  expected: **Doc version:** 1.0 · **Updated:** YYYY-MM-DD · **Applies to:** app vX.Y.Z\n"
        f"Note the separator is U+00B7 MIDDLE DOT, not a hyphen or a bullet."
    )


def _app_version() -> str:
    return (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("doc", DOCS, ids=_ids(DOCS))
def test_applies_to_is_a_version_that_existed(doc: Path) -> None:
    """A doc may lag the app - that gap is the signal to review it - but it may
    not claim to describe a version that has not been cut yet."""
    match = HEADER.search(doc.read_text(encoding="utf-8"))
    assert match is not None
    claimed = tuple(int(part) for part in match.group("applies").split("."))
    current = tuple(int(part) for part in _app_version().split("."))
    assert claimed <= current, (
        f"{doc.name} says it applies to app v{match.group('applies')}, "
        f"but VERSION is {_app_version()}."
    )


@pytest.mark.parametrize("doc", DOCS, ids=_ids(DOCS))
def test_updated_date_is_not_in_the_future(doc: Path) -> None:
    match = HEADER.search(doc.read_text(encoding="utf-8"))
    assert match is not None
    updated = date.fromisoformat(match.group("updated"))
    assert updated <= date.today(), f"{doc.name} is dated {updated}, which is in the future"


def test_the_two_specs_are_major_version_2() -> None:
    """V1 assumed FastAPI, PostgreSQL and Qdrant and is void. The V2 specs that
    replaced it start at 2.0, and dropping back to 1.x would erase that history."""
    for name in ("BUILD_SPEC_V2.md", "LOCAL_KNOWLEDGE_GRAPH_V2.md"):
        match = HEADER.search((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        assert match is not None, f"{name} has no version header"
        assert match.group("doc").startswith("2."), (
            f"{name} is at {match.group('doc')}; the V2 specs are major version 2"
        )


def test_the_load_bearing_tests_all_exist() -> None:
    r"""Every test named in `WORKORDER-CONVENTIONS.md` §0 is a real test.

    **Three of the eight rows were wrong.** `test_the_presenter_still_imports_no_qt`
    named nothing at all - there are two guards spelled differently, in two
    files - and three more rows named a module rather than the test inside it.

    §0 calls these tests load-bearing and says the temptation with one thread
    and no reviewer is to adjust them rather than fix what they found. A table
    that names them approximately cannot notice the day one is deleted, which
    is the only day it matters - so the table is checked rather than trusted.

    Parsed from the table itself: the first column is the test, the second is
    the file it lives in. Both are asserted, because a test that moved is as
    invisible as one that went.
    """
    import re

    text = (PROJECT_ROOT / "docs" / "WORKORDER-CONVENTIONS.md").read_text(
        encoding="utf-8")
    rows = re.findall(
        r"^\|\s*`(test_[a-z0-9_]+)`\s*\|\s*`(test_[a-z0-9_]+\.py)`\s*\|",
        text, re.MULTILINE)

    assert len(rows) >= 8, (
        f"only {len(rows)} load-bearing tests parsed out of the table - the "
        f"table's shape changed and this check stopped covering it")

    missing = []
    for name, filename in rows:
        path = PROJECT_ROOT / "tests" / "unit" / filename
        if not path.is_file():
            missing.append(f"{name}: {filename} does not exist")
        elif f"def {name}(" not in path.read_text(encoding="utf-8"):
            missing.append(f"{name} is not in {filename}")

    assert missing == [], (
        "WORKORDER-CONVENTIONS.md section 0 names tests that are not there:\n  "
        + "\n  ".join(missing))
