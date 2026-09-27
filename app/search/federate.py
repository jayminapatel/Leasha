r"""Repository history as a source the main search box can draw on.

Layer: L4

Asked for as *"the main search searches every thing no matter what… all the
switches in the files mail and code should be available in the main search"*,
and then sharpened: *"it is not just a switch union it is union of all data as
source too"*.

The switch half landed with `app/storage/filters.py` - one definition of what a
filter means, composed by every tab. This is the other half, and it is a
different kind of problem, because **git is not in the index and cannot be**.
A repository's history is thousands of versions of files that no longer exist;
reaching it means running `git`, and `git log -S` over real history takes
seconds. Everything else the search box does takes milliseconds.

So three rules shape this module, and each is a constraint somebody already
wrote a test for:

**The engine stays git-free.** `SearchEngine` must not gain the ability to run a
subprocess - `test_the_index_search_engine_cannot_reach_git_even_indirectly`
asserts it does not so much as mention `gitsearch`. That test is right: the
engine is what answers a keystroke, and a keystroke must never fork a process.
So federation lives here, beside the engine rather than inside it, and the
caller joins the two.

**Nothing here runs while somebody is typing.** `wants_git` decides, and it is
pure; `git_hits` is only ever reached on the full tier, behind the same worker
the index search uses. `test_nothing_that_runs_on_a_keystroke_imports_this`
names the modules that must not even *import* git - and importing this one does
not, because `gitsearch` is imported inside the function rather than at the top.
That is not a trick to get past the guard: it is what makes the guard true.

**A repository nobody asked about is not searched.** With no `/repo`, the
switches apply to every repository in the index, capped at `MAX_REPOS`. Twenty
checkouts is twenty subprocesses, and the cap is what stops a five-word query
becoming a minute.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional, Sequence

from app.core.logging import logger
from app.core.osbridge.pathnames import join_under
from app.search.engine import SearchResult
from app.search.gitquery import parse_git_query, wants_git

__all__ = ["git_hits", "wants_git", "MAX_REPOS", "ROWS_PER_REPO", "GIT_SOURCE"]

log = logger.bind(component="search.federate")

#: Repositories searched when the query names none.
#:
#: **A cap, not a preference.** Each one is at least one subprocess and possibly
#: several seconds; a corpus of forty checkouts would turn one query into a
#: minute of forking. Ordered by `repos_list`, which is file count descending -
#: so the cap keeps the repositories somebody actually works in.
MAX_REPOS = 8

#: Rows taken from each repository before the next one is asked.
ROWS_PER_REPO = 200

#: What `SearchResult.source_label` carries for a federated row, so the list can
#: say where a hit came from rather than implying it is a file on disk.
GIT_SOURCE = "repository history"


def _label(row: Any, repo_name: str) -> str:
    """The one-line explanation shown under a federated row."""
    commit = str(getattr(row, "commit", "") or "")[:8]
    author = str(getattr(row, "author", "") or "")
    when = str(getattr(row, "date", "") or "")
    parts = [GIT_SOURCE, repo_name] if repo_name else [GIT_SOURCE]
    if commit:
        parts.append(commit)
    if author:
        parts.append(author)
    if when:
        parts.append(when)
    return " · ".join(parts)


def _text_of(row: Any) -> str:
    r"""What to show as the row's body.

    A content hit has the matching line. A commit has only its subject, and a
    change has neither - so it gets its status and path, which is the whole of
    what git said about it. **Never `None`**: the snippet builder runs a regex
    over this, and a row that cannot draw is worse than a row that says little.
    """
    text = str(getattr(row, "text", "") or "").strip()
    if text:
        return text
    subject = str(getattr(row, "subject", "") or "").strip()
    if subject:
        return subject
    status = str(getattr(row, "status", "") or "").strip()
    path = str(getattr(row, "path", "") or "").strip()
    return f"{status} {path}".strip() or "(no text)"


def git_hits(
    repos: Iterable[Any],
    raw: str,
    *,
    limit: int = 50,
    start_rank: int = 1,
    runner: Optional[Callable[..., Any]] = None,
    roots: Optional[Sequence[Any]] = None,
) -> list[SearchResult]:
    r"""Run the query's git half and return it in the shape the results list draws.

    **Never raises.** A repository that has been moved, a `git` that is not
    installed, a history search that times out: each is one repository producing
    nothing, and the index half of the search is unaffected. A federated source
    that can take down the search it is federated into is not worth having.

    `runner` and `roots` are the seams. The first is `gitsearch`'s own
    subprocess seam, so every branch here is testable without a repository; the
    second lets a caller supply resolved roots directly.
    """
    switches = wants_git(raw)
    if not switches:
        return []

    # Imported here rather than at module scope. `gitsearch` is the only thing
    # in this application that forks a process, and the modules that run behind
    # a keystroke are forbidden from importing it - see the module docstring.
    from app.search.gitsearch import run_query

    query = parse_git_query(raw)
    chosen = list(roots) if roots is not None else _roots_for(repos, query.repo)
    if not chosen:
        log.debug("no repository matched {}, skipping the git half", query.repo or "*")
        return []

    hits: list[SearchResult] = []
    rank = int(start_rank)
    for name, root in chosen[:MAX_REPOS]:
        if len(hits) >= limit:
            break
        try:
            kwargs: dict[str, Any] = {"limit": ROWS_PER_REPO}
            if runner is not None:
                kwargs["runner"] = runner
            found = run_query(root, query, **kwargs)
        except Exception as exc:                # noqa: BLE001 - one repository
            log.warning("git search failed in {}: {}", name, exc)
            continue
        if not getattr(found, "ok", False):
            log.debug("git search unsuccessful in {}: {}", name,
                      getattr(found, "error", ""))
            continue

        for row in getattr(found, "rows", ()) or ():
            if len(hits) >= limit:
                break
            relative = str(getattr(row, "path", "") or "")
            hits.append(SearchResult(
                # **Negative and unique.** `group_results` keys rows by
                # `file_id`, so every federated row sharing a zero would collapse
                # the whole git half into one group. Negative because every real
                # id is positive, which also makes a federated row identifiable
                # anywhere downstream without a second field to check.
                chunk_id=-(rank + 1),
                file_id=-(rank + 1),
                path=_join(root, relative),
                text=_text_of(row),
                # **Below every index hit, deliberately.** These are not scored
                # against the others - git returns matches, not ranks - and
                # interleaving them on a made-up number would be a claim about
                # relevance that nothing supports.
                score=0.0,
                rank=rank,
                ext=_ext_of(relative),
                source_label=_label(row, name),
            ))
            rank += 1
    return hits


def _join(root: Any, relative: str) -> str:
    r"""`root/relative`, as a string, without pretending the file exists.

    A historical hit names a file that may have been deleted ten years ago. The
    path is still the most useful thing to show - it is what somebody searches
    for next - and the results list already marks paths it cannot find.

    **Joined as text, not through `Path`.** `Path(r"D:\a") / "src/b.cs"` on Linux
    produces `D:\a/src/b.cs` - two separators in one path - because `pathlib`
    there has no idea `\` divides anything. Git always reports forward slashes,
    the rest of this application stores backslashes, and this is the seam between
    them. That mistake has now been found seven times in this project; it is not
    worth an eighth.

    **The joining itself now lives in `osbridge.join_under`** (order 0x section
    7a). On Windows it is the very same expression this function used to hold,
    so every path it returns there is unchanged, character for character. On a
    Mac the repository sits at `/Users/.../repo`, and gluing a backslash on
    would have produced `/Users/.../repo\src\b.cs` - one file name with
    backslashes in it, which nothing can open; there it joins with `/`.
    """
    return join_under(root, relative)


def _ext_of(relative: str) -> str:
    name = relative.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, suffix = name.rpartition(".")
    return suffix.lower() if (dot and stem) else ""


def _roots_for(repos: Iterable[Any], named: str) -> list[tuple[str, Any]]:
    """`(name, root_path)` for the repositories this query should search.

    A named repository is matched the way `repo_root_for` matches it - as a
    substring, case-insensitively - because people type half a project name.
    With no name, every known repository is a candidate and the cap decides.
    """
    wanted = str(named or "").strip().lower()
    found: list[tuple[str, Any]] = []
    for row in repos or ():
        name = str((row.get("name") if hasattr(row, "get")
                    else getattr(row, "name", "")) or "")
        root = str((row.get("root_path") if hasattr(row, "get")
                    else getattr(row, "root_path", "")) or "")
        if not root:
            continue
        if wanted and wanted not in name.lower():
            continue
        found.append((name, root))
    return found
