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


def _tracked_markdown() -> list[Path]:
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(PROJECT_ROOT):
        subdirectories[:] = [
            name for name in subdirectories
            if name not in EXCLUDED and not name.startswith(("D:", "E:"))
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
