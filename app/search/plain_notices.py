r"""The same facts, in words an eight-year-old can act on.

Layer: L4 — pure. A table and one function.

**Two registers, one set of facts.** A notice says search did less than it
should have; that is worth saying on every surface. What differs is who is
reading. "Meaning-based search returned nothing, so these are keyword matches
only. Run `app.cli stats` to check the vector store" is exactly right on the
Code tab and useless on the first one - a child who reads it learns that
something is broken and that the fix involves a command line she does not
have.

So `notice_register="plain"` swaps the sentence and keeps the code. The code is
what callers branch on and is deliberately untouched: a notice that changed its
identity to change its wording would be a notice the UI stopped recognising.

**Every code has a plain form, and a test says so.** A missing one would fall
back to the technical sentence silently, which is the failure this table
exists to prevent - and it would happen on exactly the surface least able to
cope with it.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["PLAIN", "plain_message", "for_register"]

#: `code -> (plain sentence, or a callable given the technical message)`.
#:
#: **Plain means "says what to do", not "says less".** Each of these carries
#: the same fact as the technical form; what it drops is the vocabulary and the
#: command line, and what it adds is what the person can actually do next -
#: which for most of them is *nothing, carry on*, and saying so is the point.
PLAIN: dict[str, Any] = {
    "NOTICE_NO_VECTORS": (
        "Searching by meaning is off at the moment, so these are word matches "
        "only. You may find fewer things than usual - try different words if "
        "something is missing."
    ),
    # Work order 0h §1c. A separate sentence from `NOTICE_NO_VECTORS`,
    # because the two lanes fail independently - see that code's own
    # docstring in `app/search/engine.py`.
    "NOTICE_NO_IMAGES": (
        "Finding photos by what is in them is off at the moment. Word and "
        "meaning matches still work as usual."
    ),
    "NOTICE_RERANK_UNAVAILABLE": (
        "The best matches may not be at the very top today. Everything is "
        "here - just in a slightly rougher order."
    ),
    "NOTICE_UNMATCHED_TERMS": lambda message: (
        "Nothing here contains " + _words(message) + ", so these match the "
        "rest of what you typed."
    ),
    "NOTICE_SORTED": (
        "These are in date order, newest first - not best-match order."
    ),
    "NOTICE_WILDCARD": lambda message: (
        "Your * matched several words, and all of them were searched for."
        + (" There were too many, so only the commonest were used."
           if "capped" in message.lower() or "limit" in message.lower() else "")
    ),
    # §2a and §2b's own notices. Their technical forms exist for the power
    # surfaces; these are the sentences the universal tab shows.
    "NOTICE_SPELLING": lambda message: message,
    "NOTICE_RELAXED": lambda message: message,
    # §4a's notice, and **it was missing until the §7 coverage test asked**.
    # The technical form tells somebody to put quotes round part of it, which
    # is syntax advice on the one tab that exists so nobody needs syntax; the
    # plain form offers the same escape in words instead.
    "NOTICE_EXACT": (
        "That looked like something you pasted in, so these match your words "
        "in the order they appear. Type it in your own words to search more "
        "loosely."
    ),
}


def _words(message: str) -> str:
    r"""The terms out of "Not in the index: a, b. Results match…", as English.

    A list rendered as `a, b, c` reads as a machine's output; `a, b or c` reads
    as a sentence. Small, and the difference between a message somebody parses
    and one they simply understand.
    """
    found = re.search(r":\s*(.+?)\.", str(message))
    if not found:
        return "one of your words"
    terms = [term.strip() for term in found.group(1).split(",") if term.strip()]
    quoted = [f"'{term}'" for term in terms]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


def plain_message(code: str, message: str) -> str:
    """The plain form of one notice, or the technical one when there is none.

    Falling back rather than raising: a notice nobody translated is still a
    notice worth showing, and a search that fails because of a missing string
    would be a poor trade. `test_every_notice_has_a_plain_form` is what keeps
    the fallback from being reached.
    """
    found = PLAIN.get(str(code))
    if found is None:
        return str(message)
    if callable(found):
        try:
            return str(found(str(message)))
        except Exception:                        # noqa: BLE001 - a sentence
            return str(message)
    return str(found)


def for_register(notices: Any, register: str = "plain") -> tuple:
    r"""Rewrite a run of notices for one register. `technical` returns them as-is.

    **Not called `translate`, and that is not squeamishness.**
    `test_the_search_engine_cannot_reach_the_translator` greps `engine.py` for
    the word to enforce that the retrieval path never touches the *query*
    translator - a real architectural boundary. Importing something called
    `translate` into that module defeated the guard by accident, which is
    exactly the sort of silent erosion the guard exists to catch.

    Returns the same objects when nothing changes, so the common case costs
    nothing and identity comparisons in the UI keep working.
    """
    if str(register) != "plain" or not notices:
        return tuple(notices or ())

    from dataclasses import replace

    changed = []
    for notice in notices:
        plain = plain_message(getattr(notice, "code", ""),
                              getattr(notice, "message", ""))
        changed.append(notice if plain == notice.message
                       else replace(notice, message=plain))
    return tuple(changed)
