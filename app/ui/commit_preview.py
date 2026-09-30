r"""A commit, as the preview pane shows it. Order 0y §3c.

Layer: L5 - a worker body and the page it builds. No Qt.

**A history row is a commit, not a file.** The version of the file that matched
is gone from disk, so there is nothing to open - but there is a great deal to
read: what the commit said, who made it and when, which files it touched, and
the change itself with the searched text picked out. That is the answer to
"when did this line appear, and why", and it used to be three lines of text.

**`git show` runs here, and only for the row that is selected.** The pane calls
`preview_loader.load_preview_for` on a worker after its usual pause, so arrowing
down a list of fifty commits starts one git, not fifty - and never on the
interface thread.

The page is HTML this module writes itself. Every piece of text that came from
the repository is escaped before it goes in, so a commit message or a line of
code containing `<script>` is shown, not obeyed.
"""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Callable, Optional

from app.ui.preview_loader import KIND_HTML, KIND_TEXT, Preview

__all__ = ["commit_preview", "commit_html", "date_in_words", "STATUS_WORDS"]

#: What git's one-letter status means, in words.
STATUS_WORDS = {"A": "Added", "M": "Modified", "D": "Deleted", "R": "Renamed",
                "C": "Copied", "T": "Type changed"}

#: Colours that read on a light page and on a dark one. The pane's own text
#: colour is left alone everywhere else, so the theme still decides it.
_ADDED = "#2da44e"
_REMOVED = "#e5534b"
_HUNK = "#8a63d2"
#: The searched text: dark ink on yellow, whatever the theme.
_MARK = "background-color:#ffe066; color:#1f2328"


def date_in_words(iso: str) -> str:
    """`2024-01-02T09:00:00+00:00` as *Tuesday 2 January 2024, 09:00*.

    The time is the author's own clock, as git recorded it. A date that cannot
    be read is shown as it came, rather than hidden.
    """
    text = str(iso or "").strip()
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return text
    return f"{when:%A} {when.day} {when:%B %Y}, {when:%H:%M}"


def _marked(text: str, needle: str) -> str:
    """`text` escaped for HTML, with every `needle` in it highlighted."""
    wanted = str(needle or "")
    if not wanted:
        return escape(text)
    pieces = re.split(f"({re.escape(wanted)})", text, flags=re.IGNORECASE)
    return "".join(
        f'<span style="{_MARK}">{escape(piece)}</span>' if index % 2 else escape(piece)
        for index, piece in enumerate(pieces))


def _diff_line(line: str, needle: str) -> str:
    body = _marked(line, needle) or "&nbsp;"
    if line.startswith(("diff --git", "+++ ", "--- ", "index ", "new file", "deleted file",
                        "rename ", "similarity ", "old mode", "new mode")):
        return f"<b>{body}</b>"
    if line.startswith("@@"):
        return f'<span style="color:{_HUNK}">{body}</span>'
    if line.startswith("+"):
        return f'<span style="color:{_ADDED}">{body}</span>'
    if line.startswith("-"):
        return f'<span style="color:{_REMOVED}">{body}</span>'
    return body


def commit_html(detail: Any, needle: str = "") -> str:
    """The page for one commit: message, author and date, files, diff."""
    parts = [f"<h3>{_marked(detail.subject, needle)}</h3>"]
    if detail.body:
        parts.append(f'<p style="white-space:pre-wrap">{_marked(detail.body, needle)}</p>')

    who = f"<b>{escape(detail.author)}</b>"
    if detail.email:
        who += f" &lt;{escape(detail.email)}&gt;"
    parts.append(f"<p>{who}<br>{escape(date_in_words(detail.date))}<br>"
                 f"<code>{escape(detail.commit)}</code></p>")

    count = len(detail.files)
    parts.append(f"<p><b>{count:,} file{'s' if count != 1 else ''} changed</b></p>")
    if detail.files:
        rows = []
        for letter, path, old in detail.files:
            word = STATUS_WORDS.get(letter, letter)
            where = escape(path) + (f" (was {escape(old)})" if old else "")
            rows.append(f"<tr><td>{escape(word)}&nbsp;&nbsp;</td><td>{where}</td></tr>")
        parts.append(f'<table cellspacing="0" cellpadding="1">{"".join(rows)}</table>')

    if detail.diff:
        lines = "\n".join(_diff_line(line, needle) for line in detail.diff.split("\n"))
        parts.append(f"<pre>{lines}</pre>")
    return "\n".join(parts)


def commit_preview(row: Any, *, show: Optional[Callable[..., Any]] = None) -> Preview:
    """What the pane draws for a history row. **Worker thread only** - it runs git.

    `row` carries the commit's id, the repository folder git reads it from, and
    the text that was searched for (`presenter.git_result_row`). When git cannot
    show the commit - the repository has moved, the commit was rewritten away -
    the pane shows what the row already knew and says why there is no more.
    """
    from app.search.gitsearch import SHOW_MAX_LINES, show_commit

    sha = str(getattr(row, "commit", "") or "")
    needle = str(getattr(row, "needle", "") or "")
    name = str(getattr(row, "name", "") or "")
    detail = (show or show_commit)(Path(str(getattr(row, "repo_root", "") or "")), sha)
    if not detail.ok:
        return Preview(
            kind=KIND_TEXT, title=name or sha[:8],
            body=str(getattr(row, "preview_text", "") or ""),
            subtitle=f"Commit {sha[:8]}",
            notice=f"git could not show this commit: {' '.join(str(detail.error).split())}")

    subtitle = "  ·  ".join(part for part in (
        f"Commit {detail.commit[:8]}", detail.author, date_in_words(detail.date)) if part)
    notice = ""
    if detail.truncated:
        notice = (f"This commit's diff is longer than the preview shows: the first "
                  f"{SHOW_MAX_LINES:,} lines of it are here.")
    return Preview(kind=KIND_HTML, body=commit_html(detail, needle),
                   title=detail.subject or name, subtitle=subtitle, notice=notice,
                   meta={"commit": detail.commit, "needle": needle})
