r"""The quick search box's words and decisions. Qt-free.

Layer: L5 (presenter half)

The box that opens from anywhere (`widgets/mini_search.py`, the global
shortcut). Everything it *says*, and every decision that can be made without
a display - which chip a result belongs to, what a row is called, where the
window may sit on the screens that exist now - lives here, so it can be
tested with plain values.

**A message is called by its subject, never by its key.** A row from an
Outlook archive has a path like `pst://2024/2109476`; the number is an entry
id nobody has ever seen. The title is the subject (or "(no subject)", or
"Message" while it is not known), the second line says who sent it.

**The window remembers where it was left, and is put back on a screen that
exists.** `fit_on_screens` takes the saved rectangle and the screens' usable
areas as plain tuples: a laptop undocked from the monitor the box was left on
gets the box back on the laptop's own screen, the whole of it visible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "CHIP_ORDER", "SCOPE_CHIPS", "PLACEHOLDER", "PLACE_KEY", "DEFAULT_SIZE", "MIN_SIZE",
    "RowLines", "row_label", "row_lines", "kind_bucket", "chip_label", "chip_tip",
    "scope_request", "fit_on_screens", "default_place", "place_text", "read_place", "recent_wanted",
    "hints", "Hint", "fit_hints", "empty_words", "nothing_found", "RECENT_HEADING", "SAVED_NOTE",
    "RECENT_NOTE", "COPIED", "OUTLOOK_ONLY", "SEARCHING", "results_count",
    "PREVIEW_TIP", "EXPAND_TIP", "CLOSE_TIP", "MOVE_TIP", "RESIZE_TIP", "is_message_group",
]

#: Typed into an empty box - unchanged since the box first shipped.
PLACEHOLDER = "Search everything — Enter opens it"

#: The `index_state` key the window's place is saved under.
PLACE_KEY = "ui:mini_search_place"

#: The size it first opens at, before anybody has resized it: wide enough for
#: a file name and its folder, short enough not to cover what is being read.
DEFAULT_SIZE = (720, 560)

#: The smallest it can be made. Below this the chips and the hints wrap.
MIN_SIZE = (560, 380)

#: **The kinds the chips count and filter by**, in a fixed order so a chip
#: never moves between one keystroke and the next. "files" is everything that
#: is not mail, a picture or code.
CHIP_ORDER = ("files", "mail", "photos", "code")

#: `(bucket, label, icon)`. `None` is "All". The icons are the app's own set.
SCOPE_CHIPS: tuple[tuple[Optional[str], str, str], ...] = (
    (None, "All", "layout-grid"),
    ("files", "Files", "file-text"),
    ("mail", "Mail", "mail"),
    ("photos", "Photos", "image"),
    ("code", "Code", "code"),
)

#: `bucket -> (singular, plural)`, for `chip_label`.
_CHIP_WORDS = {
    "files": ("file", "files"),
    "mail": ("email", "emails"),
    "photos": ("photo", "photos"),
    "code": ("code result", "code results"),
}

_CHIP_TIPS = {
    None: "Show every kind of result",
    "files": "Show only documents and other files",
    "mail": "Show only emails",
    "photos": "Show only photos and pictures",
    "code": "Show only code",
}

PREVIEW_TIP = "Show the selected result beside the list, without opening it (Ctrl+P)"
EXPAND_TIP = "Show every result in the main Leasha window (Shift+Enter)"
CLOSE_TIP = "Close this box (Esc)"
MOVE_TIP = "Drag to move this box. It opens where you leave it."
RESIZE_TIP = "Drag to make this box bigger or smaller"

RECENT_HEADING = "Recent searches"
RECENT_NOTE = "Searched before"
SAVED_NOTE = "Saved search"
COPIED = "Copied the path"
OUTLOOK_ONLY = "Ctrl+O opens an email in Outlook. This result is not an email."
SEARCHING = "Searching…"


def kind_bucket(kind: str) -> str:
    r"""A `ResultGroup.kind` - `"email"`, or a file extension - as one of
    `CHIP_ORDER`.

    **Read from `_EXT_GROUPS`, the parser's own table**, the same table
    `/type code` and `/type image` already answer from - a second list of
    extensions here would be the drift this project keeps finding.
    """
    from app.search.query import _EXT_GROUPS

    key = str(kind or "").lower().lstrip(".")
    if key == "email":
        return "mail"
    if key in _EXT_GROUPS.get("code", ()):
        return "code"
    if key in _EXT_GROUPS.get("image", ()):
        return "photos"
    return "files"


def chip_label(bucket: str, count: int) -> str:
    """`"7 files"`, `"1 email"` - plain words, singular where it matters."""
    singular, plural = _CHIP_WORDS.get(bucket, (bucket, bucket))
    return f"{count} {singular if count == 1 else plural}"


def chip_tip(bucket: Optional[str]) -> str:
    """What pressing a chip does."""
    return _CHIP_TIPS.get(bucket, "Show only these results")


def results_count(count: int) -> str:
    return "1 result" if count == 1 else f"{count} results"


def scope_request(bucket: Optional[str], query: str) -> tuple[str, str]:
    r"""`(scope, query)` for the search call, for the chip that is on.

    The engine's scopes are `all`, `mail`, `documents` and `code`; a picture
    is a document with an image extension, so "Photos" searches documents
    with `type:image` added - unless the line already names a type, which
    the person's own words win over.
    """
    text = str(query or "")
    if bucket == "mail":
        return "mail", text
    if bucket == "code":
        return "code", text
    if bucket == "files":
        return "documents", text
    if bucket == "photos":
        lowered = text.lower()
        if "type:" in lowered or "/type" in lowered:
            return "documents", text
        return "documents", f"{text} type:image".strip()
    return "all", text


# ---------------------------------------------------------------------------
# What a row says
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RowLines:
    """One result, as the box draws it."""

    title: str
    subtitle: str
    when: str
    bucket: str
    #: `doc` / `mail` / `code` / `other` - the colour family (`kind_badge`).
    family: str
    #: The full path or the message's subject and sender, for the tooltip.
    tip: str


def is_message_group(group: Any, detail: Optional[Mapping[str, Any]] = None) -> bool:
    """A message itself (not an attachment): the row is called by its subject."""
    if detail and detail.get("attachment_of"):
        return False
    if str(getattr(group, "kind", "") or "") == "email":
        return True
    from app.ui.presenter.opening import inside_mail_archive

    path = str(getattr(group, "path", "") or "")
    return inside_mail_archive(path) and not getattr(group, "is_attachment", False)


def row_label(row: Any) -> str:
    r"""One line for one result: what it is called, and where it lives.

    The list's plain text - what a screen reader reads and what the older
    tests read. The drawn row carries two lines (`row_lines`).
    """
    name = str(getattr(row, "name", "") or "").strip()
    folder = str(getattr(row, "folder", "") or "").strip()
    path = str(getattr(row, "path", "") or "").strip()
    if not name:
        from app.ui.presenter.facts import display_name

        name = display_name("", path)
    return f"{name}   —   {folder}" if folder else name


def row_lines(group: Any, detail: Optional[Mapping[str, Any]] = None) -> RowLines:
    r"""Title, second line and date for one `ResultGroup`.

    * a **message**: its subject, then who sent it; never the `pst://` key;
    * an **attachment**: its own file name, then the message it came with;
    * anything else: its name, then its type and folder.
    """
    from app.core.row_facts import message_name
    from app.ui.kind_badge import family_for
    from app.ui.presenter.facts import display_name
    from app.ui.presenter.formatting import format_address
    from app.ui.presenter.results import kind_tag

    detail = detail or {}
    kind = str(getattr(group, "kind", "") or "")
    path = str(getattr(group, "path", "") or "")
    name = str(getattr(group, "name", "") or "")
    folder = str(getattr(group, "folder", "") or "")
    when = str(getattr(group, "when", "") or "")
    bucket = kind_bucket(kind)

    if is_message_group(group, detail):
        sender = format_address(detail.get("sender"))
        if detail:
            title = message_name(detail.get("subject"))
        else:
            # No details came back with the answer: the name the group was
            # given, unless that is the key - then "Message", as every list says.
            title = display_name("", path) if (not name or path.endswith(name)) else name
        parts = [f"From {sender}" if sender else ""]
        if folder:
            parts.append(folder)
        subtitle = " · ".join(part for part in parts if part)
        tip = " — ".join(part for part in (title, f"from {sender}" if sender else "") if part)
        return RowLines(title, subtitle, when, "mail", "mail", tip)

    title = display_name(name, path, kind)
    tag = kind_tag(kind)
    subtitle = " · ".join(part for part in (tag, folder) if part)
    return RowLines(title, subtitle, when, bucket, family_for(kind),
                    path if "://" not in path else title)


# ---------------------------------------------------------------------------
# The keys, said in the footer
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Hint:
    keys: str
    words: str


def hints(*, mode: str, mail: bool = False, preview: bool = False) -> tuple[Hint, ...]:
    r"""The action hints along the bottom, for what is on screen.

    `mode` is `"results"`, `"recent"` or `"empty"`. Ctrl+O appears only for
    an email, the one row it does something for.
    """
    if mode == "results":
        found = [Hint("Enter", "Open"), Hint("Ctrl+Enter", "Folder"),
                 Hint("Ctrl+C", "Copy path")]
        if mail:
            found.append(Hint("Ctrl+O", "Outlook"))
        found.append(Hint("Ctrl+P", "Preview"))
        found.append(Hint("Esc", "Clear"))
        return tuple(found)
    if mode == "recent":
        return (Hint("↑↓", "Choose"), Hint("Enter", "Search again"), Hint("Esc", "Close"))
    return (Hint("Tab", "Next kind"), Hint("Esc", "Close"))


#: Which hints stay when the footer is too narrow for all of them, first first.
_HINT_PRIORITY = ("Enter", "Esc", "Ctrl+O", "Ctrl+Enter", "Ctrl+P", "Ctrl+C", "↑↓", "Tab")


def fit_hints(found: Sequence[Hint], widths: Sequence[int], budget: int) -> tuple[Hint, ...]:
    r"""The hints that fit in `budget` pixels, in their own order, dropping the
    least needed first - Enter and Esc are the last to go."""
    ranked = sorted(range(len(found)), key=lambda i: (
        _HINT_PRIORITY.index(found[i].keys) if found[i].keys in _HINT_PRIORITY
        else len(_HINT_PRIORITY), i))
    kept: set = set()
    used = 0
    for index in ranked:
        width = int(widths[index]) if index < len(widths) else 0
        if used + width > budget and kept:
            continue
        kept.add(index)
        used += width
    return tuple(found[i] for i in range(len(found)) if i in kept)


def empty_words(has_recent: bool) -> str:
    """Under an empty box with nothing to offer."""
    if has_recent:
        return ""
    return ("Type what you remember - a word from it, who sent it, roughly when. "
            "Type / for filters like from:, after: and has attachment.")


def nothing_found(query: str) -> str:
    text = str(query or "").strip()
    return (f"Nothing found for “{text}”. Try fewer words, or press Enter to "
            "search in the main window.")


# ---------------------------------------------------------------------------
# Where the window sits
# ---------------------------------------------------------------------------

Rect = tuple[int, int, int, int]


def _overlap(a: Rect, b: Rect) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    width = min(ax + aw, bx + bw) - max(ax, bx)
    height = min(ay + ah, by + bh) - max(ay, by)
    return width * height if width > 0 and height > 0 else 0


def default_place(screen: Rect, size: Sequence[int] = DEFAULT_SIZE) -> Rect:
    """Centred across `screen`, a quarter of the way down - where it always opened."""
    sx, sy, sw, sh = screen
    width, height = min(int(size[0]), sw), min(int(size[1]), sh)
    return (sx + (sw - width) // 2, sy + max(0, min(sh // 4, sh - height)), width, height)


def fit_on_screens(saved: Optional[Rect], screens: Sequence[Rect],
                   fallback: int = 0) -> Optional[Rect]:
    r"""The rectangle to show the box in, wholly on a screen that exists.

    `saved` is `(x, y, width, height)` as it was left; `screens` are the
    screens' usable areas now. The screen it overlaps most keeps it; a box
    whose screen has gone - a monitor unplugged - goes to `screens[fallback]`
    at the default place. Then it is made no bigger than that screen and
    moved, not shrunk, until every edge is on it. `None` with no screens.
    """
    if not screens:
        return None
    fallback = fallback if 0 <= fallback < len(screens) else 0
    if saved is None:
        return default_place(screens[fallback])
    x, y, width, height = (int(v) for v in saved)
    width = max(width, MIN_SIZE[0])
    height = max(height, MIN_SIZE[1])
    best = max(range(len(screens)), key=lambda i: _overlap((x, y, width, height), screens[i]))
    if _overlap((x, y, width, height), screens[best]) <= 0:
        return default_place(screens[fallback], (width, height))
    sx, sy, sw, sh = screens[best]
    width, height = min(width, sw), min(height, sh)
    x = min(max(x, sx), sx + sw - width)
    y = min(max(y, sy), sy + sh - height)
    return (x, y, width, height)


def place_text(rect: Rect, *, preview: bool = False, preview_width: int = 0) -> str:
    """What is saved: the rectangle and whether the preview was open."""
    x, y, width, height = (int(v) for v in rect)
    return json.dumps({"x": x, "y": y, "w": width, "h": height,
                       "preview": bool(preview), "pw": int(preview_width or 0)})


def read_place(text: Any) -> tuple[Optional[Rect], bool, int]:
    """`(rect or None, preview, preview_width)` from what `place_text` saved.
    Anything unreadable is "nothing saved", never an error."""
    try:
        found = json.loads(str(text or ""))
        rect = (int(found["x"]), int(found["y"]), int(found["w"]), int(found["h"]))
        if rect[2] <= 0 or rect[3] <= 0:
            return None, False, 0
        return rect, bool(found.get("preview", False)), int(found.get("pw", 0) or 0)
    except Exception:                            # noqa: BLE001 - a remembered place
        return None, False, 0


def recent_wanted(settings: Any, overrides: Optional[Mapping[str, Any]] = None) -> bool:
    r"""The Settings switch "offer recent searches", as it is now - a change
    made in this session (`overrides`, raw `.env` strings) wins. The Search
    tab's own rule (`first_contact.offer_recent`), so the two boxes agree."""
    from app.ui.first_contact import offer_recent

    return offer_recent(settings, dict(overrides) if overrides else None)
