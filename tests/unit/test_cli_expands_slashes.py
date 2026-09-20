r"""What you type works the same from the command line as from the window.

Layer: L0/L4 - wiring, not behaviour.

**The bug.** `app/cli/search.py` passed the raw query straight to `SearchEngine.search`,
while every other caller - the window's presenter, the Code and Repos views, and
`evaluate` - ran `expand_slashes` over it first. So `/newest` reached `parse_query`
unexpanded, came back with an empty sort, and was **silently dropped**: the command line
answered `report /newest` in relevance order and said nothing about it, while the window
sorted by date. Found on 2026-09-20 by running it against the real index and checking the
dates of what came back, which is the only way it *could* be found - every result was a
plausible result.

That is the third shape of the same fault found in one afternoon (`--interpret` accepted
and ignored; a chat evaluation run against no vectors), and this project's standing rule
is that nothing fails silently. A slash command that is accepted and then discarded is
exactly that.

The check is on the source rather than on a live search because the alternative is a
real index, a real embedding model and a reranker for what is a one-line wiring
question - and a test that expensive gets marked slow and then gets skipped, which is
how the wiring came to be missing in the first place.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: `engine.search(<something>, ...)` - a real search being run.
SEARCH_CALL = re.compile(r"\bengine\.search\(\s*([A-Za-z_][\w.]*)\s*[,)]")


def test_the_command_line_expands_slashes_before_it_searches():
    source = (ROOT / "app" / "cli" / "search.py").read_text(encoding="utf-8")
    assert "expand_slashes" in source, (
        "app/cli/search.py no longer expands slash commands: `/newest`, `/type pdf` and "
        "the rest will be accepted and silently ignored, and the command line will "
        "quietly disagree with the window")
    bare = [m.group(1) for m in SEARCH_CALL.finditer(source)]
    assert not bare, (
        "app/cli/search.py passes a bare name to engine.search - it must pass "
        f"expand_slashes(...): {bare}")


def test_every_search_entry_point_agrees():
    """One entry point that skips it is the whole bug, so they are checked together."""
    entry_points = [
        Path("app") / "cli" / "search.py",
        Path("app") / "ui" / "presenter" / "search.py",
        Path("app") / "cli" / "evaluate.py",
    ]
    missing = [p.as_posix() for p in entry_points
               if "expand_slashes" not in (ROOT / p).read_text(encoding="utf-8")]
    assert not missing, (
        "a search entry point does not expand slash commands, so what a person types "
        f"means different things in different places: {missing}")


def test_the_expansion_really_produces_a_sort():
    """The positive half: `/newest` is not merely passed on, it becomes a sort.

    Without this the guard above would still pass if `expand_slashes` became a no-op.
    """
    from app.search.commands import expand_slashes
    from app.search.query import parse_query

    assert parse_query(expand_slashes("report /newest")).sort == "newest"
    assert parse_query(expand_slashes("report /oldest")).sort == "oldest"
    # And the shape that was shipping: unexpanded, the sort is empty and falsy, which
    # is why nothing raised and nothing sorted.
    assert not parse_query("report /newest").sort
