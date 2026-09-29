r"""Search inside the code, as you type: definitions first, then mentions.

Layer: L4

Order 0y §2. The Code tab's box matched **file names and paths** only; to find
where a function is used, a developer had to leave for the Search tab and add
the Code chip. That is the question a developer arrives with most often, so it
is answered in the Code tab's own box, from the index, inside the typing budget
(`BUILD_SPEC_V2.md`: under 40 ms) - never by git, never by reading a
repository.

**One keyword query, then plain Python on what it returned.** The passages
come from `keyword.search`, the same BM25 engine and the same filters as every
other box (so `/repo`, `/type` and the code scope mean exactly what they mean
elsewhere). Each passage is then read line by line for the lines that hold
what was typed:

* a **definition** is a line that declares a name containing the word - a
  `class`, `interface`, `def`/`function`/`fn`/`func`, a `const`/`let`/`var`,
  or an assignment at the start of a line in a module. The patterns are the
  git grammar's own (`gitquery.SYMBOL_PATTERNS`), loosened so `password`
  finds `ResetPasswordHandler`, because the index already splits camelCase
  that way (`identifiers.symbol_tokens`);
* a **mention** is any other line holding the word.

Definitions are listed before mentions, each group in the keyword engine's
order.

**Line numbers come from the file, not the index.** The indexed text of a code
file is not the file byte for byte - the plain-text reader folds runs of blank
lines - so counting newlines in the index gives the wrong line. `resolve_lines`
reads the real file, on the worker, and finds the matching line nearest to
where the passage sat; it stops when its time budget is spent, so a slow disk
costs line numbers, never typing. A row with no number yet is given one when it
is opened.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "CodeMatch",
    "KIND_DEFINITION",
    "KIND_MENTION",
    "code_matches",
    "resolve_line",
    "resolve_lines",
    "terms_of",
]

KIND_DEFINITION = "definition"
KIND_MENTION = "mention"

#: Passages asked of the keyword engine for one keystroke. Enough for a page of
#: rows after de-duplication; the engine's own cost is per matching row.
PASSAGE_LIMIT = 200
#: Lines taken from one passage. A passage that mentions a word twenty times is
#: one place, not twenty rows.
LINES_PER_PASSAGE = 3
#: Rows handed back for one keystroke.
MATCH_LIMIT = 200
#: A line longer than this is shown cut, with an ellipsis: minified code.
LINE_CHARS = 240
#: Files bigger than this are not read for a line number.
RESOLVE_MAX_BYTES = 4 * 1024 * 1024
#: Seconds `resolve_lines` may spend reading files for one keystroke.
RESOLVE_BUDGET_S = 0.03

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class CodeMatch:
    """One line of code that holds what was typed."""

    kind: str                     # KIND_DEFINITION or KIND_MENTION
    path: str                     # the file, as the index knows it
    text: str                     # the line, trailing space removed, cut at LINE_CHARS
    at: int                       # character offset in the indexed text
    line: Optional[int] = None    # 1-based, once `resolve_lines` has found it
    ext: str = ""


def terms_of(text: str) -> list[str]:
    """The words worth looking for in a line: identifiers of two letters or more."""
    return [w for w in _WORD.findall(str(text or "")) if len(w) >= 2]


def _definition_patterns(terms: Iterable[str]) -> list[re.Pattern[str]]:
    from app.search.gitquery import SYMBOL_PATTERNS

    patterns: list[re.Pattern[str]] = []
    for term in terms:
        name = rf"\w*{re.escape(term)}\w*"
        for key in ("class", "interface", "function"):
            patterns.append(re.compile(SYMBOL_PATTERNS[key].format(name=name), re.I))
        patterns.append(re.compile(
            rf"^\s*(?:export\s+)?(?:const|let|var|val)\s+{name}\b", re.I))
        # A module-level assignment: at the start of the line, not indented.
        patterns.append(re.compile(rf"^{name}\s*(?::[^=]*)?=(?!=)", re.I))
    return patterns


def _holds(line: str, needles: list[str]) -> bool:
    lowered = line.lower()
    return any(n in lowered for n in needles)


def _needles(terms: list[str]) -> list[str]:
    """What a line must contain. A word of five letters or more also matches
    its stem (the index is stemmed: "handlers" found a passage that says
    "handler"), so the line the passage was found for is not missed."""
    needles = []
    for term in terms:
        low = term.lower()
        needles.append(low)
        if len(low) >= 5:
            needles.append(low[:-1] if low.endswith("s") else low[: max(4, len(low) - 2)])
    return needles


def code_matches(store: Any, parsed: Any, *, types: Iterable[str] = (),
                 limit: int = MATCH_LIMIT,
                 passages: int = PASSAGE_LIMIT) -> list[CodeMatch]:
    """Definitions, then mentions, for what `parsed` asks - from the index only.

    `parsed` is the Code box's parse; it is scoped to code here, and the
    configured code types apply when nothing was typed with `/type`, exactly as
    the file list applies them (`tasks.code_rows_for`). Returns [] when there
    is no word to look for - the file list alone answers a box of filters.
    """
    from app.search import keyword

    terms = terms_of(getattr(parsed, "text", ""))
    if not terms:
        return []
    scoped = parsed.scoped("code")
    wanted_types = tuple(types or ())
    if not getattr(scoped, "ext", ()) and wanted_types:
        scoped = replace(scoped, ext=wanted_types)
    hits = keyword.search(store, scoped, limit=passages, prefix_last=True)

    needles = _needles(terms)
    definitions = _definition_patterns(terms)
    seen: set[tuple[str, str]] = set()
    found: dict[str, list[CodeMatch]] = {KIND_DEFINITION: [], KIND_MENTION: []}
    for hit in hits:
        text = str(hit.get("text") or "")
        path = str(hit.get("path") or "")
        start = int(hit.get("char_start") or 0)
        taken = 0
        offset = 0
        for raw in text.split("\n"):
            here = offset
            offset += len(raw) + 1
            line = raw.rstrip()
            if not line.strip() or not _holds(line, needles):
                continue
            key = (path, line.strip())
            if key in seen:
                continue
            seen.add(key)
            kind = (KIND_DEFINITION if any(p.search(line) for p in definitions)
                    else KIND_MENTION)
            shown = line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 1] + "…"
            found[kind].append(CodeMatch(kind=kind, path=path, text=shown,
                                         at=start + here, ext=str(hit.get("ext") or "")))
            taken += 1
            if taken >= LINES_PER_PASSAGE:
                break
    return (found[KIND_DEFINITION] + found[KIND_MENTION])[:limit]


def resolve_line(path: str, text: str, at: int, *, content: Optional[str] = None,
                 max_bytes: int = RESOLVE_MAX_BYTES) -> Optional[int]:
    """The 1-based line in the real file that holds `text`, nearest to `at`.

    None when the file cannot be read, is too big, or no longer holds the line
    (it changed since it was indexed). Never raises.
    """
    if content is None:
        try:
            target = Path(path)
            if target.stat().st_size > max_bytes:
                return None
            content = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
    wanted = text.rstrip("…").strip()
    if not wanted:
        return None
    best: Optional[tuple[int, int]] = None
    offset = 0
    for number, line in enumerate(content.split("\n"), start=1):
        if line.strip().startswith(wanted) or (wanted in line and len(wanted) > 20):
            distance = abs(offset - at)
            if best is None or distance < best[0]:
                best = (distance, number)
        offset += len(line) + 1
    return best[1] if best else None


def resolve_lines(matches: list[CodeMatch], *, budget_s: float = RESOLVE_BUDGET_S,
                  clock: Any = time.perf_counter) -> list[CodeMatch]:
    """`matches` with line numbers filled in, as far as the time budget allows.

    Each file is read once however many of its lines are listed. Matches past
    the budget keep `line=None`; the order is never changed.
    """
    deadline = clock() + budget_s
    contents: dict[str, Optional[str]] = {}
    out: list[CodeMatch] = []
    for match in matches:
        if match.line is not None or clock() > deadline:
            out.append(match)
            continue
        if match.path not in contents:
            try:
                target = Path(match.path)
                contents[match.path] = (
                    None if target.stat().st_size > RESOLVE_MAX_BYTES
                    else target.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                contents[match.path] = None
        content = contents[match.path]
        line = (resolve_line(match.path, match.text, match.at, content=content)
                if content is not None else None)
        out.append(replace(match, line=line))
    return out
