"""The Code tab: routing, git rows and the wording around them.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from app.core.file_state import derive
from app.core.logging import logger
from app.ui.presenter.formatting import (
    format_count,
    format_size,
    format_when,
    shorten_path,
)

_log = logger.bind(component="ui.presenter")


@dataclass(frozen=True, slots=True)
class RepoFileRow:
    """One indexed file **inside** a repository, for the Code tree.

    The same five columns as the repository above it, read the way a file wants
    them: what it is called, how big it is, what kind it is, when it changed,
    where it lives. A tree whose two levels disagreed about what column three
    meant would be harder to read than two separate tables.
    """

    name: str           # the file name alone; the folder is in `path`
    size: str           # "12.4 KB"
    kind: str           # "py"
    seen: str           # "2 hours ago"
    path: str           # shortened, for the column
    full_path: str      # for opening, revealing and the tooltip
    ext: str = ""       # lower-case, no dot - what `type:` matches
    size_bytes: int = 0
    seen_at: int = 0
    #: Which repository the file is in. **Empty until the Code tab became one
    #: list**: a tree already answered that with the row it hung under, and a
    #: flat list has to say it in a column - "where is that file" is the
    #: question somebody arrives with, and the repository is the answer.
    repo: str = ""
    #: **2026-09-29 note:** now the one-word Status from
    #: `app.core.file_state.derive` ("Indexed", "Deferred", ...), the same word
    #: every other results list shows, rather than the raw store value below.
    #: INDEXED, SKIPPED, FAILED or PENDING - straight from the store. Shown as
    #: a column because "it is in the list but I cannot search inside it" is a
    #: real and useful thing to know, and hiding it invites the same search
    #: twice.
    status: str = ""
    #: Text the preview pane should draw instead of reading the file. Filled
    #: for a *historical* hit, whose version no longer exists on disk - see
    #: `preview_loader.load_preview_for`. Empty for a file in the checkout,
    #: which is read normally.
    preview_text: str = ""
    #: Order 0y §2. Why this row is here once something is typed: "Definition",
    #: "File name" or "Mention". Empty for a box with nothing typed.
    match: str = ""
    #: The line number as shown ("" until it is known), and as a number to
    #: sort and open on (0 until known).
    line: str = ""
    line_no: int = 0
    #: The line of code itself, for a Definition or Mention row.
    code: str = ""


def repo_file_rows(
    records: Iterable[Any], *, now: Optional[float] = None
) -> list[RepoFileRow]:
    """Store rows - or `FileRecord`s - to display rows for a repository's files.

    Takes either shape because the store may hand back dictionaries from a
    dedicated query or dataclasses from `iter_files`, and which one arrives is
    an implementation detail of `read_repo_files`, not of the tree.
    """
    def field(record: Any, key: str, default: Any = "") -> Any:
        if isinstance(record, Mapping):
            return record.get(key, default)
        return getattr(record, key, default)

    out: list[RepoFileRow] = []
    for record in records:
        path = str(field(record, "path", ""))
        if not path:
            continue
        mtime = int(field(record, "mtime_ns", 0) or 0)
        size = int(field(record, "size_bytes", 0) or 0)
        ext = str(field(record, "ext", "") or "").lower().lstrip(".")
        out.append(RepoFileRow(
            repo=str(field(record, "repo", "") or ""),
            status=derive(field(record, "status", ""), field(record, "skip_code", None)),
            name=path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path,
            size=format_size(size),
            kind=ext,
            seen=format_when(mtime, now=now) if mtime else "",
            path=shorten_path(path, limit=60),
            full_path=path,
            ext=ext,
            size_bytes=size,
            seen_at=mtime,
        ))
    return out


#: How many files one repository contributes to the tree at a time. A tree is
#: read, not scrolled through: a repository with 48,000 files would freeze the
#: window building items nobody will look at, and the search box - one tab away,
#: with `repo:` already set - is the right tool past this point. The tree says
#: so when it truncates rather than quietly showing part of the truth.
REPO_FILE_LIMIT = 500


def repo_files_summary(shown: int, *, limit: int = REPO_FILE_LIMIT) -> str:
    """The child row shown when a repository has more files than the tree lists."""
    return (
        f"Showing the first {format_count(limit)} files. "
        f"Press Enter on the repository to search all {format_count(shown)}."
    )


@dataclass(frozen=True, slots=True)
class CodeRoute:
    """What one line typed into the Code box means.

    **One box, two engines.** The owner's correction: *"the code search page is
    all wrong it should be a combined one search box with the git code files in
    the list"*. A tree of repositories above a separate git box made somebody
    choose an engine before they had a question - and the question is nearly
    always "where is that file", which is answered from the index in
    milliseconds.

    So the grammar decides, not the person. A line with no git switch in it is
    an index search: instant, as you type, over every repository at once. A line
    carrying `/history`, `/branch`, `/introduced` and the rest is a git run, on
    Enter, because `git log -S` diffs every commit it walks.
    """

    #: "index" or "git".
    engine: str = "index"
    #: Free text, with the switches removed - **for the index path only.**
    #: A git run is handed the raw line and re-read by `parse_git_query`, which
    #: is the module that knows what `/class OrderService` means; copying half
    #: of that decision here is how the two would come to disagree.
    text: str = ""
    #: `/repo` - a name, not a path.
    repo: str = ""
    #: `/type` or `/extension`, lower-case and without dots.
    extensions: tuple[str, ...] = ()
    #: Why it chose git, for the line above the results. Empty for the index.
    because: str = ""
    #: The whole parse, for the index engine. **Added because `text`, `repo`
    #: and `extensions` were not enough and quietly pretended to be.**
    #:
    #: The Code dropdown offered `/name` and `/path`; `parse_query` filled
    #: `names` and `paths` correctly; and this route carried neither, so both
    #: narrowed nothing while looking like they had. `test_command_subsets.py`
    #: could not catch it - it asserts the *parser* fills a field, not that the
    #: tab consumes it.
    #:
    #: `None` on the git path, where the raw line is re-read by
    #: `parse_git_query` instead. Typed `Any` because `ParsedQuery` is Layer 4
    #: and this module runs on a keystroke.
    parsed: Any = None


#: Switches only the repository engine has. **Moved to `app/search/gitquery.py`**
#: and re-exported here under the name the Code tab already imports.
#:
#: It moved because the main search box needs the same question answered - "is
#: this line worth a subprocess?" - and asking it should not require importing a
#: UI module. `gitquery` is pure: a catalogue and a parser, no `git` anywhere.
def _git_only() -> frozenset[str]:
    from app.search.gitquery import GIT_ONLY as _names

    return _names


GIT_ONLY: frozenset[str] = _git_only()


@dataclass(frozen=True, slots=True)
class GitScope:
    """What the tree on the left has narrowed the file list to.

    **A scope, not a query.** The tree says *where to look*; the box still says
    *what to look for*, and the two compose - asked for as *"dont forget the
    code switches apply there too"*. Selecting `main` and then typing
    `/type cs order` means "`.cs` files matching order, as of main", and neither
    half overrides the other.
    """

    #: "" (everything), "repo", "worktree", "branch" or "commit".
    kind: str = ""
    #: Repository name, as `repos.name` holds it.
    repo: str = ""
    #: Its path on disk - a git run needs this, the index needs the name.
    root: str = ""
    #: Branch name or commit sha. Empty for a repository or the working tree.
    ref: str = ""
    #: What the commit said, for the line above the list.
    subject: str = ""

    @property
    def from_git(self) -> bool:
        """Whether answering this needs git rather than the index.

        **The index cannot answer a branch.** It holds the working tree: a file
        deleted on `main` but alive on a feature branch has no row, and one that
        only ever existed on a branch never had one. That is the whole reason
        this pane is worth building rather than being a filter over names.
        """
        return self.kind in ("branch", "commit")

    def describe(self) -> str:
        if self.kind == "branch":
            return f"{self.repo} · branch {self.ref}"
        if self.kind == "commit":
            subject = f" · {self.subject}" if self.subject else ""
            return f"{self.repo} · commit {self.ref}{subject}"
        if self.kind in ("repo", "worktree"):
            return f"{self.repo} · working tree"
        return "every repository"


def git_rows_matching(
    rows: Any, *, text: str = "", extensions: Any = (),
    types: Any = None, paths: Any = (), names: Any = (),
) -> list[Any]:
    r"""Narrow git-sourced rows by the same switches the index path honours.

    **The switches have to work here too, and git cannot apply them.** A branch
    listing comes back from `git ls-tree` as paths; `/type cs` and free text are
    then a filter over that list rather than a clause in a query. Capped at
    `TREE_FILE_LIMIT` before it arrives, so this is a pass over a few thousand
    strings.

    `extensions` is what the person typed and wins outright; `types` is the
    configured "what counts as code" set, used only when they typed nothing -
    the same precedence as the index path, so a branch and the working tree
    filter identically.

    `paths` and `names` are `/path` and `/name`, and they are here because the
    Code dropdown has always offered both while nothing consumed either. They
    are the same two questions the index path asks and must be asked the same
    way: `/path` is any part of the folder, `/name` is the basename only.

    Matching is substring and case-insensitive on the whole path, because
    `order` should find `src/OrderService.cs` - the rule `code_files` already
    uses for the index.
    """
    wanted = str(text or "").strip().lower()
    typed = {str(e).lstrip(".").lower() for e in (extensions or ()) if str(e).strip()}
    allowed = typed or {str(e).lstrip(".").lower() for e in (types or ())}

    found = []
    for row in rows or ():
        # **Either shape.** A `GitRow` straight from a reader, or the mapping
        # the pane shapes for the table - `repo_file_rows` takes both for the
        # same reason, and reading only attributes silently filtered every
        # mapping out, which looked exactly like a branch with no files in it.
        path = str((row.get("path") if isinstance(row, Mapping)
                    else getattr(row, "path", "")) or "")
        if not path:
            continue
        lowered = path.lower()
        if wanted and wanted not in lowered:
            continue
        # `/path` asks about the folder, so the basename is cut off first -
        # otherwise `path:service` matches `OrderService.cs` sitting in the
        # repository root, which is the answer to the other switch.
        folder = lowered.replace("\\", "/").rpartition("/")[0]
        if any(str(one).lower() not in folder for one in (paths or ())):
            continue
        base = lowered.replace("\\", "/").rpartition("/")[2]
        if any(str(one).lower() not in base for one in (names or ())):
            continue
        if allowed:
            # The same two questions `indexed_ext` answers, in the same order:
            # a suffix, or the whole name for `Makefile` and `Dockerfile`.
            name = path.replace("\\", "/").rpartition("/")[2].lower()
            stem, dot, suffix = name.rpartition(".")
            kind = suffix if (dot and stem) else name.lstrip(".")
            if kind not in allowed:
                continue
        found.append(row)
    return found


def code_type_filter(store: Any) -> Optional[list[str]]:
    r"""The configured "what counts as code" set, or None for no filter.

    **The Code tab listed by location, not by type.** `code_files` narrows on
    `repo_id` and `source_kind` and nothing else, so every PDF, spreadsheet,
    image and log that happens to live in a repository folder appeared in it -
    reported from the window as *"its bringing files which are not code"*, and
    warned about by `app.cli repos` ever since repositories were added:
    *"`scope:code` will match your whole corpus rather than just code"*.

    **It changes what is listed, never what is indexed.** Unticking a group
    here removes those types from one tab; the main search still finds every one
    of them, which is the whole reason this is a view preference rather than a
    reading rule. See `app/core/code_types.py`.

    Read per search rather than cached, so the setting takes effect on the next
    keystroke rather than the next restart - it is one `get_state` and a set
    union over a list already in memory.

    Never raises. An unreadable preference costs the filter, never the tab, and
    the failure direction is "show everything" - a Code tab that has silently
    hidden a language is far harder to notice than one showing a stray PDF.
    """
    from app.core.code_types import choice_from, extensions_for

    try:
        preset, chosen = choice_from(store)
        wanted = extensions_for(preset, chosen)
    except Exception as exc:                     # noqa: BLE001 - see the docstring
        _log.debug("code types not read: {}", exc)
        return None
    return sorted(wanted) if wanted is not None else None


def code_route(text: str) -> CodeRoute:
    """Read the Code box: which engine, and what to give it.

    Qt-free and here rather than in the view, because "which engine" is the
    decision the whole tab turns on and a decision made inside a widget is one
    nobody can test. It is also the decision that must never be made *wrongly
    towards git*: an index lookup taken as a repository search costs somebody
    two seconds and a subprocess for a question that had a 3ms answer.
    """
    from app.search.commands import expand_slashes
    from app.search.gitquery import git_command_for, parse_git_query
    from app.search.query import parse_query

    raw = str(text or "").strip()
    if not raw:
        return CodeRoute()

    # Which git-only switches are present. Read through `git_command_for` so an
    # alias - `/hist`, `/b`, `/by` - counts exactly as its canonical name does.
    named: list[str] = []
    for token in raw.split():
        if not token.startswith("/") and ":" not in token:
            continue
        name = token.lstrip("/").partition(":")[0].partition("=")[0]
        command = git_command_for(name)
        if command is not None and command.name in GIT_ONLY:
            named.append(command.name)

    if named:
        query = parse_git_query(raw)
        # **`/repo` is consumed by the git parser too**, and has to be: git is
        # *run inside* a checkout, so which one is the caller's business - but
        # left in the free text it becomes part of the pattern, and
        # `/repo leasha CustomerId /history` searched for the literal string
        # "/repo leasha CustomerId". The symptom before that was "name a
        # repository first" in answer to a line that named one.
        return CodeRoute(
            engine="git", text=query.text, repo=query.repo,
            extensions=query.extensions,
            because="/" + ", /".join(sorted(set(named))),
        )

    parsed = parse_query(expand_slashes(raw))
    return CodeRoute(
        engine="index",
        text=(parsed.text or "").strip(),
        repo=(parsed.repos[0] if getattr(parsed, "repos", ()) else ""),
        extensions=tuple(getattr(parsed, "ext", ()) or ()),
        parsed=parsed,
    )


# ---------------------------------------------------------------------------
# The Code tab's wording and row shapes
#
# Here rather than in `code_view.py` for the reason every other decision is:
# what a summary says and what a git result looks like as a row are choices,
# and a choice made inside a widget is one nobody can test without a display.
# ---------------------------------------------------------------------------

def _anything_indexed(store: Any) -> bool:
    """Cheap and guarded. Only decides which of two sentences to show.

    **It used to say that and not be true.** `stats()` is three `COUNT(*)`,
    two of them scans of `chunks` - 93ms at two million, around 460ms at
    ten - and this runs while the tab is being drawn. `has_any_files()` is
    one row with a `LIMIT 1`, which is what "is there anything" needs.

    Moved here from `CodeView` (order 0y §1) when the view reached the
    250-line guard; the name is kept so `test_ui_never_blocks`' named
    exemption still covers it.
    """
    try:
        return store.has_any_files()
    except Exception as exc:                 # noqa: BLE001
        _log.debug("could not read the index size: {}", exc)
        return True                          # the less alarming of the two


def repo_root_for(repos: Iterable[Mapping[str, Any]], name: str) -> str:
    """The folder for a repository name, or the only one there is.

    **Only one is a fact; several is a question.** With no name given and
    exactly one repository known, that is plainly the one meant. With several,
    picking the first would be a guess presented as an answer - so it returns
    nothing and the caller asks.
    """
    wanted = (name or "").strip().lower()
    rows = list(repos or ())
    for row in rows:
        if wanted and wanted in str(row.get("name", "")).lower():
            return str(row.get("root_path", "") or "")
    if not wanted and len(rows) == 1:
        return str(rows[0].get("root_path", "") or "")
    return ""


def preset_label(preset: str) -> str:
    """A preset's name, in words. `""` for one nobody configured."""
    from app.core.code_types import PRESET_LABELS

    return PRESET_LABELS.get(str(preset or ""), "your code-type filter")


def repo_list_empty(*, indexed_files: int, hidden: int, has_query: bool,
                    preset: str = "") -> str:
    r"""Why this repository's list is empty. Three states, three sentences.

    From `WORKORDER-202626081149-code-tab.md` §5. All three used to render
    identically, as nothing:

    * the repository has no indexed files at all - its folder is probably not
      under an indexed root, which is a *configuration* problem;
    * it has files and the type filter hid every one - a *setting* problem, and
      the number is the thing that makes it obvious;
    * it has matching files and the query excluded them - which is search
      working correctly.

    `repo_empty_state` already does exactly this one level up, for *no
    repositories at all*, and its docstring explains why a generic "no results"
    would waste the answer that matters. The same care had not been applied
    here.
    """
    if indexed_files <= 0:
        return ("No indexed files in this repository.\n\n"
                "Its folder may not be under an indexed root — add the folder "
                "above it in Settings, then index again.")
    if hidden and hidden >= indexed_files:
        return (f"0 of {indexed_files:,} shown — all hidden by "
                f"{preset_label(preset).lower()}.\n\n"
                f"Change it with the code-types button above.")
    if has_query:
        return "No file matches that query in this repository."
    return "No files to show."


def code_preset(store: Any) -> str:
    """Which code-type preset is in force. Never raises - it is a caption.

    Reads `choice_from` rather than a second state key, so the caption cannot
    name a preset the filter is not using.
    """
    from app.core.code_types import choice_from

    preset, _chosen = choice_from(store)
    return preset


#: Order 0y §2: the words for each kind of row, in the order they are listed.
MATCH_DEFINITION = "Definition"
MATCH_FILE = "File name"
MATCH_MENTION = "Mention"


def _repo_for(path: str, repos: Iterable[Mapping[str, Any]]) -> str:
    """The repository holding `path`: the longest root it sits under."""
    folded = path.replace("\\", "/").lower()
    best, name = -1, ""
    for row in repos or ():
        root = str(row.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
        if root and (folded == root or folded.startswith(root + "/")) and len(root) > best:
            best, name = len(root), str(row.get("name") or "")
    return name


def code_match_rows(matches: Iterable[Any], repos: Iterable[Mapping[str, Any]]) -> list[RepoFileRow]:
    """`code_search.CodeMatch`es as rows of the Code list. Order 0y §2b."""
    from app.search.code_search import KIND_DEFINITION

    repos = list(repos or ())
    out: list[RepoFileRow] = []
    for match in matches or ():
        path = str(match.path)
        name = path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path
        line = int(match.line or 0)
        out.append(RepoFileRow(
            name=name, size="", kind=str(match.ext or ""), seen="",
            path=shorten_path(path, limit=60), full_path=path,
            ext=str(match.ext or ""), repo=_repo_for(path, repos),
            match=MATCH_DEFINITION if match.kind == KIND_DEFINITION else MATCH_MENTION,
            line=str(line) if line else "", line_no=line,
            code=str(match.text or "").strip(),
        ))
    return out


def code_list(file_rows: list[RepoFileRow], match_rows: list[RepoFileRow]) -> list[RepoFileRow]:
    """Definitions, then files whose name matched, then mentions (order 0y §2a).

    File rows are marked "File name" only when there are content rows beside
    them; with nothing typed the list is today's list, unchanged.
    """
    from dataclasses import replace as _replace

    if not match_rows:
        return list(file_rows)
    definitions = [r for r in match_rows if r.match == MATCH_DEFINITION]
    mentions = [r for r in match_rows if r.match != MATCH_DEFINITION]
    files = [_replace(r, match=MATCH_FILE) for r in file_rows]
    return definitions + files + mentions


def match_counts(rows: Iterable[RepoFileRow]) -> str:
    """`3 definitions · 12 files · 48 mentions` - "" when nothing was typed (§2d)."""
    rows = list(rows)
    if not any(r.match for r in rows):
        return ""
    counts = {kind: sum(1 for r in rows if r.match == kind)
              for kind in (MATCH_DEFINITION, MATCH_FILE, MATCH_MENTION)}
    words = ((MATCH_DEFINITION, "definition", "definitions"),
             (MATCH_FILE, "file", "files"), (MATCH_MENTION, "mention", "mentions"))
    return "  ·  ".join(f"{counts[k]:,} {one if counts[k] == 1 else many}"
                        for k, one, many in words)


def code_page(payload: Any, repos: list[Any], scope: Any = None, *,
              preset: str = "") -> tuple[list[RepoFileRow], str]:
    """The Code list and its summary line, from what the worker returned.

    Order 0y §2: definitions, then the files whose name matched, then mentions
    (`code_list`), with the counts ahead of the usual summary (`match_counts`).
    Here rather than in the view for the reason the rest of this module is.
    """
    records = payload.get("rows") if isinstance(payload, dict) else payload
    files = repo_file_rows(list(records or [])[:REPO_FILE_LIMIT])
    found = payload.get("matches") if isinstance(payload, dict) else None
    rows = code_list(files, code_match_rows(found or (), repos))
    counts = match_counts(rows)
    summary = code_summary(files, repos, scope, preset=preset)
    return rows, (f"{counts}  ·  {summary}" if counts else summary)


def code_summary(rows: list[Any], repos: list[Any], scope: Any = None,
                 *, preset: str = "") -> str:
    r"""What is on screen, and what there is. Both, because "40 files" over a
    corpus of 48,000 and over one of 40 mean different things.

    **And what it is scoped to**, when the tree has narrowed it. A list showing
    a branch with nothing on screen saying so is the same failure as a skipped
    archive that says nothing: the numbers look ordinary and mean something
    else. `git ls-tree` also answers from the repository rather than the index,
    so "2 files" beside "48,000 indexed" would otherwise be simply confusing.
    """
    total = sum(int(row.get("files", 0) or 0) for row in repos)
    count = len(repos)
    # **The arithmetic is done here, not handed in.** The view had to compute
    # it, which put a sum and a `max(0, ...)` into a module the length guard
    # keeps short - and the numbers it needs are the two already passed.
    hidden = max(0, total - len(rows)) if preset else 0
    parts = [f"{len(rows):,} file{'s' if len(rows) != 1 else ''}"]
    # **The arithmetic, whenever a type filter is hiding something.**
    #
    # `DEFAULT_PRESET` is `build`, which excludes `.md`, `.txt`, `.json`, `.yml`
    # and `.csv` - so `README.md`, `package.json` and `requirements.txt` are
    # filtered out of the list on a fresh install and nothing on screen says a
    # filter is active. `code_type_filter`'s own docstring warns that "a Code
    # tab that has silently hidden a language is far harder to notice than one
    # showing a stray PDF", and then the default did exactly that. Combined
    # with a repository holding no indexed files it is the second reason the
    # owner saw an empty list and could not tell why.
    if hidden > 0:
        parts.append(f"{hidden:,} hidden by {preset_label(preset).lower()}")
    if scope is not None and str(getattr(scope, "kind", "")):
        parts.append(scope.describe())
    if len(rows) >= REPO_FILE_LIMIT:
        parts.append(f"showing the newest {REPO_FILE_LIMIT:,} — narrow it with "
                     f"/repo or /type")
    parts.append(f"{count:,} repositor{'ies' if count != 1 else 'y'}, "
                 f"{total:,} indexed file{'s' if total != 1 else ''}")
    return "  ·  ".join(parts)


def git_summary(found: Any) -> str:
    parts = [f"{len(found.rows):,} result{'s' if len(found.rows) != 1 else ''} "
             f"{found.explain}", f"{found.elapsed_s:.2f}s"]
    if found.truncated:
        parts.append("stopped at the limit — narrow it with /path, /extension "
                     "or /depth")
    if not found.rows:
        parts.append(f"command: {' '.join(found.command)}")
    return "  ·  ".join(parts)


def git_result_row(row: Any, repo_root: str) -> Any:
    """One git result, in the shape the table and the preview already read.

    A hit in the *checkout* has a file to open and preview; a hit in history
    does not - the version that matched no longer exists on disk. Handing the
    pane a path that is not there would show "file missing" for every history
    result, which reads as a broken preview rather than as a file that is
    genuinely gone.
    """
    historical = bool(row.commit) and row.kind != "content"
    full = ("" if historical or not row.path
            else str(Path(repo_root) / row.path) if repo_root else row.path)
    where = row.path or ""
    if row.line_no:
        where = f"{where}:{row.line_no}"

    name = row.path.rsplit("/", 1)[-1] if row.path else (row.subject or row.commit[:8])
    return RepoFileRow(
        name=name,
        # A history row has no size on disk: the version that matched is gone,
        # and a number invented for the column would be a number somebody
        # believes.
        size="",
        repo=row.commit[:8] if row.kind == "commit" else (row.author or ""),
        kind={"commit": "commit", "change": row.status or "change"}.get(
            row.kind, row.status or "line"),
        seen=row.date or "",
        path=where,
        full_path=full,
        preview_text=(f"{row.subject}\n\n{row.author}   {row.date}   {row.commit}"
                      if row.kind == "commit" else row.text),
    )
