"""Every tracked document carries a version header.

The specs drive the build. A spec that changed silently is a spec nobody can
trust, and "which version of the plan were we working to?" has to be answerable
from the file itself. `docs/VERSIONING.md` defines the header; this enforces it,
so a new document cannot be added without one.

Deliberately stdlib-only, so it runs even when the app's dependencies are not
installed - this is a repo-hygiene check, not an application test.
"""

from __future__ import annotations

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

# Directories that are not ours to version.
EXCLUDED = {"venv", ".git", "node_modules", ".pytest_cache", "__pycache__"}


def _tracked_markdown() -> list[Path]:
    return sorted(
        path
        for path in PROJECT_ROOT.rglob("*.md")
        if not EXCLUDED.intersection(path.relative_to(PROJECT_ROOT).parts)
    )


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
