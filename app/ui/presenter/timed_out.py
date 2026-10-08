"""The Indexing page's "Timed-out files" panel, in words.

Layer: L5. Part of the presenter package; imports no Qt.

Work order 0z, item F3. The panel lists the files that ran out of time, one
row per file type, each with the button that reads that type again with a
longer limit. The rows come from `SqliteStore.timed_out_groups` - read on a
worker, with the rest of the index summary (`tasks.read_index_summary`) - and
this module turns them into what the panel says. Every sentence the panel
shows is here, so it can be tested without a display.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Iterable, Optional

from app.index.timed_out_retry import DEFAULT_FACTOR, MAX_FACTOR
from app.ui.presenter.formatting import format_count

__all__ = [
    "ALL_TYPES_LABEL",
    "FACTOR_DEFAULT",
    "FACTOR_LABEL",
    "FACTOR_MAX",
    "FACTOR_MIN",
    "FACTOR_SUFFIX",
    "FACTOR_TOOLTIP",
    "PANEL_TITLE",
    "RETRY_BUSY",
    "RETRY_LABEL",
    "RETRY_TOOLTIP",
    "TimedOutRow",
    "factor_words",
    "panel_title",
    "retry_accessible_name",
    "timed_out_rows",
]

PANEL_TITLE = "Timed-out files"
#: The button's words, exactly as the work order gives them.
RETRY_LABEL = "Retry with a longer time limit"
RETRY_TOOLTIP = (
    "Read these files again now, giving each one longer than usual. Only "
    "these files are read, and only this retry gets the longer limit - the "
    "limits on the Tuning shelf stay as they are. A file that runs out of "
    "time again stays timed out, and says what it was given.")
FACTOR_LABEL = "Give each file"
FACTOR_SUFFIX = " times the usual limit"
FACTOR_TOOLTIP = (
    "How much longer a retry gives each file: this many times the limit it "
    "would usually get. It applies to the next retry you start here and is "
    "not saved.")
#: The box offers 2 upwards: on this button, "longer" has to be longer. (The
#: command line also takes 1, for a retry after raising the setting itself.)
FACTOR_MIN = 2
FACTOR_MAX = MAX_FACTOR
FACTOR_DEFAULT = DEFAULT_FACTOR
#: Said when the button is pressed while a run is going.
RETRY_BUSY = ("An index run is going. Retry the timed-out files once it has "
              "finished.")
ALL_TYPES_LABEL = "all of them"


@dataclass(frozen=True)
class TimedOutRow:
    """One file type's timed-out files, as the panel draws them."""

    #: `files.ext`: no dot, `""` for files with no extension.
    ext: str
    count: int
    #: One of the files, in full, for the tooltip.
    example: str = ""

    @property
    def kind(self) -> str:
        return f".{self.ext}" if self.ext else "no extension"

    @property
    def heading(self) -> str:
        """"3 .pdf files", or "2 files with no extension"."""
        noun = "file" if self.count == 1 else "files"
        if not self.ext:
            return f"{format_count(self.count)} {noun} with no extension"
        return f"{format_count(self.count)} {self.kind} {noun}"

    @property
    def example_text(self) -> str:
        """`e.g. big.pdf` - the name only; the panel is not a file list."""
        if not self.example:
            return ""
        return f"e.g. {PurePath(self.example.replace(chr(92), '/')).name}"


def timed_out_rows(groups: Optional[Iterable[Any]]) -> list[TimedOutRow]:
    """The store's groups as panel rows, most files first. Tolerant: anything
    that is not a group with a count above zero is left out, never raised on -
    this runs on the thread that paints."""
    rows: list[TimedOutRow] = []
    for group in groups or ():
        try:
            count = int(group.get("count", 0) or 0)
            ext = str(group.get("ext", "") or "")
            example = str(group.get("example", "") or "")
        except (AttributeError, TypeError, ValueError):
            continue
        if count > 0:
            rows.append(TimedOutRow(ext=ext, count=count, example=example))
    rows.sort(key=lambda row: (-row.count, row.ext))
    return rows


def panel_title(rows: Iterable[TimedOutRow]) -> str:
    """"12 files timed out" - the panel's heading, over every row."""
    total = sum(row.count for row in rows)
    noun = "file" if total == 1 else "files"
    return f"{format_count(total)} {noun} timed out"


def factor_words(factor: float) -> str:
    return f"{factor:g} times the usual limit"


def retry_accessible_name(row: TimedOutRow) -> str:
    """What a screen reader says for one row's button: every row's button has
    the same words on it, so the name says which files it is for."""
    return f"{RETRY_LABEL}: {row.heading}"
