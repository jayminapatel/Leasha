"""Which search runs when, and what the window says about it.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import logger

_log = logger.bind(component="ui.presenter")


#: Milliseconds of stillness before the interim (keyword-only) tier runs.
TYPING_DEBOUNCE_MS = 150

#: Milliseconds of stillness before the full hybrid pipeline runs. Longer,
#: because it costs a model call and the person may not have finished thinking.
IDLE_DEBOUNCE_MS = 400

#: Below this many characters, a query is too vague to spend the full pipeline
#: on - "co" matches half the corpus and the answer is not useful anyway.
MIN_FULL_SEARCH_CHARS = 3


class Tier:
    """Which search a keystroke earns: none, the keyword glance, or the full pipeline."""
    NONE = "none"
    INTERIM = "interim"
    FULL = "full"


def tier_for(query: str, *, still_for_ms: int, submitted: bool = False) -> str:
    """Which search to run, given how long the person has stopped typing.

    Pressing Enter always means the full pipeline, however short the query and
    however recently a key was pressed: it is an explicit statement of intent and
    second-guessing it is infuriating.
    """
    text = query.strip()
    if not text:
        return Tier.NONE
    if submitted:
        return Tier.FULL
    if len(text) < MIN_FULL_SEARCH_CHARS:
        # Still worth a keyword glance - prefix matching makes even two
        # characters useful - but not worth embedding.
        return Tier.INTERIM if still_for_ms >= TYPING_DEBOUNCE_MS else Tier.NONE
    if still_for_ms >= IDLE_DEBOUNCE_MS:
        return Tier.FULL
    if still_for_ms >= TYPING_DEBOUNCE_MS:
        return Tier.INTERIM
    return Tier.NONE


def notice_register_for(surface: str, preferences: Any = None) -> str:
    """`"plain"` or `"technical"` for this surface, right now - item 4b.

    The same seam `search_options` resolves a `SearchPolicy` through
    (`notice_register`), so a person who has switched off "Explain in plain
    words" gets exact dates on the Search tab too, not just on the power
    surfaces whose default already is technical.
    """
    from app.search.policy import from_settings

    return from_settings(surface, preferences).notice_register


def interpret_message(translation: Any) -> tuple[Optional[str], str]:
    """`(new box text or None, status line)` for a finished interpretation.

    `None` for the box means leave what the person typed alone. A translation
    that changed nothing must not overwrite the box with an identical string -
    it would move the cursor and clear the selection for no reason, which reads
    as the button having done something destructive.
    """
    if getattr(translation, "changed", False):
        return str(translation.query), str(getattr(translation, "note", "") or "")
    return None, str(getattr(translation, "note", "") or "")


def file_query(raw: str) -> Any:
    r"""One typed line, parsed - **the whole of it, not the part a tab liked.**

    This used to return `(text, extensions)`, which is a fair summary of what
    the Files tab could do at the time and the reason it could do no more.
    `/path`, `/after`, `/size`, `/repo` and every mail field were parsed
    correctly and then thrown away here, one function above the store call - so
    the dropdown offered filters that did nothing, and the same query typed in
    Files and in Search returned two different sets.

    Now the parse travels intact to `SqliteStore.browse_files`, which applies
    every switch through the one definition in `storage/filters.py`. The tab
    decides which rows it is about; it no longer decides what a switch means.

    Note what is *not* here any more: the old special-casing that folded
    `parsed.names` into the free text. `/name` is an ordinary filter on the
    basename, and leaving it as one is what makes it narrow a content search to
    filenames without a mode flag.
    """
    from app.search.commands import expand_slashes
    from app.search.query import parse_query

    return parse_query(expand_slashes((raw or "").strip()))


def search_options(tier: str, *, scope: str, rerank: bool,
                   surface: str = "search", preferences: Any = None,
                   declined: Any = None) -> dict:
    r"""What to pass the engine for one tier.

    `rerank` is only meaningful on the full tier - the interim one is BM25 with
    no model at all, and passing it there would look like a setting that does
    nothing. Here rather than in the view because "which options apply to which
    tier" is a rule, and a rule inside a widget is a rule nobody can test.

    `surface` chooses the **policy** - what this tab may do on the person's
    behalf. It is resolved here, in the one function every search already goes
    through, precisely so that no view ever grows `if self.is_search_tab:`.
    That was the alternative, and it would have written each rule twice: once
    where it was decided and once where it was almost decided.
    """
    from app.search.policy import from_settings

    options: dict[str, Any] = {
        "scope": scope,
        "policy": from_settings(surface, preferences),
    }
    if tier == Tier.FULL:
        options["rerank"] = bool(rerank)
    # **Both tiers**, so the keyword glance and the full search answer the
    # same question - otherwise "mail from 2017" would flip between the words
    # and the filters as the second tier landed. `SearchWorker` takes this key
    # off before the engine sees the options (see `auto_filters`).
    if declined is not None and options["policy"].auto_chips:
        options["declined"] = tuple(declined)
    return options


def results_message(response: Any) -> tuple[str, str]:
    """`(summary, status)` for a completed search.

    Three branches deciding two strings, which is logic - and logic inside a Qt
    widget can only be checked by a person searching for the right thing at the
    right moment.

    The precedence matters and is the reason this is one function rather than
    three scattered `setText` calls: an ignored operator is the most actionable
    thing that can be said, so it wins; a dead semantic half is next, because
    silent degradation is how "search feels worse than it should" goes
    unreported for weeks; the plain count is the fallback.
    """
    parsed = getattr(response, "parsed", None)
    summary = status_line(response)

    if not getattr(response, "results", None):
        hint = ""
        if parsed is not None and getattr(parsed, "has_filters", False):
            # The single most common cause of a surprising empty result, and
            # invisible otherwise: the filters are doing exactly what they were
            # told and excluding everything.
            hint = "  The filters may be excluding everything."
        return f"No results.{hint}", summary

    # **The most actionable thing that can be said, so it outranks the rest.**
    # A word matching nothing is usually the entire explanation for a baffling
    # list: a typo, a name spelled differently in the documents, or something
    # not indexed yet. Twenty results with no clue why is what this replaces.
    unmatched = list(getattr(response, "unmatched", ()) or ())
    if unmatched:
        words = ", ".join(f"'{word}'" for word in unmatched)
        found = "no document contains" if len(unmatched) == 1 else "no document contains any of"
        return summary, (
            f"{found} {words} — the rest of your words were searched for, "
            f"which is why these results may look unrelated."
        )

    unknown = list(getattr(parsed, "unknown_operators", ()) or ()) if parsed else []
    if unknown:
        return summary, "Ignored: " + ", ".join(unknown)
    return summary, semantic_health(response) or ""


def status_line(response: Any) -> str:
    """The line under the search box: how many, how fast, and with what caveats.

    **The caveats are the point.** "12 results" beside a list that is still
    being reranked is a different claim from "12 results" beside a finished one,
    and a cached result that looks identical to a fresh one is how somebody
    concludes the index is not picking up their new files. Each qualifier is
    there because leaving it out would let the line say something untrue.
    """
    bits = [
        f"{len(getattr(response, 'results', []) or [])} result(s)",
        f"{getattr(response, 'elapsed_ms', 0) or 0:.0f}ms",
    ]
    if getattr(response, "interim", False):
        bits.append("keyword only, still searching…")
    if getattr(response, "from_cache", False):
        bits.append("cached")
    if getattr(response, "reranked", False):
        bits.append("reranked")
    return "  ·  ".join(bits)


def search_shape(response: Any, *, query_len: int, scope: str) -> dict[str, Any]:
    """One completed search, described by shape only - never by its text.

    For the debug recorder. `keyword_hits` and `vector_hits` are the two fields
    that justify the whole thing: `vector.search` returns `[]` for an empty
    vector store, a failed embedding or a LanceDB hiccup, and search carries on
    with keyword results because half a search beats none. That is the right
    behaviour and it makes the failure **invisible** - results look thin, and
    nothing distinguishes "the corpus is thin" from "the semantic half is dead".

    `vector_hits == 0` beside a healthy `keyword_hits` is the signal, and a
    session file carries it without anybody having to know to ask.
    """
    parsed = getattr(response, "parsed", None)
    return {
        "tier": "interim" if getattr(response, "interim", False) else "full",
        "query_len": query_len,
        "terms": len(parsed.terms) if parsed else 0,
        "phrases": len(parsed.phrases) if parsed else 0,
        "filters": bool(parsed and parsed.has_filters),
        "scope": scope,
        "results": len(getattr(response, "results", ())),
        "keyword_hits": getattr(response, "keyword_count", None),
        "vector_hits": getattr(response, "vector_count", None),
        "elapsed_ms": round(getattr(response, "elapsed_ms", 0.0), 1),
        "reranked": bool(getattr(response, "reranked", False)),
        "from_cache": bool(getattr(response, "from_cache", False)),
    }


def semantic_health(response: Any) -> Optional[str]:
    """A sentence for the status bar when meaning-based search is not working.

    None when it is fine. The condition - keyword results but no vector results -
    is not something a person can infer from a results list, and the fix is a
    single command, so saying it is far better than letting the results quietly
    be worse than they should be.
    """
    keyword = getattr(response, "keyword_count", 0) or 0
    vector = getattr(response, "vector_count", 0) or 0
    if keyword and not vector:
        return ("Keyword results only - meaning-based search returned nothing. "
                "Check it with: app.cli stats")
    return None


@dataclass(frozen=True, slots=True)
class GitPass:
    """Whether a search should also read repository history, and what to say.

    **A decision, so it lives here.** The view's job is to start a worker; which
    switches make a search slow, and how to describe that to somebody waiting,
    are questions with right answers and belong where they can be tested without
    a display. `search_view.py` is held under 250 lines by
    `test_every_qt_view_keeps_its_logic_in_the_presenter`, and that guard was
    right to fire when this logic first went in there.
    """

    switches: tuple[str, ...] = ()
    #: Where the git rows' ranks start, so the two halves read as one list.
    rank_base: int = 1

    @property
    def wanted(self) -> bool:
        return bool(self.switches)

    @property
    def status(self) -> str:
        """The waiting sentence, naming the switches that reach into history."""
        if not self.switches:
            return ""
        return "Reading repository history for /" + ", /".join(self.switches) + "…"


def git_pass(query: str, tier: str, *, shown: int = 0) -> GitPass:
    r"""Should this search also read history, and from which rank?

    **Full tier only.** `git log -S` walks every commit it is given and takes
    seconds; the first non-negotiable in this application is that no unbounded
    work sits behind a keystroke. An interim search is a keystroke by another
    name, so it never qualifies however the query is written.

    Cheap on the searches that will never touch git, which is nearly all of
    them: `wants_git` is a regex and two dictionary lookups.
    """
    if tier != Tier.FULL:
        return GitPass()

    from app.search.gitquery import wants_git

    return GitPass(switches=wants_git(query), rank_base=max(0, int(shown)) + 1)


def federated_summary(index_rows: int, git_rows: int) -> str:
    """The status line once both halves have answered.

    Said as two numbers rather than one total, because *"41 results"* hides that
    thirty of them are commits from 2016 - and which half a result came from is
    the first thing somebody wants to know when history is involved.
    """
    if not git_rows:
        return ""
    return (f"{index_rows:,} from the index, "
            f"{git_rows:,} from repository history")


#: Words next to which a kind word means a file type rather than a subject.
#:
#: *"excel formula"* and *"word count"* are real searches about spreadsheets and
#: writing; *"excel file"* and *"as a word document"* are somebody naming a type.
#: The difference is the noun beside it, which is why this exists rather than a
#: bare list of kind words.
_DOCUMENT_NOUNS = frozenset({
    "document", "documents", "doc", "docs", "file", "files", "format",
    "attachment", "attachments", "spreadsheet", "presentation", "deck",
})


def kind_suggestion(raw: str) -> tuple[str, str]:
    r"""`(kind word, the switch to offer)`, or `("", "")`.

    From `WORKORDER-202626081059-search-quality.md` F2. `_EXT_GROUPS` has known
    twelve kind words - `excel`, `word`, `mail`, `code` - since Layer 4, and
    `/type excel` works perfectly. **"get me all excel files" does nothing**: the
    word sits in `terms` and no `ext` filter is produced. The vocabulary is
    there; the bridge from prose to filter is not, and that bridge is what the
    owner expected.

    **This offers, and never applies.** A non-negotiable says a query the
    application altered must be visible and editable, because invisible
    narrowing makes search unpredictable - and silently filtering on a guessed
    word is how somebody loses a document and never learns why. So this returns
    something to *show*; applying it is a click.

    The kind word must sit next to a document noun, which is what separates
    *"as a word document"* from *"word count"*.
    """
    from app.search.query import _EXT_GROUPS

    words = [word.strip(".,;:!?").lower() for word in str(raw or "").split()]
    for index, word in enumerate(words):
        if word not in _EXT_GROUPS:
            continue
        neighbours = words[max(0, index - 1):index] + words[index + 1:index + 2]
        if any(near in _DOCUMENT_NOUNS for near in neighbours):
            return word, f"type:{word}"
    return "", ""


def interpret_hint(raw: str, *, enabled: bool) -> str:
    r"""Whether to point at the Interpret button, and what to say.

    F7: Interpret is the designed answer to a sentence like *"find a project
    execution plan as a word document"* - turning it into `type:docx` is
    precisely its job - and the owner did not press it. **That is a
    discoverability fault, not a user error**: a button whose value is invisible
    until pressed will not be pressed.

    Offered only when it would actually help: a long query, no operators
    already typed, and a kind word in it. Anything looser is a hint on every
    search, which is a hint nobody reads.

    This does not replace the parsing fixes. Search must work with Ollama
    stopped - that is the first non-negotiable - so Interpret makes good queries
    better rather than making bad parsing acceptable.
    """
    text = str(raw or "").strip()
    if not enabled or len(text.split()) < 5 or ":" in text or "/" in text:
        return ""
    if not kind_suggestion(text)[0]:
        return ""
    return "This looks like a sentence — press Interpret to turn it into filters."


#: Codes for the two notices the *window* raises, as opposed to the engine.
NOTICE_KIND_SUGGESTION = "NOTICE_KIND_SUGGESTION"
NOTICE_INTERPRET_HINT = "NOTICE_INTERPRET_HINT"
NOTICE_FILTER_OFFER = "NOTICE_FILTER_OFFER"
#: A date the parser could not read, said in words (order "dates" §1d).
NOTICE_DATE_PROBLEM = "NOTICE_DATE_PROBLEM"


@dataclass(frozen=True, slots=True)
class _Hint:
    """Shaped like `engine.Notice`, because the bar branches on `code`."""

    code: str
    message: str


def result_view_state(response: Any, raw: str, *,
                      interpret_enabled: bool = False) -> tuple:
    """Everything the results handler needs, decided in one place.

    Returns `(notices, terms, summary, status)`. Four small decisions that were
    four statements in the view - and `search_view.py` is held under 250 lines
    by `test_every_qt_view_keeps_its_logic_in_the_presenter`, which fired when
    the kind-word suggestion went in. None of these needs a window to compute
    and none of them was ever view logic.
    """
    parsed = getattr(response, "parsed", None)
    notices = [
        *getattr(response, "notices", ()),
        *window_notices(raw, parsed, interpret_enabled=interpret_enabled),
    ]
    terms = (list(parsed.terms) + list(parsed.phrases)) if parsed else []
    summary, status = results_message(response)
    return notices, terms, summary, (status or summary)


def filter_offers(chips: Any, raw: str) -> list[Any]:
    r"""The recognised filters (`chips_for`) as offers on the notice bar.

    **Order 0m section 1b's scenario found these were never shown.** `chips_for`
    was built, tested at the presenter and ticked (search-experience 3b), and
    nothing in the window ever called it - a child typing "the email Dave sent"
    saw no offer at all. The notice bar is the surface that already carries
    click-to-apply suggestions (`apply:` links, `SearchView._apply_suggestion`),
    so an offer is the same shape: **the typed words stay exactly as typed**,
    and one click appends the filter, which then shows as a removable chip.

    A filter the person has already typed is not offered again.
    """
    import html

    typed = str(raw or "").lower()
    offers: list[Any] = []
    for chip in chips or ():
        operator = chip.as_filter()
        if f"{chip.field}:" in typed:
            continue
        # `date:` sets both edges, so it has already said what either would.
        if chip.field in ("after", "before") and ("date:" in typed or "/date" in typed):
            continue
        # So does `between:`, which is `date:` under another name (order 0x
        # §6a). A separate test rather than a longer one above, so the line
        # `date:` has always been answered by is left exactly as it was.
        if chip.field in ("after", "before") and ("between:" in typed or "/between" in typed):
            continue
        shown = html.escape(chip.label())
        offers.append(_Hint(
            NOTICE_FILTER_OFFER,
            f'Only show results <a href="apply:{html.escape(operator, quote=True)}">'
            f"{shown}</a>?"))
    return offers


def window_notices(raw: str, parsed: Any = None, *,
                   interpret_enabled: bool = False) -> list[Any]:
    r"""The suggestions the window adds to the engine's own notices.

    Both are **offers**, and that is the whole design. A non-negotiable says a
    query the application altered must be visible and editable; silently
    filtering on a guessed kind word is how somebody loses a document and never
    learns why. So these are things to show, and applying one is a click.

    Nothing is suggested once the person has already said it - a query carrying
    `type:` needs no help choosing a type, and a hint on every search is a hint
    nobody reads.
    """
    import html

    found: list[Any] = []
    # **First, because it is the one that explains a surprising list.** A
    # date the parser could not read is a filter that is not there, and the
    # list below it is wider than was asked for. Escaped: the bar draws rich
    # text, and the sentence quotes whatever was typed.
    for problem in getattr(parsed, "date_problems", ()) or ():
        found.append(_Hint(NOTICE_DATE_PROBLEM, html.escape(str(problem), quote=False)))
    if not getattr(parsed, "ext", ()):
        word, switch = kind_suggestion(raw)
        if word:
            found.append(_Hint(
                NOTICE_KIND_SUGGESTION,
                f'Search <a href="apply:{switch}">{word} files only</a>? '
                f"Your results are not filtered by type."))
    hint = interpret_hint(raw, enabled=interpret_enabled)
    if hint:
        found.append(_Hint(NOTICE_INTERPRET_HINT, hint))
    return found


# ---------------------------------------------------------------------------
# Search notices
# ---------------------------------------------------------------------------

def with_date_problems(line: str, parsed: Any) -> str:
    r"""A tab's summary line, with any unreadable date said first.

    Order "dates" §1d, for the boxes whose only place for a notice is the
    line under them - Files, Mail and Code. Here rather than in each view
    because all three are within a few lines of the 250-line guard, and
    because "which comes first" is a decision: the date, since a filter that
    is silently not there is the whole explanation for the list beneath it.
    """
    problems = [str(p) for p in getattr(parsed, "date_problems", ()) or () if str(p)]
    if not problems:
        return line
    parts = [f"\u26a0 {problem}" for problem in problems]
    return "  ·  ".join([*parts, line] if line else parts)


def notice_line(notices: Any) -> str:
    """One line for the notice bar, or "" when the search was healthy.

    **Here rather than in the widget, so it can be tested.** `QtWidgets` needs
    a display, and the last time UI logic was verified by reading it instead of
    running it, `QPdfView()` shipped without its parent argument and crashed
    the window on startup. Everything that decides *what* is shown lives in
    this module; the widget only draws it.

    Reads `message` and nothing else. The wording is the backend's and is free
    to change; `code` is what anything branching must use, and there is a test
    asserting the UI never parses a message string to decide anything.
    """
    if not notices:
        return ""
    messages = [
        str(getattr(notice, "message", "")).strip()
        for notice in notices
        if str(getattr(notice, "message", "")).strip()
    ]
    if not messages:
        return ""
    # Two spaces between, not a newline: the bar is one line that wraps, and a
    # hard break makes a single notice and two notices look like different
    # kinds of thing.
    return "  ".join(f"\u26a0 {message}" for message in messages)


# ---------------------------------------------------------------------------
# §3b — the filters the rules recognised, offered beside what was typed
# ---------------------------------------------------------------------------

def chips_for(store: Any, sentence: str, policy: Any = None) -> tuple:
    r"""Filters the rules translator read out of a sentence. **Never raises.**

    **Here rather than in the engine, and that is a rule not a preference.**
    `test_the_search_engine_cannot_reach_the_translator` forbids `engine.py`
    from knowing translation exists, because the retrieval path must never be
    able to spend a second on a model. The first version of this put chips on
    the response and tripped that guard - the same trap §1 hit with a module
    called `translate`, and the guard was right both times. Chips are a thing
    said *about* a query, not part of running one, so they belong with the
    translator, on the presenter side, where the Interpret button already is.

    **The policy still decides**, so no view carries a rule of its own: this
    reads `auto_chips` exactly as the engine reads the other five behaviours.

    Deterministic and offline - no model, no network - which is what makes
    this the half of Interpret that works on every machine.
    """
    if policy is not None and not getattr(policy, "auto_chips", True):
        return ()
    try:
        from app.search.translate_rules import read

        return tuple(read(sentence, store).chips)
    except Exception as exc:                       # noqa: BLE001 - a helper
        _log.debug("no filter chips for this query: {}", exc)
        return ()


def auto_filters(store: Any, sentence: str, policy: Any = None,
                 declined: Any = ()) -> tuple[str, tuple]:
    r"""`(query to run, filters applied)` for a typed sentence. **Worker only,
    never raises.**

    **Owner decision 2026-09-27: "mail from 2017" applies its filters.** It
    used to run as the words `"mail" OR "2017"`, with the right filters only
    offered on the notice bar - see `translate_rules.apply` for exactly which
    readings are confident enough to act on and why "invoice 2017" is not one.
    The box keeps what was typed; the query that runs loses the words a filter
    consumed and gains the filter, and the window draws each applied filter as
    a removable chip (`ChipRow.show_applied`). Removing one adds its key to
    `declined`, and those words go back to being ordinary search terms.

    Here, on the presenter side, for the reason `chips_for` is: the engine may
    not know translation exists. `auto_chips` decides, as it does for the
    offers, so the power surfaces (off by default) keep their words as typed.
    Called from `SearchWorker.run`, because the reading asks the store for its
    senders and file types - a query, so never on the interface thread.
    """
    # 2026-10-04: the reading itself is `app.search.run.read_filters`, so the
    # command line, the shell, the mini box and the MCP server read a sentence
    # exactly as this tab does. This name stays for the callers that use it.
    from app.search.run import read_filters

    return read_filters(store, sentence, policy, declined)


#: Which offered chip fields an applied filter already answers, so the bar
#: does not offer "only 2017?" beside a search already limited to 2017.
_ANSWERED_BY = {"type": ("type",), "date": ("after", "before"),
                "person": ("from", "to")}


def unanswered(chips: Any, applied: Any) -> tuple:
    """The offered `chips` no applied filter already covers."""
    covered = {field for chosen in applied or ()
               for field in _ANSWERED_BY.get(getattr(chosen, "kind", ""), ())}
    return tuple(chip for chip in chips or () if chip.field not in covered)
