r"""Running one search, the same way from every surface. **Worker only.**

Layer: L4. No Qt, and nothing from `app.ui` - the window calls down into this,
never the reverse.

2026-10-04, the owner: "where ever possible the same code should run for
functions so they are all consistent and standard", and his decision: **the
command line and the MCP server search exactly as the window's Search tab
does** - the Settings search switches, plain-English filter reading, saved
searches, one row per document.

Before this module each surface had its own copy of the steps, and each copy
had dropped a different one: the command line never read "mail from 2017" as
filters and ignored the Settings switches, the shell skipped `/slash`
commands, the mini box ignored saved searches, and the MCP server returned
one row per passage. Every step now lives here once, in the order the Search
tab runs them:

1. `expand_query` - `/type pdf` becomes `type:pdf`, then `saved:name`
   becomes the query it stands for (and its scope).
2. `read_filters` - the plain-English rules (`translate_rules.apply`), as the
   Settings switch "filter chips" allows on this surface.
3. `search_once` - the engine, with the surface's policy from
   `from_settings(surface, preferences)` and the one rerank setting.
4. `git_results` - repository history, only when a history switch was typed.
5. `documents` - one row per document, headed by its best passage, with the
   missing/offline mark the results list draws.

The window still runs these across its own threads - expansion where the
saved list is cached, the engine on a `SearchWorker`, history on a second
worker - but each step is the function below. `run_search` is all five in
order, for a caller with no window: `app.cli search`, the shell, the mini
box and the MCP server.

**Nothing here touches a model beyond what the engine already does**, and no
step adds a per-result query: the marks and mail details are one batched read
for the page (`app.search.marks`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, NamedTuple, Optional, Sequence

from app.core.logging import logger

__all__ = [
    "GROUP_FETCH_MULTIPLIER",
    "Document",
    "Expanded",
    "SearchRun",
    "documents",
    "expand_query",
    "fetch_depth",
    "find_files",
    "git_results",
    "group_by_document",
    "load_saved",
    "note_saved_runs",
    "read_filters",
    "read_typed",
    "rerank_wanted",
    "respell",
    "run_search",
    "search_once",
    "words_of",
]

_log = logger.bind(component="search.run")

#: Where the window keeps the Rerank switch (toolbar and Settings, one value).
RERANK_STATE = "ui:rerank_enabled"

#: How many chunks to fuse before grouping, as a multiple of the groups shown.
#:
#: **Grouping shrinks the list, so the fetch has to be deeper than the display.**
#: If fifty chunks come back and thirty belong to one PDF, grouping yields far
#: fewer documents than chunks. Four is enough for a document matching in a
#: handful of places without quadrupling rerank cost.
GROUP_FETCH_MULTIPLIER = 4


def fetch_depth(display_count: int, *, multiplier: int = GROUP_FETCH_MULTIPLIER) -> int:
    """How many chunks to ask for, to end up with `display_count` documents."""
    return max(1, int(display_count)) * max(1, int(multiplier))


# ---------------------------------------------------------------------------
# The settings every surface reads
# ---------------------------------------------------------------------------

def rerank_wanted(settings: Any, store: Any = None) -> bool:
    r"""Whether searches rerank. **One answer for every surface.**

    The window's Rerank box (toolbar and Settings, one control since the fix
    described in `search_bar.build_rerank`) is saved in store state
    `ui:rerank_enabled`; until it has ever been touched, `RERANK_ENABLED` in
    `.env` decides. Before 2026-10-04 the engine's reranker read only the
    `.env` value while the toolbar read only the state, so the two could
    disagree - and the command line followed `.env` alone.
    """
    stored = ""
    if store is not None:
        try:
            stored = str(store.get_state(RERANK_STATE, "") or "").strip().lower()
        except Exception as exc:                 # noqa: BLE001 - fall back to .env
            _log.debug("could not read the rerank switch: {}", exc)
    if stored in ("on", "off"):
        return stored == "on"
    return bool(getattr(settings, "rerank_enabled", False))


def load_saved(store: Any) -> tuple:
    """The saved searches, as `SavedSearch`es, most-run first. Never raises."""
    if store is None or not hasattr(store, "saved_searches"):
        return ()
    try:
        from app.search.saved import ordered

        return ordered(store.saved_searches())
    except Exception as exc:                     # noqa: BLE001 - a search still runs
        _log.debug("could not read the saved searches: {}", exc)
        return ()


def note_saved_runs(store: Any, names: Iterable[str]) -> None:
    """Count a run of each saved search, so the menu orders by use. Never raises."""
    for name in names or ():
        try:
            store.note_saved_search_run(name)
        except Exception as exc:                 # noqa: BLE001 - a counter
            _log.debug("could not count a run of {}: {}", name, exc)


# ---------------------------------------------------------------------------
# Step 1: what was typed, expanded
# ---------------------------------------------------------------------------

class Expanded(NamedTuple):
    """A typed line with its slashes and saved searches expanded."""

    query: str
    #: The scope a saved search carries, `""` when none was expanded.
    scope: str
    #: The saved searches that resolved - the ones whose run is counted.
    names: tuple


def expand_query(text: Any, saved: Any = ()) -> Expanded:
    r"""`/type pdf` to `type:pdf`, then `saved:name` to what it stands for.

    **Slashes first, then saved.** `/saved invoices` becomes `saved:invoices`
    in `expand_slashes`, exactly as `/type pdf` becomes `type:pdf`, so there is
    one doorway and one grammar. A name nobody saved is left as typed
    (`expand_saved`'s rule) and is not counted as run. Pure: `saved` is a list
    already in hand, never a store.
    """
    from app.search.commands import expand_slashes
    from app.search.saved import expand_saved, find, mentions_saved, names_in

    query = expand_slashes(str(text or "").strip())
    if not mentions_saved(query):
        return Expanded(query, "", ())
    names = tuple(name for name in names_in(query) if find(saved, name) is not None)
    query, scope = expand_saved(query, saved)
    # **And slashes again, inside what a saved search stood for** (fixed
    # 2026-10-04). A search is saved as the box held it (`ask_to_save` takes
    # the typed text), so `report /oldest` saved and run as `saved:reports`
    # reached the parser unexpanded: the sort was dropped and "oldest" became
    # a search word. `expand_slashes` leaves an expanded line as it is.
    return Expanded(expand_slashes(query), scope, names)


# ---------------------------------------------------------------------------
# Step 2: plain English read as filters
# ---------------------------------------------------------------------------

def read_filters(store: Any, sentence: str, policy: Any = None,
                 declined: Any = ()) -> tuple[str, tuple]:
    r"""`(query to run, filters applied)` for a typed sentence. **Never raises.**

    **Owner decision 2026-09-27: "mail from 2017" applies its filters** - see
    `translate_rules.apply` for which readings are confident enough to act on.
    The policy's `auto_chips` decides, so a surface with it off (or a person
    who switched it off in Settings) keeps the words as typed. `declined` are
    filters the person removed; those words go back to being search terms.

    Not in the engine, and that is a rule: the retrieval path may not know
    translation exists (`test_the_search_engine_cannot_reach_the_translator`).
    It reads the store's senders and file types, so worker only.
    """
    text = str(sentence or "")
    if policy is not None and not getattr(policy, "auto_chips", True):
        return text, ()
    try:
        from app.search.translate_rules import apply

        applied = apply(text, store, declined=tuple(declined or ()))
        return applied.query, tuple(applied.filters)
    except Exception as exc:                       # noqa: BLE001 - a helper
        _log.debug("no filters applied to this query: {}", exc)
        return text, ()


def read_typed(store: Any, raw: str, *, surface: str, preferences: Any = None,
               declined: Any = ()) -> tuple:
    r"""`(parsed, applied)` for what was typed in a list tab's box. **Worker.**

    **One reading for every tab** (owner, 1 October 2026): slash commands,
    then the plain-English rules, then the parser - the Files, Mail and Code
    tabs, `app.cli files` and the MCP `find_files` tool all read a line here.
    """
    from app.search.commands import expand_slashes
    from app.search.policy import from_settings
    from app.search.query import parse_query

    text = expand_slashes(str(raw or "").strip())
    query, applied = read_filters(store, text, from_settings(surface, preferences),
                                  tuple(declined or ()))
    return parse_query(query), tuple(applied)


def words_of(parsed: Any) -> str:
    """The words left to match once filters are taken out, without the filler."""
    from app.search.query import _INSTRUCTION_WORDS, _STOPWORDS

    filler = _STOPWORDS | _INSTRUCTION_WORDS
    words = [str(term) for term in (getattr(parsed, "terms", ()) or ())
             if str(term).lower() not in filler]
    return " ".join([*words, *(str(p) for p in (getattr(parsed, "phrases", ()) or ()))])


def respell(store: Any, words: Any, *, surface: str, preferences: Any = None) -> Any:
    """The Search tab's spelling help for a list that came back empty, or None.

    **The same rule the engine applies** (`SearchEngine._spelling`): exactly one
    word the index has never seen, corrected by `spelling.from_store`, and only
    when the surface's "fix spelling" switch is on. Asked only when the list is
    empty - a word can be part of a file's name the text index has never held.
    """
    from app.search.keyword import unmatched_terms
    from app.search.policy import from_settings
    from app.search.spelling import from_store

    words = tuple(str(word) for word in (words or ()) if str(word).strip())
    if not words or from_settings(surface, preferences).typo_correction == "off":
        return None
    try:
        missing = unmatched_terms(store, words)
        return from_store(store, missing[0]) if len(missing) == 1 else None
    except Exception as exc:                     # noqa: BLE001 - a suggestion, not the list
        _log.debug("no spelling help for this list: {}", exc)
        return None


def find_files(store: Any, raw: str, *, limit: int, preferences: Any = None,
               declined: Any = (), page: Optional[Callable[[Any], dict]] = None) -> dict:
    r"""The Files tab's search: files by name, folder or contents. **Worker.**

    The line is read as every tab reads it (`read_typed`, surface `files`),
    `store.browse_files` applies every switch, and an empty list gets the
    tab's spelling help once. Returns `{"rows", "parsed", "applied",
    "spelling"?}` plus whatever `page` adds - the tab's own page carries a
    total and the offline volumes; left out, the rows alone.

    2026-10-04: `app.cli files` and the MCP `find_files` tool searched names
    only (`search_files_by_name`), so a switch, a misspelling or "pdf from
    2019" found something different there than on the tab.
    """
    from app.search.query import with_terms

    if page is None:
        def page(parsed: Any) -> dict:
            return {"rows": store.browse_files(parsed, limit=limit)}

    parsed, applied = read_typed(store, raw, surface="files", preferences=preferences,
                                 declined=declined)
    found_page = page(parsed)
    found = None if found_page["rows"] else respell(
        store, words_of(parsed).split(), surface="files", preferences=preferences)
    if found is not None:
        corrected = with_terms(parsed, tuple(
            found.suggestion if str(term).lower() == found.typed else term
            for term in parsed.terms))
        again = page(corrected)
        if again["rows"]:
            found_page, parsed = again, corrected
            found_page["spelling"] = found.sentence()
    found_page.update(parsed=parsed, applied=applied)
    return found_page


# ---------------------------------------------------------------------------
# Steps 3 and 4: the engine, then repository history
# ---------------------------------------------------------------------------

def search_once(engine: Any, query: str, *, tier: str = "full",
                declined: Any = None, **options: Any) -> Any:
    r"""One engine search at one tier, with the plain-English filters applied.

    `options` go to the engine as given (`scope`, `policy`, `rerank`, `limit`).
    `declined` present - even empty - means the surface reads filters
    (`search_options` adds it only when the policy's `auto_chips` is on); it
    is never passed to the engine, which has no such argument and must not.
    The interim tier is keyword-only and takes a scope and nothing else.
    `response.applied` is set on every response, cached or not, so a chip can
    never be carried over from the search that filled the cache.
    """
    applied: tuple = ()
    if declined is not None:
        query, applied = read_filters(getattr(engine, "store", None), query,
                                      options.get("policy"), declined)
    if tier == "interim":
        response = engine.interim(query, scope=options.get("scope", "all"))
    else:
        response = engine.search(query, **options)
    response.applied = applied
    return response


def git_results(store: Any, query: str, *, start_rank: int = 1,
                limit: Optional[int] = None) -> list:
    r"""Repository history for a query that asked for it, else `[]`. **Never raises.**

    `git_hits` returns nothing unless a history switch (`/history`, `/branch`
    ...) was typed, so this costs a regex on every other search. Seconds when
    it does run - the window starts it on its own worker, full tier only.
    """
    if store is None:
        return []
    try:
        from app.search.federate import git_hits

        kwargs: dict = {"start_rank": int(start_rank)}
        if limit is not None:
            kwargs["limit"] = int(limit)
        return list(git_hits(store.repos_list(), query, **kwargs))
    except Exception as exc:                     # noqa: BLE001 - one half
        _log.warning("the repository half of the search failed: {}", exc)
        return []


# ---------------------------------------------------------------------------
# Step 5: one row per document
# ---------------------------------------------------------------------------

def group_by_document(items: Iterable[Any], key: Optional[Callable[[Any], Any]] = None
                      ) -> list[list]:
    r"""Ranked chunks to documents: `[[best chunk, next, ...], ...]`.

    **The rule, in one place** - the results list (`presenter.group_results`),
    the mini box, the command line and the MCP server all group with it. Rows
    must already be in rank order; the first appearance of a document decides
    where it sits, so the engine's order is kept rather than recomputed. `key`
    is `file_id` unless the caller folds by something wider (a conversation).

    **Never in the engine.** The measured baselines were taken against chunk-
    level ranking; grouping there would change what "rank 1" means.
    """
    key = key or (lambda row: getattr(row, "file_id", 0))
    order: list[Any] = []
    collected: dict[Any, list] = {}
    for row in items or ():
        found = key(row)
        if found not in collected:
            collected[found] = []
            order.append(found)
        collected[found].append(row)
    return [collected[found] for found in order]


@dataclass
class Document:
    """One document of a search's results, headed by its best passage."""

    rank: int
    #: The best-ranked `SearchResult` of this document.
    best: Any
    #: How many of its passages matched.
    matches: int
    #: `marks.OK`, `MISSING` or `OFFLINE` - whether it can be opened now.
    status: str = "ok"
    #: Subject, sender and date for a message, or an attachment's message
    #: (`marks.mail_details`); `None` for anything else.
    mail: Optional[dict] = None

    def as_dict(self) -> dict:
        found = self.best.as_dict()
        found.update(rank=self.rank, matches=self.matches, status=self.status)
        if self.mail:
            found["mail"] = dict(self.mail)
        return found


def documents(results: Sequence[Any], *, store: Any = None, limit: int = 0,
              marks: bool = True) -> list[Document]:
    r"""`results` as one `Document` per file, best first, ranked from 1.

    With `marks` and a `store`, each carries its mail details and its
    missing/offline mark - two batched reads for the page, the window's own
    (`tasks.decorate_results`), only over the documents kept.
    """
    groups = group_by_document(results)
    if limit:
        groups = groups[:limit]
    heads = [group[0] for group in groups]
    details: dict = {}
    status: dict = {}
    if marks and heads:
        from app.search.marks import mail_details, status_marks

        details = mail_details(store, heads) if store is not None else {}
        status = status_marks(store, heads)
    out = []
    for rank, group in enumerate(groups, start=1):
        head = group[0]
        file_id = int(getattr(head, "file_id", 0) or 0)
        out.append(Document(rank=rank, best=head, matches=len(group),
                            status=status.get(file_id, "ok"),
                            mail=details.get(file_id)))
    return out


# ---------------------------------------------------------------------------
# All five, for a caller with no window
# ---------------------------------------------------------------------------

@dataclass
class SearchRun:
    """One search, as `run_search` ran it."""

    #: What was typed.
    raw: str
    #: After slashes and saved searches, before the filters were read - what
    #: the window's box would hold, and what the history half searches.
    expanded: str
    #: The scope searched: a saved search's, or the caller's.
    scope: str
    #: The engine's answer; `response.applied` holds the filters read.
    response: Any
    #: Repository history rows, ranked after the index's.
    git: list = field(default_factory=list)
    #: Saved searches the line referred to that resolved.
    saved_names: tuple = ()
    #: One per document, best first, capped at what was asked for.
    documents: list = field(default_factory=list)

    @property
    def applied(self) -> tuple:
        return tuple(getattr(self.response, "applied", ()) or ())

    @property
    def results(self) -> list:
        """Every chunk, index first and history after - before grouping."""
        return [*(getattr(self.response, "results", None) or []), *self.git]

    def as_dict(self) -> dict:
        r"""The engine's own report (`SearchResponse.as_dict` - timings, dates,
        notices) with `results` one entry per document, and what the line was
        read as. `app.cli search --json`'s shape."""
        out = dict(self.response.as_dict())
        out.update(
            query=self.raw, expanded=self.expanded, scope=self.scope,
            applied=[{"kind": a.kind, "label": a.label, "operators": list(a.operators)}
                     for a in self.applied],
            hits=len(self.documents),
            results=[document.as_dict() for document in self.documents],
        )
        return out


def run_search(engine: Any, text: str, *, surface: str = "search",
               preferences: Any = None, saved: Any = None, scope: str = "all",
               rerank: Optional[bool] = None, declined: Any = (),
               limit: int = 0, marks: bool = True,
               note_saved: bool = False) -> SearchRun:
    r"""Search as the Search tab does, start to finish. **Worker only.**

    * `preferences` - the Settings search switches, as
      `app.search.policy.preferences(settings)` gives them.
    * `saved` - the saved searches; `None` reads them from the engine's store.
    * `rerank` - `None` follows the engine's reranker, which the window and
      the command line set from `rerank_wanted`; a bool asks explicitly, as
      the window's toolbar does.
    * `limit` - documents wanted; `0` keeps every document the engine's
      ordinary depth found, as the window's list does. The engine is asked
      for at least its ordinary depth, so the top documents are the window's.
    * `note_saved` - count a run of each saved search used. The window does;
      a read-only surface (MCP) does not.

    Raises what the engine raises; the history half never does.
    """
    from app.search.engine import FUSED_LIMIT
    from app.search.policy import from_settings

    store = getattr(engine, "store", None)
    if saved is None:
        saved = load_saved(store)
    expanded = expand_query(text, saved)
    chosen_scope = expanded.scope or scope or "all"
    if note_saved and expanded.names:
        note_saved_runs(store, expanded.names)
    if not expanded.query:
        from app.search.engine import SearchResponse

        return SearchRun(raw=str(text or ""), expanded="", scope=chosen_scope,
                         response=SearchResponse())

    options: dict = {"scope": chosen_scope, "policy": from_settings(surface, preferences)}
    if rerank is not None:
        options["rerank"] = bool(rerank)
    if limit:
        options["limit"] = max(FUSED_LIMIT, fetch_depth(limit))
    response = search_once(engine, expanded.query, tier="full",
                           declined=tuple(declined or ()), **options)
    git = git_results(store, expanded.query,
                      start_rank=len(getattr(response, "results", None) or []) + 1)
    run = SearchRun(raw=str(text or ""), expanded=expanded.query, scope=chosen_scope,
                    response=response, git=git, saved_names=expanded.names)
    run.documents = documents(run.results, store=store, limit=limit, marks=marks)
    return run
