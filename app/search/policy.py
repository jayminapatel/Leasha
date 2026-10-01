r"""What each search surface is allowed to do on the person's behalf.

Layer: L4 — pure. One dataclass, a table of per-surface defaults, and nothing
else. No Qt, no store, no I/O.

**The whole point is that no view branches on which tab it is.** Files, Mail
and Code were about to grow `if self.is_search_tab:` in a dozen places, and
each one would have been a rule written twice - once where it was decided and
once where it was almost decided. The engine reads a policy; the surface says
which policy it has; the difference between tabs is a row in a table anybody
can read.

**Progressive disclosure by tab**, from the order's §0. The first Search tab is
the universal surface and its acceptance test is literal: *an 8-year-old finds
her homework.* Everything that helps is on. Files, Mail and Code are power
surfaces whose users typed `/type:pdf` on purpose, and a tool that second-
guesses an expert is a tool the expert switches off - so the conservative
column is not timidity, it is a different contract.

**Every behaviour ships on and every one can be switched off**, per §0's
principle 2: people mature, and nobody should be forced in a direction. What is
*not* configurable is stated in `SAFETY`, on purpose, here, so nobody debates
it later.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Optional

__all__ = [
    "SearchPolicy",
    "SURFACES",
    "BEHAVIOURS",
    "SAFETY",
    "for_surface",
    "from_settings",
    "describe",
]

#: The surfaces, by the name their settings key uses.
SEARCH = "search"
FILES = "files"
MAIL = "mail"
CODE = "code"
SURFACES = (SEARCH, FILES, MAIL, CODE)

SURFACE_LABELS = {
    SEARCH: "Search",
    FILES: "Files",
    MAIL: "Mail",
    CODE: "Code",
}

#: **Not configurable, and written down rather than assumed.** §0's principle
#: 3. Each of these is a promise the application makes regardless of any
#: setting, and the reason to list them here is that a list nobody wrote is a
#: list somebody eventually argues with.
SAFETY = (
    "No search ever deletes or alters one of your files.",
    "Nothing you type or index leaves this computer.",
    "When search is working with less than it should, it says so.",
)


@dataclass(frozen=True)
class SearchPolicy:
    """What one surface may do without being asked.

    Frozen because a policy is a decision already taken: a view that could
    mutate the one it was handed would be branching on the tab again, in a
    less visible way.
    """

    #: Zero results with several words: drop the rarest and try again, saying
    #: so on the page. **The label is what makes it legal** - silent rewriting
    #: is what loses trust, and this codebase has said so since the parser.
    relax_on_empty: bool = True
    #: `auto` joins the best spelling candidate to the query and says so;
    #: `suggest` offers it as a chip; `off` does neither.
    typo_correction: str = "auto"
    #: `plain` translates every notice into a sentence anybody can act on;
    #: `technical` keeps today's wording, which the power surfaces earned.
    notice_register: str = "plain"
    #: Filters the rules translator recognised appear as removable chips
    #: rather than being written into the query.
    auto_chips: bool = True
    #: A mild preference for recent documents in the fused score.
    recency_blend: bool = True
    #: Near-identical results collapse to one row, newest shown.
    version_folding: bool = True
    #: Each result can say why it is on the page, from signals already
    #: recorded. **Facts, never scores** - see `presenter.why_result`.
    explain_results: bool = True

    def with_overrides(self, **changes: Any) -> "SearchPolicy":
        """A copy with some fields changed. Unknown names are ignored.

        Ignored rather than raising because the caller is often a settings
        dictionary that may hold keys from a newer build - and a search that
        refuses to run because it met an unfamiliar preference is a worse
        outcome than one that ignores it.
        """
        known = {field.name for field in fields(self)}
        return replace(self, **{name: value for name, value in changes.items()
                                if name in known})


#: The behaviours, in the order the Settings grid shows them, with the sentence
#: each one owes the person reading it.
#:
#: **The tooltip states the effect**, §0's principle 5 - what it does and what
#: happens at the edge of it, in plain words. That is already the house style;
#: this table is where the rule is kept for search.
BEHAVIOURS = (
    ("typo_correction", "Fix obvious spelling",
     "When a word matches nothing at all, look for the closest real word and "
     "say which one was used. Off means a misspelt word simply finds nothing."),
    ("relax_on_empty", "Try again with fewer words",
     "When nothing matches all your words, drop the rarest one and show what "
     "matches the rest - labelled, so you can see what was dropped."),
    ("auto_chips", "Offer filters it recognises",
     "A name, a date or a kind of file in what you typed becomes a chip you "
     "can accept or dismiss. What you typed is never changed."),
    ("recency_blend", "Prefer recent documents",
     "Among equally good matches, newer ones come first. It never hides an "
     "older document, it only orders them."),
    ("version_folding", "Fold older versions together",
     "Near-identical documents collapse into one row with the newest shown "
     "and the rest one click away."),
    ("explain_results", "Say why a result is here",
     "Each result can show what put it there - which of your words are in it, "
     "whether it was found by meaning, how recent it is, whether you have "
     "opened it before. Facts only: it never invents a score."),
    ("notice_register", "Explain in plain words",
     "Messages about what search could and could not do are written for "
     "anybody rather than for a developer."),
)

#: **1 October 2026, owner: plain English is read the same way on every tab.**
#: "mail about holiday from maya" did three different things on Search, Mail
#: and Files, which the owner called confusing; `auto_chips` is no longer
#: switched off anywhere by default. The rest of this table is unchanged.
#:
#: Per-surface defaults. **Everything on for the universal tab, conservative
#: for the power surfaces** - §0's first principle, as data rather than as an
#: `if` in four views.
#:
#: Files, Mail and Code keep `typo_correction="suggest"` rather than `off`:
#: somebody who mistyped still wants to be told, they just do not want their
#: query altered underneath them. That is the whole difference between the two
#: columns - the power surfaces propose, the universal one acts.
_DEFAULTS: dict[str, dict[str, Any]] = {
    SEARCH: {},                                  # the dataclass defaults
    FILES: {
        "typo_correction": "suggest",
        "notice_register": "technical",
        "version_folding": False,
    },
    MAIL: {
        "typo_correction": "suggest",
        "notice_register": "technical",
    },
    CODE: {
        # **Never on the Code tab.** A misspelt identifier is not a misspelt
        # word: `recieve_handler` may be exactly what is in the codebase, and
        # "helpfully" searching for `receive_handler` instead hides the thing
        # somebody is looking for. Relaxation is off for the same reason - a
        # coder pasting three terms means all three.
        "typo_correction": "off",
        "relax_on_empty": False,
        "notice_register": "technical",
        "recency_blend": False,
        "version_folding": False,
    },
}


def for_surface(surface: str) -> SearchPolicy:
    """The default policy for one surface. Unknown names get the strict one.

    Unknown means a new tab that has not been thought about, and the safe
    answer there is the power-surface contract: propose, never act.
    """
    name = str(surface or "").strip().lower()
    if name not in _DEFAULTS:
        return SearchPolicy(**_DEFAULTS[CODE])
    return SearchPolicy(**_DEFAULTS[name])


#: A behaviour that is "off". Several spellings because two of the six fields
#: are choices rather than switches, and a person switching something off does
#: not care which kind it is.
OFF = (False, "off", "technical", "", None)

#: What "off" *means* for each field, where it is not simply `False`.
#:
#: Written out because the two non-boolean fields have different off-states and
#: guessing either would be wrong: a register with no value is not a register,
#: and `typo_correction="off"` is a real, named choice a person can make.
OFF_VALUE = {
    "typo_correction": "off",
    "notice_register": "technical",
}


def from_settings(surface: str, stored: Optional[dict] = None) -> SearchPolicy:
    r"""The surface's default, narrowed by what the person has switched off.

    **A global switch can only turn a behaviour off, never force it on.** That
    is the rule, and it is what makes six settings do the work of twenty-four:
    off means off everywhere, and on means "follow this surface's contract".

    The alternative - a global `on` overriding each surface - would put plain-
    words notices on the Code tab and spelling correction on identifiers, which
    is precisely what the per-surface defaults exist to prevent. Somebody who
    genuinely wants that gets it from the per-surface keys below.

    `stored` accepts both: a bare behaviour name is the global switch, and
    `"<surface>:<behaviour>"` is one cell of the grid. The cell wins, because
    it is the more specific statement of intent.
    """
    policy = for_surface(surface)
    if not stored:
        return policy

    name = str(surface or "").strip().lower()
    prefix = f"{name}:"

    # Global first, and only ever as a veto.
    changes: dict[str, Any] = {}
    for key, value in stored.items():
        key = str(key)
        if ":" in key or value not in OFF:
            continue
        if hasattr(policy, key):
            changes[key] = OFF_VALUE.get(key, False)

    # Then the per-cell values, which may switch something back on.
    changes.update({key[len(prefix):]: value for key, value in stored.items()
                    if str(key).startswith(prefix)})
    return policy.with_overrides(**changes) if changes else policy


#: `Settings` field -> the behaviour it switches off.
#:
#: Written out rather than derived, because the two names differ on purpose:
#: `SEARCH_FIX_SPELLING` reads as a setting and `typo_correction` reads as a
#: policy field, and forcing either to match the other would make one of them
#: worse.
SETTING_FIELDS = {
    "search_fix_spelling": "typo_correction",
    "search_relax_on_empty": "relax_on_empty",
    "search_auto_chips": "auto_chips",
    "search_recency_blend": "recency_blend",
    "search_version_folding": "version_folding",
    "search_plain_words": "notice_register",
    "search_explain_results": "explain_results",
}


def preferences(settings: Any, overrides: Optional[dict] = None) -> dict:
    r"""`Settings` as the dictionary `from_settings` wants.

    One conversion, in the layer that knows both names, so no caller has to
    hold the mapping twice. Values that are already the default are still
    included - `from_settings` only acts on the ones that are *off*, so passing
    everything costs nothing and keeps this free of a second rule about what to
    send.

    `overrides` is what a settings panel has just written, keyed by the
    lower-cased `.env` name and holding **raw strings**, because that is what
    `.env` holds. Converted here rather than at the call site: a caller doing
    it would be a second place that has to know `"false"` is a boolean, and the
    second place is the one that gets it wrong.
    """
    found: dict = {}
    merged = {**{field: getattr(settings, field, None)
                 for field in SETTING_FIELDS},
              **{str(key).lower(): value
                 for key, value in (overrides or {}).items()}}

    for field, behaviour in SETTING_FIELDS.items():
        value = merged.get(field)
        if value is None:
            continue
        if behaviour == "notice_register":
            # A switch on one side, a register on the other: off means
            # technical, which is the register the power surfaces use anyway.
            found[behaviour] = "plain" if _truthy(value) else "technical"
        elif behaviour == "typo_correction":
            found[behaviour] = str(value).strip().lower()
        else:
            found[behaviour] = _truthy(value)
    return found


def _truthy(value: Any) -> bool:
    """`.env` holds strings; a panel holds booleans. Both arrive here."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def describe(policy: SearchPolicy) -> list[str]:
    """What this policy does, one sentence per behaviour that is on.

    Shown where somebody asks why a search behaved as it did. A policy that
    cannot explain itself is a policy people work around.
    """
    said: list[str] = []
    for name, label, _help in BEHAVIOURS:
        if name == "explain_results":
            # **Not listed, because it does nothing to the search.** This
            # function answers "why did my search behave like that", and the
            # other six all alter what comes back or how it is worded. An
            # explanation of a result is an affordance beside it; putting it
            # here would make the Code tab - which acts on nobody's behalf -
            # appear to be doing something.
            continue
        value = getattr(policy, name, None)
        if value in (False, "off", ""):
            continue
        if name == "typo_correction" and value == "suggest":
            said.append("Suggest a spelling, but do not change what you typed")
            continue
        if name == "notice_register" and value != "plain":
            continue
        said.append(label)
    return said
