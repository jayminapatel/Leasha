"""Why a result is here, and how it matched.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from app.core.logging import logger

_log = logger.bind(component="ui.presenter")


# ---------------------------------------------------------------------------
# Adoptions §1 — "why is this result here?"
# ---------------------------------------------------------------------------

#: How recent a document has to be before recency is worth mentioning.
#:
#: 0.5 is one half-life - about six months. Below that the blend contributed
#: almost nothing and saying so would be **noise dressed as an explanation**,
#: which is the failure this whole feature is trying to avoid.
RECENT_ENOUGH = 0.5


def _matched_words(result: Any, parsed: Any) -> tuple:
    """Which of the typed words actually appear in this passage.

    **Read off the text rather than inferred from the score**, because that
    is the difference between a fact and a guess - and a guess in the
    explanation is worse than no explanation at all.
    """
    text = str(getattr(result, "text", "") or "").lower()
    if not text:
        return ()
    found = []
    seen = set()
    for term in getattr(parsed, "terms", ()) or ():
        # **Shown as typed.** `SearchEngine` handed back as `searchengine`
        # reads as a correction of something that was not wrong, which is the
        # same discourtesy the recent-searches list was careful to avoid.
        typed = str(term or "").strip().strip("*")
        word = typed.lower()
        if len(word) > 1 and word in text and word not in seen:
            seen.add(word)
            found.append(typed)
    return tuple(found)


def _when(mtime_ns: Any) -> str:
    """A date somebody can read, or "" if there is none to show.

    2026-10-04, code review: `row_facts.day_words`, the lists' own form - this
    wrote the month in full ("17 September 2023") where every list writes
    "17 Sep 2023".
    """
    from app.core.row_facts import day_words

    return day_words(mtime_ns)


def why_result(result: Any, parsed: Any = None, *, fold: Any = None,
               opens: int = 0, register: str = "plain") -> tuple:
    r"""The plain-words reasons this result is on the page. **Never raises.**

    Adoptions §1, and the constraint is the whole point: **state facts, never
    scores.** Every line below is read from something already recorded -
    which retriever found it, whether the text contains the typed words, the
    freshness the blend used, the usage log, the fold - and no line invents a
    number or a percentage. "87% relevant" is a sentence nobody can check and
    everybody would believe.

    An empty tuple is a legitimate answer: a plain keyword hit with nothing
    else to say about it should say nothing rather than pad.

    `register` follows the notice pattern - `plain` for the everyday tab,
    `technical` where the power surfaces have earned the detail.
    """
    try:
        lines: list = []
        technical = str(register or "plain").lower() == "technical"

        words = _matched_words(result, parsed)
        if words:
            lines.append("Your words are in it: " + ", ".join(words))

        sources = tuple(getattr(result, "sources", ()) or ())
        if 0 in sources and 1 in sources:
            lines.append("Found both by your words and by meaning - the "
                         "strongest signal this search has.")
        elif sources == (1,):
            # **Said out loud, because it looks like a mistake otherwise.**
            # A row with none of the typed words in it reads as a bug to
            # somebody who does not know the search understands meaning.
            lines.append("Found by meaning rather than by matching your "
                         "words.")
        elif sources == (0,) and not words:
            lines.append("Your words are in this file, though not in the "
                         "part shown here.")

        if getattr(result, "declares", False):
            lines.append("This is where it is defined, not just used.")

        freshness = float(getattr(result, "recency", 0.0) or 0.0)
        if freshness >= RECENT_ENOUGH:
            # Shot date before copy date - the same substitution `to_row`
            # makes, so the date named here agrees with the one on the row.
            when = _when(getattr(result, "taken_at_ns", 0) or getattr(result, "mtime_ns", 0))
            lines.append(
                f"Recent, so it came slightly ahead of equally good older "
                f"ones{f' - {when}' if when else ''}.")

        if opens > 0:
            # **A fact about them, not a score.** "You have opened this
            # before" is checkable; "popular" is not.
            lines.append("You have opened this before."
                         if opens == 1 else
                         f"You have opened this {opens} times before.")

        older = tuple(getattr(fold, "older", ()) or ()) if fold else ()
        if older:
            lines.append(f"{len(older)} other cop{'y' if len(older) == 1 else 'ies'}"
                         f" of this were folded into this row.")

        if technical and getattr(result, "rerank_score", None) is not None:
            lines.append("Reranked by the cross-encoder.")
        return tuple(lines)
    except Exception as exc:                       # noqa: BLE001 - a courtesy
        _log.debug("no explanation for this result: {}", exc)
        return ()


def explain_for(result: Any, parsed: Any = None, policy: Any = None, *,
                fold: Any = None, opens: int = 0) -> tuple:
    """`why_result`, with the switch and the register read off the policy.

    **The policy decides here rather than in the view**, exactly as
    `chips_for` does - so no surface carries a rule of its own and the
    off-switch cannot be honoured in three places and forgotten in a fourth.
    """
    if policy is not None and not getattr(policy, "explain_results", True):
        return ()
    register = str(getattr(policy, "notice_register", "plain") or "plain")
    return why_result(result, parsed, fold=fold, opens=opens,
                      register=register)


# ---------------------------------------------------------------------------
# Adoptions §2 — how it matched, not only that it did
# ---------------------------------------------------------------------------

#: What a result says about the way it was found. **Words, never colour
#: alone**: the accessibility rule this codebase already applies to the focus
#: ring applies here too, and a marker that is only a hue is a marker several
#: people on any given day cannot see.
MEANING_MARKER = "meaning match"


def why_lines(row: Any, terms: Sequence[str] = (), prefs: Optional[dict] = None,
              *, opens: int = 0) -> tuple:
    r"""The plain-words reasons a row is on the page, for the "Why is this here?"
    menu entry. Adoptions section 1, which built `why_result` and `explain_for`
    and left nothing in the window to call them.

    `prefs` is the dictionary `SearchView` already holds
    (`app.search.policy.preferences`): its `explain_results` switch and its
    `notice_register` decide here, so the off-switch cannot be honoured in the
    menu and forgotten in the dialog. Returns `()` when the switch is off.
    """
    from types import SimpleNamespace

    prefs = prefs or {}
    policy = SimpleNamespace(
        explain_results=bool(prefs.get("explain_results", True)),
        notice_register=str(prefs.get("notice_register", "plain") or "plain"))
    parsed = SimpleNamespace(terms=tuple(terms or ()))
    return explain_for(row, parsed, policy, opens=opens)


def explain_switch_on(prefs: Optional[dict] = None) -> bool:
    """Whether "Why is this here?" is offered at all - the same switch."""
    return bool((prefs or {}).get("explain_results", True))


def match_marker(row: Any, policy: Any = None) -> str:
    r"""`"meaning match"` for a result no typed word appears in, else `""`.

    **A keyword hit keeps today's highlight and says nothing extra.** The
    marker exists for the row that has none of the person's words in it,
    which reads as a mistake to anybody who does not know the search
    understands meaning - and adding a badge to every row would make the one
    that matters invisible.

    Rides `explain_results`, deliberately: this is the shortest possible
    answer to *why is this here*, and a separate eighth switch for one word
    would be a preference nobody could tell apart from the seventh.
    """
    if policy is not None and not getattr(policy, "explain_results", True):
        return ""
    lanes = tuple(getattr(row, "sources", ()) or ())
    return MEANING_MARKER if lanes == (1,) else ""
