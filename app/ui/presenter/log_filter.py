"""Which lines of an index run's log count as "warnings and errors".

Layer: L5. Part of the presenter package; imports no Qt.

Work order 0x §4e. The Indexing page's log (`widgets/run_log.py`) gains a
choice between every line and only the ones that need somebody's attention.
**The decision is here, not in the widget**, so it can be tested without a
window and so any other place that ever filters the same log (the command line,
a copy for a bug report) draws the line in the same place.

What counts as needing attention, by the entry's `kind` (the keys in
`app/index/activity.py`):

* `warning` - the pipeline's own plain-sentence warnings (a folder it could not
  read, a disk filling up, a file that will not open);
* `notice` - things that are not failures but are worth knowing before a run
  that takes days, such as "40GB free on the index drive";
* `error` - not recorded by any reader today; listed so that §2c's "the indexer
  stopped unexpectedly" line, when it arrives, is never filtered out.

Everything else - phases starting, a large file being opened, pauses and
carrying on, the finish - is the run's ordinary story, shown under "All".
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "LOG_FILTER_ALL",
    "LOG_FILTER_WARNINGS",
    "LOG_FILTERS",
    "WARNING_KINDS",
    "is_warning",
    "shown_under",
]

#: The two choices, as keys. Keys rather than the words so a saved choice or a
#: test does not break if the words are ever changed by the owner.
LOG_FILTER_ALL = "all"
LOG_FILTER_WARNINGS = "warnings"

#: `(key, words)` in the order the page offers them.
LOG_FILTERS: tuple[tuple[str, str], ...] = (
    (LOG_FILTER_ALL, "All"),
    (LOG_FILTER_WARNINGS, "Warnings and errors"),
)

#: The entry kinds that survive the "Warnings and errors" filter.
WARNING_KINDS = frozenset({"warning", "notice", "error"})


def is_warning(entry: Any) -> bool:
    """Does this log entry need somebody's attention? Never raises."""
    return str(getattr(entry, "kind", "") or "") in WARNING_KINDS


def shown_under(entry: Any, choice: str) -> bool:
    """Is `entry` shown when the filter is set to `choice`?

    An unknown choice shows everything: a filter that silently hides lines
    because of a typo is worse than no filter.
    """
    if choice == LOG_FILTER_WARNINGS:
        return is_warning(entry)
    return True
