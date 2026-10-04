"""Search results as rows and groups, and the words drawn around them.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.core.code_types import extensions_for as _code_extensions_for
from app.search.run import GROUP_FETCH_MULTIPLIER, fetch_depth  # noqa: F401 - re-exported
from app.ui.presenter.explain import match_marker
from app.ui.presenter.facts import (
    attachment_context,
    attachment_words,
    date_words,
    message_name,
    shown_date_ns,
    volume_folder,
)
from app.ui.presenter.formatting import (
    BREADCRUMB_PARTS,
    _exact_date,
    breadcrumb,
    format_address,
    shorten_path,
)
from app.ui.presenter.offline import online_only_note
from app.ui.presenter.snippets import Snippet, build_snippet


@dataclass
class ResultRow:
    """One search result, formatted for display."""

    rank: int
    chunk_id: int
    file_id: int
    path: str
    display_path: str
    snippet: Snippet
    explain: str
    location: str = ""
    score: float = 0.0
    #: Straight through from `SearchResult`, which now carries both. Kept on the
    #: row rather than only on the group so a flat list can show them too.
    #:
    #: **And `to_row` did not actually copy them, for the life of the
    #: feature.** The comment above said "straight through" and the
    #: constructor omitted both, so `ResultGroup.when` - built from
    #: `rows[0].mtime_ns` - was `format_when(0)`, which is the empty string.
    #: **Every document result showed no date at all.** Mail was unaffected
    #: and hid it: a message takes its date from `sent_at` in the details map,
    #: so the Mail tab looked right while the other three quietly did not.
    ext: str = ""
    mtime_ns: int = 0
    #: Which retrievers found this, straight from `SearchResult.sources`.
    #:
    #: **Carried as data, not parsed back out of `explain`.** The rule this
    #: codebase set for notices - the UI never reads a message string to
    #: decide anything - applies just as well to a row deciding whether to
    #: show a "meaning match" marker.
    sources: tuple = ()
    #: The raw locator - `Q3!A14` - kept beside the sentence in `location`.
    #: Adoptions §6a. The grid preview will need the machine-readable form to
    #: scroll to the region, and re-parsing the sentence to get it back would
    #: be the mistake `cell_location` exists to prevent.
    label: str = ""
    #: Offline Media §3a/3b. `None` for an ordinary file. Straight through
    #: from `SearchResult` - see that field's own comment for why `path` on
    #: such a row is never a real filesystem path and must be resolved
    #: before it is opened.
    volume_id: Optional[int] = None
    relative_path: str = ""
    #: Adoptions section 1: what `why_result` reads to say, in plain words,
    #: why this row is on the page. Carried as data straight from
    #: `SearchResult`, the same rule `sources` follows - the explanation is
    #: built from recorded facts, never re-derived from the sentence in
    #: `explain`.
    text: str = ""
    recency: float = 0.0
    declares: bool = False
    rerank_score: Optional[float] = None
    #: 2026-10-04, the owner ("the same code should run"): what the row's
    #: group calls it and where it says it is, stamped by `_build_group`, so
    #: the preview pane's heading and facts and a pinned result read the same
    #: as the list - never the `pst://` key a message's `path` is.
    name: str = ""
    folder: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank, "chunk_id": self.chunk_id, "path": self.path,
            "display_path": self.display_path, "snippet": self.snippet.text,
            "highlights": [list(h) for h in self.snippet.highlights],
            "explain": self.explain, "location": self.location,
        }


def cell_location(locator: Any) -> str:
    r"""`Q3!D14` as a sentence: *Sheet 'Q3' · near D14*. Adoptions §6a.

    **The store keeps the code and the words are written here**, the same rule
    the notices follow - nothing anywhere has to parse a sentence back apart,
    and the wording can be changed without a migration.

    *near*, not *at*. The locator is the row the chunk **starts** on, and a
    passage is several rows long; saying "at" would be a precision the value
    does not have, on the one screen where a person is deciding whether to
    open a forty-thousand-row workbook.

    `""` for anything that is not a locator, which is every document that is
    not a spreadsheet.
    """
    from app.extract.cells import parse

    found = parse(locator)
    if found is None:
        return ""
    sheet, column, row = found
    return f"Sheet '{sheet}' · near {column}{row}"


def to_row(result: Any, terms: Sequence[str], *, path_limit: int = 70) -> ResultRow:
    """Turn a `SearchResult` into something a list widget can draw."""
    page = getattr(result, "page", None)
    # **The cell wins over the sheet number.** For a spreadsheet `page` is the
    # sheet *index*, so "page 3" is both true and useless - it names a thing
    # nobody's spreadsheet calls a page and gives no way to find the row.
    location = cell_location(getattr(result, "label", ""))
    if not location:
        # Work order 202626270515: a transcript or on-screen-text hit in a video
        # or recording says *when* - `at 12:41` - from the same `label` column.
        from app.extract.timecode import describe_timecode

        location = describe_timecode(getattr(result, "label", ""))
    if not location:
        location = f"page {page}" if page is not None else ""

    return ResultRow(
        rank=getattr(result, "rank", 0),
        chunk_id=getattr(result, "chunk_id", 0),
        file_id=getattr(result, "file_id", 0),
        path=getattr(result, "path", ""),
        display_path=shorten_path(getattr(result, "path", ""), limit=path_limit),
        snippet=build_snippet(getattr(result, "text", ""), terms),
        explain=result.explain() if hasattr(result, "explain") else "",
        location=location,
        score=float(getattr(result, "score", 0.0) or 0.0),
        ext=str(getattr(result, "ext", "") or ""),
        # **The shot date before the copy date - work order 0f §3a's third
        # clause.** `ResultRow.mtime_ns` is what every date this row shows
        # (`_build_group`'s `when`/`when_exact`, the tooltip in
        # `result_tooltip`) is formatted from, so it has to already be the
        # right date rather than something a later step corrects - the field
        # keeps its name because renaming it would ripple through every one
        # of those, and it already means "the date to show" nowhere else.
        mtime_ns=int(getattr(result, "taken_at_ns", 0) or 0)
                 or int(getattr(result, "mtime_ns", 0) or 0),
        sources=tuple(getattr(result, "sources", ()) or ()),
        label=str(getattr(result, "label", "") or ""),
        text=str(getattr(result, "text", "") or ""),
        recency=float(getattr(result, "recency", 0.0) or 0.0),
        declares=bool(getattr(result, "declares", False)),
        rerank_score=getattr(result, "rerank_score", None),
        # **Copied, not just declared** - the same omission `ext` and `mtime_ns`
        # once had. `ResultRow.volume_id` existed for Offline Media and
        # nothing filled it, so every row on a catalogued drive looked like an
        # ordinary file: the preview tried to read its letter-free synthetic
        # path and said "no longer where it was indexed" instead of showing the
        # text the index holds, and Open/Reveal could not resolve it either.
        volume_id=(int(result.volume_id) if getattr(result, "volume_id", None) is not None
                   else None),
        relative_path=str(getattr(result, "relative_path", "") or ""),
    )


def to_rows(results: Iterable[Any], terms: Sequence[str], **kwargs: Any) -> list[ResultRow]:
    return [to_row(result, terms, **kwargs) for result in results]


# ---------------------------------------------------------------------------
# One row per document, not one per chunk
#
# **The problem this solves.** Results are chunk-level, and nothing grouped
# them, so a long PDF matching in five places took five of the top ten rows.
# The person saw three documents where they should have seen ten.
#
# **Grouping lives here and never in the engine.** That is not a stylistic
# preference. The measured baseline in HANDOFF.md §3b - 75% at rank 1 after
# translation - was taken against chunk-level ranking. Grouping inside the
# engine would change what "rank 1" means and make every future measurement
# incomparable with that one, silently. `SearchEngine` keeps returning exactly
# what it returns today; this decides how to draw it.
# ---------------------------------------------------------------------------

#: `GROUP_FETCH_MULTIPLIER` and `fetch_depth` - how many chunks to fuse before
#: grouping - are defined once in `app.search.run` since 2026-10-04, where the
#: command line and the MCP server group by the same rule; imported above.


@dataclass(frozen=True, slots=True)
class ResultGroup:
    """Every matching chunk of one document, as a single row."""

    file_id: int
    #: Filename for a file; the subject for a message.
    name: str
    #: A breadcrumb, not a raw path. The full path stays on `path` for the
    #: tooltip, for opening, and for "Copy path" - shortening is a display
    #: choice and must never be the only copy of the truth.
    folder: str
    kind: str
    when: str
    path: str
    #: Every matching chunk, best first.
    rows: list[ResultRow] = field(default_factory=list)
    #: The precise date `when` may have blurred into "yesterday" or "3 weeks
    #: ago" - item 4b. Always computed, whatever register `when` itself is
    #: reading in, so the tooltip can show it regardless.
    when_exact: str = ""
    #: `(start, end)` into `folder` - item 4c. `(0, 0)` means nothing to
    #: emphasise: the common case, where no other result in the set shares
    #: this group's name. Set only by `group_results`, over the whole result
    #: set already in hand - never a second query.
    folder_emphasis: tuple[int, int] = (0, 0)
    #: §5b: this row's file is an email attachment, not the message itself.
    #: **The attachment is the object, its message is the context** - `name`
    #: stays the attachment's own filename (unlike a message, whose path is a
    #: synthetic key nobody would recognise) and `folder` carries who sent it
    #: and what it was about instead of a breadcrumb through a `pst://` key
    #: nobody typed. `kind` is untouched too, deliberately: it is the
    #: attachment's own real extension, so the icon painted is the document's
    #: actual type rather than a generic "attachment" glyph.
    is_attachment: bool = False

    @property
    def best(self) -> Optional[ResultRow]:
        """The chunk shown while collapsed."""
        return self.rows[0] if self.rows else None

    @property
    def score(self) -> float:
        """**The best chunk's score, never the mean.**

        Averaging punishes a long document that matches strongly in one place -
        which is the common case in an archive, and precisely the document
        somebody is looking for.
        """
        return self.rows[0].score if self.rows else 0.0

    @property
    def match_count(self) -> int:
        return len(self.rows)

    @property
    def match_label(self) -> str:
        """Shown only when there is more than one. "1 match" is noise on every
        row of a list where one is the normal case."""
        return f"{self.match_count} matches" if self.match_count > 1 else ""


def group_results(
    rows: Sequence[ResultRow],
    *,
    limit: int = 0,
    details: Optional[Mapping[int, Mapping[str, Any]]] = None,
    now: Optional[float] = None,
    register: str = "plain",
    conversations: bool = False,
) -> list[ResultGroup]:
    """Chunk rows to document groups, ordered by each group's best chunk.

    `rows` must already be in rank order - the engine's order is preserved
    rather than recomputed, which is what keeps this display-only.

    `details` optionally maps `file_id` to mail metadata (see
    `store.messages_for`), so a message can use its subject rather than a
    synthetic path nobody would recognise. Absent, or missing an entry, falls
    back to the filename without raising.

    `register` gates `when` between "yesterday"/"3 weeks ago" (`"plain"`, the
    default) and an exact date (`"technical"`) - item 4b. Whichever it picks,
    `ResultGroup.when_exact` always carries the exact date, for the tooltip.

    `conversations` (order 0z F2, the View menu's "One row per conversation")
    folds every message of one conversation, and every attachment of those
    messages, into one group - keyed by `details[file_id]["conversation"]`,
    which for an attachment is its parent message's. The group is headed by
    its best-ranked passage, as a document's is; when that passage is in an
    attachment, the group is drawn as the **message** it is attached to, with
    the attachment named beside it (`_as_conversation`). A file that is not
    mail, or mail with no conversation recorded, groups by itself as before.
    """
    details = details or {}

    def key_of(row: ResultRow) -> Any:
        detail = details.get(row.file_id) if conversations else None
        conversation = (detail or {}).get("conversation")
        return ("conversation", str(conversation)) if conversation else row.file_id

    # The grouping rule itself - first appearance decides position, so the
    # best-ranked chunk of a document decides where the document sits - is
    # `app.search.run.group_by_document` since 2026-10-04, shared with the
    # command line and the MCP server. Only the drawing is decided here.
    from app.search.run import group_by_document

    groups = []
    for found in group_by_document(rows, key_of):
        head = found[0].file_id
        group = _build_group(head, found, details.get(head), now=now, register=register)
        if isinstance(key_of(found[0]), tuple):
            group = _as_conversation(group, details)
        groups.append(group)
    groups = _distinguish_twins(groups)
    return groups[:limit] if limit else groups


def _as_conversation(group: ResultGroup, details: Mapping[int, Mapping[str, Any]]) -> ResultGroup:
    """Order 0z F2: one group standing for a conversation's matching messages.

    Two things change from a one-document group, both in what the row *says*;
    its passages, its rank and what opening it does are untouched.

    * **The parent message is shown when only an attachment matched.** A group
      headed by an attachment is named for the message it is attached to -
      "Dave - School trip" - with the attachment's own name on the grey line,
      because in a list of conversations the message is what is recognised.
    * **It says how many messages it stands for**, so folding hides nothing:
      the count is of the messages (and attachments' messages) with a passage
      in this group.
    """
    head = details.get(group.file_id) or {}
    folder, name, kind = group.folder, group.name, group.kind
    if group.is_attachment:
        subject = message_name(head.get("subject"))
        sender = format_address(head.get("sender"))
        folder = f"in the attachment {group.name}"
        name = f"{sender} — {subject}" if sender else subject
        kind = "email"
    messages = {int((details.get(row.file_id) or {}).get("attachment_of") or row.file_id)
                for row in group.rows}
    if len(messages) > 1:
        note = f"{len(messages):,} messages of this conversation matched"
        folder = f"{folder} · {note}" if folder else note
    return replace(group, name=name, folder=folder, kind=kind)


def _path_pieces(path: str) -> list[str]:
    """A path's folder segments, root to leaf, drive letter dropped."""
    cleaned = (path or "").replace("\\", "/").strip("/")
    pieces = [piece for piece in cleaned.split("/") if piece]
    if pieces and pieces[0].endswith(":"):
        pieces = pieces[1:]
    return pieces[:-1] if len(pieces) > 1 else []


def _distinguish_twins(groups: list[ResultGroup]) -> list[ResultGroup]:
    """Item 4c: when two groups in one set share a display name, extend the
    shorter of their breadcrumbs until they read differently, and mark where
    the newly-added, distinguishing segment starts.

    Computed once over the whole set already in hand - zero extra queries,
    the same discipline `group_results` already keeps. A name that appears
    once is untouched: this is the rare case, not the common one.
    """
    by_name: dict[str, list[int]] = {}
    for index, group in enumerate(groups):
        by_name.setdefault(group.name.strip().lower(), []).append(index)

    twins = {index: [j for j in indices if j != index]
            for indices in by_name.values() if len(indices) > 1
            for index in indices}
    if not twins:
        return groups

    out = list(groups)
    for index, rivals in twins.items():
        group = out[index]
        pieces = _path_pieces(group.path)
        rival_pieces = [_path_pieces(out[j].path) for j in rivals]
        folder, emphasis = _twin_breadcrumb(pieces, rival_pieces)
        if folder:
            out[index] = replace(group, folder=folder, folder_emphasis=emphasis)
    return out


def _twin_breadcrumb(pieces: list[str],
                     rival_pieces: list[list[str]]) -> tuple[str, tuple[int, int]]:
    """The breadcrumb for one twin, extended only as far as it needs to read
    differently from every rival's own folder segments - item 4c.

    Returns `(text, (start, end))`; `(0, 0)` means the ordinary
    `BREADCRUMB_PARTS`-deep breadcrumb already told the two apart, so there is
    nothing new to draw attention to.
    """
    if not pieces:
        return "", (0, 0)
    for depth in range(min(BREADCRUMB_PARTS, len(pieces)), len(pieces) + 1):
        tail = pieces[-depth:]
        # A rival shallower than this depth cannot have the same tail at this
        # depth at all, so it can never collide - only a rival at least this
        # deep needs comparing.
        if all(rival[-depth:] != tail for rival in rival_pieces if len(rival) >= depth):
            prefix = "… > " if depth < len(pieces) else ""
            text = prefix + " > ".join(tail)
            if depth == min(BREADCRUMB_PARTS, len(pieces)):
                return text, (0, 0)          # already distinct at the usual depth
            return text, (len(prefix), len(prefix) + len(tail[0]))
    # Identical all the way to the root - genuinely the same folder. Show the
    # full path's worth of breadcrumb; there is no single segment to point at.
    text = " > ".join(pieces)
    return text, (0, 0)


def _build_group(
    file_id: int,
    rows: list[ResultRow],
    detail: Optional[Mapping[str, Any]],
    *,
    now: Optional[float] = None,
    register: str = "plain",
) -> ResultGroup:
    path = rows[0].path if rows else ""
    name = path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path
    folder = breadcrumb(path[: len(path) - len(name)])
    kind = (rows[0].ext if rows else "") or _ext_of(name)
    detail = detail or {}
    # 2026-10-04, the owner: one function per fact, on every list
    # (`presenter.facts`). The date is `shown_date_ns` - the message's sent
    # date for a message or an attachment, else the shot date, else the file's
    # - read by `date_words` in the Search tab's register; the Files and Code
    # lists call the same two.
    sent = detail.get("sent_at")
    shown_ns = shown_date_ns(mtime_ns=rows[0].mtime_ns if rows else 0, sent_at=sent)
    when_exact = _exact_date(shown_ns)
    when = date_words(shown_ns, register=register, now=now)

    is_attachment = bool(detail.get("attachment_of"))
    is_message = not is_attachment and bool(detail) and "volume_label" not in detail

    if is_attachment:
        # §5b: **the attachment is the object, its message is the context.**
        # `name` and `kind` are left exactly as computed above - the
        # attachment's own filename and real extension, the same as any
        # other file result - and only `folder` changes, from a breadcrumb
        # through a `pst://…/attachments/…` key nobody typed to who sent it
        # and what it was about. This is the *parent* message's detail
        # (`mail_details` resolved it that way), never this row's own. The
        # date is the message's sent date (above): the attachment file's own
        # time is its archive's, which tells nobody anything.
        folder = attachment_context(detail.get("sender"), detail.get("subject"))
    elif is_message:
        # A message: its path is a synthetic key nobody typed and nobody would
        # recognise, so the subject is the only usable name.
        subject = message_name(detail.get("subject"))
        sender = format_address(detail.get("sender"))
        # **Item 3b: sender-first.** "Mum — Re: holiday photos" is how people
        # remember mail, not "Re: holiday photos" with the sender relegated to
        # the grey line underneath. Display order only - `folder` no longer
        # repeats the sender it now leads the name with, but grouping, the
        # payload and every action stay exactly what they were.
        name = f"{sender} — {subject}" if sender else subject
        # 2026-10-04: the real number where the message's stored headers say
        # it, "attachments" where only the flag does - it said "1 attachment"
        # for every message with any.
        folder = attachment_words(_attachment_count(rows),
                                  has_attach=bool(detail.get("has_attach")))
        kind = "email"
    elif rows and rows[0].volume_id is not None:
        # A file on a catalogued drive: the drive's name and the folder on
        # it, the way the Files list says it - not "3 > Photos" read out of
        # the `leasha-volume://3/...` key.
        folder = volume_folder(detail.get("volume_label"), rows[0].relative_path,
                               volume_id=rows[0].volume_id)

    # Stamped on this file's passages, so whatever is handed one of them -
    # the preview pane, a pin - shows this group's name, folder and date.
    # Only this file's: a folded conversation holds other messages' too.
    for row in rows:
        if row.file_id == file_id:
            row.name, row.folder = name, folder
            row.mtime_ns = shown_ns or row.mtime_ns

    return ResultGroup(
        file_id=file_id, name=name, folder=folder, kind=kind,
        when=when, path=path, rows=rows, when_exact=when_exact,
        is_attachment=is_attachment,
    )


def _attachment_count(rows: Sequence[ResultRow]) -> Optional[int]:
    """How many files the message's stored headers name, when a passage on
    hand is that header block; `None` when none is - no second query."""
    from app.ui.presenter.mail import split_index_headers

    for row in rows:
        headers, _rest = split_index_headers(row.text)
        if headers:
            named = [part for part in headers.get("Attachments", "").split(", ") if part.strip()]
            return len(named) or None
    return None


def _ext_of(name: str) -> str:
    return name.rpartition(".")[2].lower() if "." in name else ""


#: A short tag per kind, rather than an icon font or bundled SVGs.
#:
#: Text survives dark mode, high-DPI and a missing font file, all of which an
#: icon set has to be got right for - and none of which is worth spending on
#: before anybody has said the tags are insufficient.
KIND_LABELS = {
    "email": "MAIL",
    "pdf": "PDF",
    "docx": "DOC", "doc": "DOC", "odt": "DOC", "rtf": "DOC",
    "xlsx": "XLS", "xls": "XLS", "ods": "XLS", "csv": "CSV",
    "pptx": "PPT", "ppt": "PPT", "odp": "PPT",
    "txt": "TXT", "md": "TXT", "log": "TXT",
    # Work order 202626270515: what a hit in a video or recording is.
    **{ext: "VID" for ext in (
        "mp4", "m4v", "mov", "mkv", "avi", "wmv", "webm", "mpg", "mpeg",
        "3gp", "flv", "m2ts")},
    **{ext: "AUD" for ext in (
        "mp3", "m4a", "wav", "flac", "ogg", "oga", "opus", "aac", "wma")},
}


def row_identity(payload: Any) -> Any:
    """A stable identity for a row or group - item 5d's stable-update rule.

    "The row under the pointer must not visibly jump" needs to find *the same
    payload* again after a rebuild adds rows or re-ranks them, which a screen
    position or a model row index cannot do - both change on every rebuild by
    definition. A `ResultGroup`'s `file_id` survives a re-rank; a chunk row
    (expanded inside a group) is identified by its own `chunk_id`, since two
    chunks of the same document share a `file_id`.
    """
    if isinstance(payload, ResultGroup):
        return ("group", payload.file_id)
    return ("chunk", getattr(payload, "chunk_id", None))


def kind_tag(kind: str) -> str:
    """Four characters at most, so an unknown type still gets a legible tag.

    2026-10-04, the owner: **the one type badge** - Files, Code, Search and
    the preview pane all show this (Files used to say "DOCX", Code "docx").
    Read case-blind and without a dot, so "DOCX", ".docx" and "docx" are one
    kind. A file with no extension has no badge, on every list: `""`.
    """
    key = str(kind or "").strip().lstrip(".").lower()
    if not key:
        return ""
    return KIND_LABELS.get(key, key.upper()[:4])


#: Extensions painted in monospace - item 3c. Deliberately narrower than
#: `preview_loader`'s text-file list: that one decides what can be *read* at
#: all, this one decides what reads like *code* to the form coders already
#: use everywhere else. `.txt`/`.md`/`.csv` are prose and data, not source,
#: and stay in the ordinary body font.
#: 2026-10-04, code review: the Code tab's "Source code only" list
#: (`code_types`, 311 types) rather than a 33-type copy of part of it, so a
#: row is code - badge, monospace, "opens at its line" - by the same rule
#: everywhere. Every one of the 33 is in it, and no prose or data type is.
CODE_EXTENSIONS = _code_extensions_for("source") or frozenset()


def is_code_kind(kind: str) -> bool:
    """Whether `kind` (a `ResultGroup.kind` or `ResultRow.ext`) is source code.

    Item 3c: **only the font changes here.** A line number was asked for
    alongside the monospace font and is deliberately not shown - see the
    dated note on item 3c in the work order. Nothing from extraction through
    to a `SearchResult` carries a line number today, chunk-relative or
    absolute, and painting a guessed one would cost this list more trust than
    it earns, on the one order whose acceptance sentence is about trust.
    """
    return (kind or "").lower() in CODE_EXTENSIONS


def why(row: Any) -> str:
    """Why this result is here, for a tooltip or the right-click menu.

    **Moved off the row, not deleted.** `keyword and meaning both matched ·
    score 0.83` is genuinely valuable - being able to ask is where trust comes
    from - but it was the second thing the eye landed on, on every row, for the
    life of the application.
    """
    bits = [
        getattr(row, "location", ""),
        getattr(row, "explain", ""),
        f"score {getattr(row, 'score', 0.0):.2f}",
    ]
    return "  ·  ".join(bit for bit in bits if bit)


def group_subtitle(group: Any, *, show_scores: bool = False,
                   expanded: bool = False, policy: Any = None,
                   volume_note: str = "", online_only: bool = False) -> str:
    """The grey line under the name: where it is, and how many matches.

    `expanded` is passed in rather than read off the group, because a
    `ResultGroup` is frozen and describes the *data*. Whether its chunks are
    currently on screen is a fact about one view at one moment, and putting it
    on the dataclass would make two views of the same results fight over it.

    `volume_note` is Offline Media §3a's remaining half: the tooltip already
    carries `presenter.offline_volume_note`'s exact sentence - "on **<name>**
    (offline, scanned <date>) - plug it in to open" - and this is the same
    sentence painted inline, so a person does not have to hover to learn a
    result is not where it looks like it is. Passed in rather than read off
    `group`, for the same reason `expanded` is: whether a volume is connected
    *right now* is a fact about one moment on one machine, not about the
    data, and computing it here would make this a Windows-touching function
    for every caller, including the Qt-free tests that check its wording.
    """
    bits = [getattr(group, "folder", "")]
    if volume_note:
        bits.append(volume_note)
    elif online_only:
        bits.append(online_only_note(True))
    # §2 of the adoptions order. **On the group's best row, and only there.**
    # A badge on every row would make the one that matters invisible; this
    # appears exactly where a person would otherwise think the search had
    # made a mistake - a result with none of their words in it.
    marker = match_marker(getattr(group, "best", None), policy)
    if marker:
        bits.append(marker)
    label = getattr(group, "match_label", "")
    if label:
        bits.append(f"{label} {'▾' if expanded else '▸'}")
    best = getattr(group, "best", None)
    if show_scores and best is not None:
        bits.append(f"{best.explain}  ·  score {group.score:.2f}")
    return "  ·  ".join(bit for bit in bits if bit)


def result_tooltip(payload: Any, *, missing: bool = False, volume_note: str = "",
                   placeholder: bool = False) -> str:
    """The full path and the explanation - both of which came off the row.

    The breadcrumb is a display choice; the tooltip is where the truth stays.
    So is the date - item 4b promises the exact one here regardless of
    whatever register the visible row is reading in. Item 3a/7a: the kind
    word (`kind_tag`) is here too, now that the painted row shows an icon
    instead of the `[PDF]` text it used to carry the word in directly.

    `volume_note` is Offline Media §3a's own sentence - "on **<name>**
    (offline, scanned <date>) - plug it in to open" - from `app.ui.
    presenter.offline_volume_note`, and it replaces the generic `missing`
    line rather than joining it: a person does not need to be told "this
    file is missing" and then, separately, exactly where it actually is.
    """
    best = getattr(payload, "best", payload)
    relative_path = str(getattr(best, "relative_path", "") or "")
    # **A catalogued-volume row's `path` is never a real filesystem path** -
    # it is the letter-free key `volume_synthetic_path` builds - so showing
    # it verbatim here would read as a broken tooltip rather than a helpful
    # one. `relative_path` is where on the drive the file actually is.
    path = relative_path if relative_path else getattr(payload, "path", "")
    lines = [path]
    kind = str(getattr(payload, "kind", "") or "")
    if kind:
        lines.append(kind_tag(kind))
    if best is not None:
        lines.append(why(best))
    exact = getattr(payload, "when_exact", "") or _exact_date(getattr(best, "mtime_ns", 0))
    if exact:
        lines.append(f"Date: {exact}")
    if volume_note:
        lines.append(volume_note)
    elif missing:
        lines.append("This file is missing - the index is stale for it.")
    if placeholder:
        lines.append(online_only_note(True))
    return "\n\n".join(line for line in lines if line)


def accessible_text(payload: Any, *, expanded: bool = False) -> str:
    r"""One line naming a result, for anything that cannot see it drawn.

    **The delegate migration left the model empty.** Painting moved into
    `result_delegate`, which reads `ROLE_PAYLOAD` and draws from it - so the
    item itself no longer carried any text at all. Everything sighted still
    worked, and a screen reader was handed fifty items that each said nothing.
    `QAccessible` reads `AccessibleTextRole`, falling back to `DisplayRole`;
    with neither set there is nothing to fall back to.

    Deliberately not the tooltip: that is two or three lines with the full path
    and an explanation, which is right to *hover* and wrong to have read aloud
    for every row while somebody arrows down a list. This is the name, the
    folder and the date - what a sighted reader takes from the row at a glance.

    Item 7a's three additions, each because a sighted reader gets the
    equivalent for free and a screen reader user must not be the one person
    who does not: **the kind word** (`kind_tag`) now that a picture has
    replaced the `[PDF]` text that used to carry it; **whether a multi-match
    group is expanded**, since the chevron is otherwise a purely visual cue;
    and **the exact date**, never the register's friendly "yesterday" -
    sighted or not, this is the one line meant to be trusted outright.
    """
    name = str(getattr(payload, "name", "") or getattr(payload, "title", "") or "")
    if not name:
        path = str(getattr(payload, "path", "") or "")
        name = path.replace("\\", "/").rsplit("/", 1)[-1] or path
    parts = [name]
    kind = str(getattr(payload, "kind", "") or "")
    if kind:
        parts.append(kind_tag(kind))
    folder = str(getattr(payload, "folder", "") or "")
    if folder:
        parts.append(f"in {folder}")
    label = str(getattr(payload, "match_label", "") or "")
    if label:
        parts.append(f"{label}, {'expanded' if expanded else 'collapsed'}")
    when = str(getattr(payload, "when_exact", "") or getattr(payload, "when", "")
              or getattr(payload, "modified", "") or "")
    if when:
        parts.append(when)
    return ", ".join(part for part in parts if part)


def results_terminator(count: int) -> str:
    """The quiet end-of-list message.

    Item 5c: shows that the list has ended and the count, so scrolling to the
    bottom reads as an answer rather than a stall. Faint, because it is
    context that *nobody needs while reading*, but answers the implicit
    question "are there more if I scroll?" when there aren't.
    """
    if count == 0:
        return ""
    if count == 1:
        return "That's all — 1 result."
    return f"That's all — {count:,} results."


@dataclass(frozen=True, slots=True)
class Terminator:
    """The end-of-list row itself - item 5c.

    A model row of its own, painted by `result_delegate.ResultDelegate`,
    rather than text bolted onto the summary label above the list: the point
    is that scrolling *to the bottom* is what answers "are there more", and
    that only reads as an answer from inside the list somebody is already
    scrolling.
    """
    text: str
