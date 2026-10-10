r"""The Photos page's narrowing, sorting and wording - no Qt, no I/O.

Layer: L5 (presenter)

2026-10-05, the owner: "this photos view should be like other tabs where i can
narrow by year name location etc etc .. also have / commands", and "make it
like a professional photo management/viewer".

**One grammar.** The box reads exactly as every other tab's does - `who:`,
`place:`, `date:`/`after:`/`before:`, `path:`, `type:`, `shows:`, `name:`,
`sort:`, plain words, `-word` - through the same `read_typed`; the Photos tab
adds nothing to it but `only:` (named, unnamed, no-faces, described,
undescribed, text, screenshots), which is a shared command too, so Files and
Search honour it. Clicking a person, a year or a place in the side list writes
the same words into the box (`toggle_in_box`), so the box is always the truth.

**In memory.** Every picture is read once (`SqliteStore.photo_library`, 0.11 s
for the owner's 15,010) and `narrow` works on that list, so a click narrows at
once without another query.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from pathlib import PurePath
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "narrow", "facets", "sort_rows", "SORTS", "KINDS", "COLUMNS", "PHOTOS_COMMANDS",
    "month_heading", "date_text", "size_text", "people_text", "column_text",
    "summary", "year_of", "toggle_in_box", "box_has", "words_of_row", "only_matches",
    "from_mail", "in_scope", "arrange",
]

#: The switches the Photos box offers when "/" is typed - the shared ones a
#: picture can answer, in the order a photo manager would reach for them.
PHOTOS_COMMANDS: tuple[str, ...] = (
    "who", "date", "place", "only", "shows", "after", "before", "between",
    "path", "name", "type", "size", "sort",
)

#: `only:` values, as the side list shows them: `(value, label)`.
KINDS: tuple[tuple[str, str], ...] = (
    ("named", "People named"),
    ("unnamed", "Faces not yet named"),
    ("no-faces", "No faces"),
    ("described", "Described"),
    ("undescribed", "Not yet described"),
    ("text", "Has text"),
    ("screenshots", "Screenshots and scans"),
    # 2026-10-07, the owner: "can the pictures from mail not be in the pictures
    # tab" - they are left out unless this is ticked (see `narrow`).
    ("mail", "Pictures from mail"),
)

#: `(key, label)` - the sort menu; `sort:` in the box wins over it.
SORTS: tuple[tuple[str, str], ...] = (
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("name", "Name"),
    ("size", "Size, largest first"),
    ("place", "Place"),
)

#: The Details view's columns: `(key, heading)`.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "Name"),
    ("date", "Date taken"),
    ("people", "People"),
    ("place", "Place"),
    ("shows", "Shows"),
    ("type", "Type"),
    ("size", "Size"),
    ("folder", "Folder"),
)

#: Folder or file names that mark a screenshot without reading it - the same
#: words `storage.filters.ONLY_SQL["screenshots"]` looks for.
_SCREENSHOT_WORDS = ("screenshot", "screen shot", "snip")


# --- reading a row ---------------------------------------------------------------

def _moment(row: Any) -> Optional[_dt.datetime]:
    """The row's shown date as a local datetime, or None."""
    when = row.when_ns
    if not when:
        return None
    try:
        return _dt.datetime.fromtimestamp(when / 1e9)
    except (OverflowError, OSError, ValueError):
        return None


def year_of(row: Any) -> Optional[int]:
    moment = _moment(row)
    return moment.year if moment else None


def from_mail(row: Any) -> bool:
    """Whether a picture arrived attached to a message, rather than being a
    photo on disk. A rule about its key - no I/O."""
    from app.core.row_facts import attachment_of

    return bool(attachment_of(getattr(row, "path", ""))[0])


def in_scope(rows: Sequence[Any], parsed: Any) -> int:
    """How many of `rows` the page is choosing from: the photos on disk, or -
    with `only:mail` in the box - the pictures from mail. The "of N" in the
    line under the photos, so it never counts what the page is not showing."""
    wants_mail = MAIL_KIND in tuple(getattr(parsed, "only", ()) or ())
    return sum(1 for row in rows if from_mail(row) == wants_mail)


def arrange(rows: Sequence[Any], parsed: Any, words: str,
            sort_key: str) -> tuple[list, str, int, int]:
    """The pictures the box lets through, in the order asked, with the order and the
    two counts the summary line shows. **Runs on a worker** - the Photos tab hands it
    to one, because until 2026-10-09 the window's thread did this on every search
    over 46,000 pictures and stopped answering.

    2026-10-10: moved here from `photos_view._arrange`. It is pure - no Qt, no I/O -
    so it belongs with the rest of the page's logic, and the view went over the 250
    lines `test_presenter`'s guard allows a view (a long view is where untested logic
    hides).
    """
    shown = narrow(rows, parsed, words)
    order = getattr(parsed, "sort", "") or sort_key
    return sort_rows(shown, order), order, len(shown), in_scope(rows, parsed)


#: The `only:` value that brings pictures from mail in. Without it they are
#: left out of the page altogether: most are logos, signatures and scans.
MAIL_KIND = "mail"


def only_matches(row: Any, value: str) -> bool:
    """Whether `row` has what `only:<value>` asks for."""
    if value == MAIL_KIND:
        return from_mail(row)
    if value == "named":
        return bool(row.people)
    if value == "unnamed":
        return row.faces > len(row.people)
    if value == "no-faces":
        return row.faces == 0
    if value == "described":
        return bool(row.described)
    if value == "undescribed":
        return not row.described
    if value == "text":
        return bool(row.has_text)
    if value == "screenshots":
        lowered = str(row.path).casefold()
        return bool(row.page_like) or any(w in lowered for w in _SCREENSHOT_WORDS)
    return True                                   # an unknown word narrows nothing


def words_of_row(row: Any) -> str:
    """What plain words are matched against: the path, the people, the place
    and what the picture shows."""
    return " ".join((str(row.path), " ".join(row.people), row.place or "",
                     " ".join(row.tags))).casefold()


def _starts(values: Iterable[str], wanted: str) -> bool:
    return any(v.casefold().startswith(wanted) for v in values)


def _in_dates(row: Any, after: Any, before: Any) -> bool:
    """Whether the row's date lies within `after`/`before` (dates or datetimes)."""
    if not (after or before):
        return True
    moment = _moment(row)
    if moment is None:
        return False
    for bound, is_after in ((after, True), (before, False)):
        if not bound:
            continue
        if isinstance(bound, _dt.datetime):
            ok = moment >= bound if is_after else moment <= bound
        else:
            ok = moment.date() >= bound if is_after else moment.date() <= bound
        if not ok:
            return False
    return True


def _size_ok(row: Any, sizes: Sequence[tuple[str, int]]) -> bool:
    """Whether the row's size satisfies every `size:` comparison."""
    for op, amount in sizes:
        if op in (">", ">=") and not row.size_bytes >= amount:
            return False
        if op in ("<", "<=") and not row.size_bytes <= amount:
            return False
    return True


def narrow(rows: Sequence[Any], parsed: Any, words: str = "") -> list[Any]:
    """The rows `parsed` (a `ParsedQuery`) lets through, in the order given.

    Each switch means here what it means on every tab: within one switch the
    values are alternatives (`who:Jason,Sarita`), between switches all hold.
    `words` are the plain words left (`run.words_of`), each of which must
    appear; `-word` excludes."""
    g = lambda name: tuple(getattr(parsed, name, ()) or ())  # noqa: E731
    who, place, shows, paths = g("who"), g("place"), g("shows"), g("paths")
    ext, names, only = g("ext"), g("names"), g("only")
    not_who, not_place, not_shows = g("not_who"), g("not_place"), g("not_shows")
    not_paths, not_ext, not_names, not_only = (g("not_paths"), g("not_ext"),
                                               g("not_names"), g("not_only"))
    excluded = [w.casefold() for w in g("excluded")] + [p.casefold() for p in g("not_phrases")]
    sizes = g("sizes")
    after, before = getattr(parsed, "after", None), getattr(parsed, "before", None)
    terms = [w for w in str(words or "").casefold().split() if w]
    wants_mail = MAIL_KIND in only

    out = []
    for row in rows:
        # 2026-10-07: pictures from mail are off unless asked for by name.
        if not wants_mail and from_mail(row):
            continue
        people = row.people
        if who and not any(_starts(people, w) for w in who):
            continue
        if not_who and any(_starts(people, w) for w in not_who):
            continue
        row_place = (row.place or "").casefold()
        if place and not any(p in row_place for p in place if row_place):
            continue
        if not_place and row_place and any(p in row_place for p in not_place):
            continue
        if shows and not any(_starts(row.tags, s) for s in shows):
            continue
        if not_shows and any(_starts(row.tags, s) for s in not_shows):
            continue
        lowered_path = str(row.path).casefold()
        if paths and not any(p.casefold() in lowered_path for p in paths):
            continue
        if not_paths and any(p.casefold() in lowered_path for p in not_paths):
            continue
        row_ext = str(row.ext).lower()
        if ext and row_ext not in ext:
            continue
        if not_ext and row_ext in not_ext:
            continue
        file_name = PurePath(row.path).name.casefold()
        if names and not any(n in file_name for n in names):
            continue
        if not_names and any(n in file_name for n in not_names):
            continue
        if only and not all(only_matches(row, v) for v in only):
            continue
        if not_only and any(only_matches(row, v) for v in not_only):
            continue
        if not _in_dates(row, after, before) or not _size_ok(row, sizes):
            continue
        if terms or excluded:
            hay = words_of_row(row)
            if not all(t in hay for t in terms) or any(x in hay for x in excluded):
                continue
        out.append(row)
    return out


def facets(rows: Iterable[Any]) -> dict[str, Counter]:
    """How many of `rows` each person, year, place, kind and type has - the
    numbers beside every entry in the side list."""
    found: dict[str, Counter] = {k: Counter() for k in
                                 ("people", "years", "places", "kinds", "types")}
    for row in rows:
        # 2026-10-07: a picture from mail is counted on its own line and
        # nowhere else - the other numbers are of what the page shows.
        if from_mail(row):
            found["kinds"][MAIL_KIND] += 1
            continue
        for person in row.people:
            found["people"][person] += 1
        year = year_of(row)
        if year is not None:
            found["years"][year] += 1
        if row.place:
            found["places"][row.place] += 1
        found["types"][str(row.ext).lower()] += 1
        for key, _label in KINDS:
            if only_matches(row, key):
                found["kinds"][key] += 1
    return found


def sort_rows(rows: Sequence[Any], key: str) -> list[Any]:
    """The rows in the order `key` names (`SORTS`); newest first for any other key."""
    if key == "oldest":
        return sorted(rows, key=lambda r: r.when_ns)
    if key == "name":
        return sorted(rows, key=lambda r: PurePath(r.path).name.casefold())
    if key == "size":
        return sorted(rows, key=lambda r: r.size_bytes, reverse=True)
    if key == "place":
        return sorted(rows, key=lambda r: ((r.place or "~").casefold(), -r.when_ns))
    return sorted(rows, key=lambda r: r.when_ns, reverse=True)


# --- the box -------------------------------------------------------------------------

def _quoted(value: str) -> str:
    value = str(value)
    return f'"{value}"' if any(c.isspace() for c in value) or "," in value else value


def box_has(text: str, op: str, value: Any) -> bool:
    """Whether the box already holds exactly `op:value`."""
    from app.ui.chips_logic import chips_for

    wanted = str(value).casefold()
    return any(chip.op == op and chip.value.strip('"').casefold() == wanted
               and not chip.negated for chip in chips_for(text))


def toggle_in_box(text: str, op: str, value: Any) -> str:
    """The box with `op:value` added - or taken out, when it is there. A side
    list click and a typed switch are then one and the same thing."""
    from app.ui.chips_logic import chips_for, without

    wanted = str(value).casefold()
    for chip in chips_for(text):
        if chip.op == op and chip.value.strip('"').casefold() == wanted and not chip.negated:
            return without(text, chip)
    token = f"{op}:{_quoted(value)}"
    return f"{text.rstrip()} {token}".strip()


# --- wording -----------------------------------------------------------------------------

def month_heading(when_ns: int) -> str:
    """`"June 2023"` for a date in nanoseconds; "No date" for none or unreadable."""
    if not when_ns:
        return "No date"
    try:
        return _dt.datetime.fromtimestamp(when_ns / 1e9).strftime("%B %Y")
    except (OverflowError, OSError, ValueError):
        return "No date"


def date_text(row: Any) -> str:
    """`12 Jun 2023, 14:05`; a date guessed from the folder starts with `about`,
    and a picture with no date taken says it is the file's own date."""
    moment = _moment(row)
    if moment is None:
        return ""
    text = f"{moment.day} {moment:%b %Y, %H:%M}"
    if not row.taken_at_ns:
        return text + " (file date)"
    if row.taken_is_hint:
        # A guess from a folder name ("2022", "Summer 2019") knows a year, or
        # a month - never the hour it would otherwise print.
        utc = _dt.datetime.fromtimestamp(row.taken_at_ns / 1e9, _dt.timezone.utc)
        if (utc.month, utc.day, utc.hour, utc.minute) == (1, 1, 0, 0):
            return f"about {utc.year}"                   # a folder's year
        # 2026-10-05: a date from the file's own name (`era_hints.guess_moment`)
        # - a day, often with its time.
        if (moment.hour, moment.minute, moment.second) == (0, 0, 0):
            return f"about {moment.day} {moment:%b %Y}"
        return f"about {text}"
    return text


def size_text(size: int) -> str:
    """Bytes as GB, MB, KB or bytes for the Details view."""
    size = int(size or 0)
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} GB"
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} MB"
    if size >= 1 << 10:
        return f"{size / (1 << 10):.0f} KB"
    return f"{size} bytes"


def people_text(row: Any) -> str:
    """The named people, then how many faces are still unnamed."""
    named = ", ".join(row.people)
    extra = row.faces - len(row.people)
    if extra > 0:
        return f"{named} + {extra} not named" if named else f"{extra} face(s), not named"
    return named


def column_text(row: Any, key: str) -> str:
    """The text of one Details-view cell, by column key (`COLUMNS`)."""
    path = PurePath(row.path)
    if key == "name":
        return path.name
    if key == "date":
        return date_text(row)
    if key == "people":
        return people_text(row)
    if key == "place":
        return row.place or ""
    if key == "shows":
        return ", ".join(row.tags[:6])
    if key == "type":
        return str(row.ext).upper()
    if key == "size":
        return size_text(row.size_bytes)
    if key == "folder":
        return str(path.parent)
    return ""


def summary(shown: int, total: int, selected: int = 0) -> str:
    """The status line under the photos."""
    text = f"{shown:,} photo(s)" if shown == total else f"{shown:,} of {total:,} photo(s)"
    if selected:
        text += f" · {selected:,} selected"
    return text
