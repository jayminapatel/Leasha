"""The `/` menu: commands, scopes and the values offered after them.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, NamedTuple, Optional, Sequence

from app.core.logging import logger

_log = logger.bind(component="ui.presenter")


def _switch_catalogue() -> tuple[Any, ...]:
    """`app.search.commands.COMMANDS`, imported at call time.

    A module-level import would put Layer 4 into the import graph of a module
    that runs on every keystroke, which
    `test_nothing_that_runs_on_a_keystroke_imports_this` exists to prevent.
    Called once, at import, to build `ALL_COMMANDS`.
    """
    from app.search.commands import COMMANDS

    return COMMANDS


# -- what each box offers on `/` ---------------------------------------------
#
# **The search box is the union; the focused tabs are subsets of it.** Search is
# the generic one and must offer every command there is, because somebody who
# does not yet know which tab they want types there. A focused tab offers what
# it can honour and nothing else.
#
# Here rather than in `widgets/command_popup.py` because the rule is about the
# grammar, not about Qt: `test_command_subsets.py` checks each offered command
# against the field its tab's query function actually consumes, and that check
# should not need a display to run.

#: Every switch there is, in the catalogue's own order.
#:
#: **The subsets used to be much smaller, and that was the bug.** Files offered
#: two of eleven and Code three; the other eight were typed, parsed, and then
#: dropped on the floor by a tab that had no idea what to do with them. The
#: owner's report was exactly that: *"the switches Search should have all
#: switches, files should have all switches... and the results should be same
#: across but only applicable to the tab, this is not the case"*.
#:
#: What made the subsets necessary was that each tab wrote its own filtering.
#: Now they share one - `storage/filters.file_filter_sql` - so a switch means
#: the same thing everywhere and the only question left is which *rows* a tab is
#: about. That question has an answer for every switch on every tab, so the
#: subsets collapse into this.
#: Read from the catalogue rather than restated, so a switch added there is
#: offered everywhere without a second list to remember.
ALL_COMMANDS: tuple[str, ...] = tuple(
    command.name for command in _switch_catalogue()
)

#: Files: every file row. A `/from` here is not a mail search - it narrows to
#: the mail *files* on disk whose sender matches, which is a question about
#: files and belongs on the tab about files.
FILES_COMMANDS = ALL_COMMANDS

#: Mail: every switch too, for the same reason in reverse. The five mail
#: columns are what `browse_messages` was built for, and the file-level ones
#: narrow by the message's own file - its name, folder, type and size.
MAIL_COMMANDS = ALL_COMMANDS

#: Code: what Files offers. `repo:` is not special here, it is simply the switch
#: this tab is most often used with - and it worked on the other tabs already.
CODE_COMMANDS = ALL_COMMANDS


#: How many values a dropdown offers. Enough to cover a real corpus's file
#: types and a person's regular correspondents; few enough that the list is
#: still something you scan rather than search.
VALUE_LIMIT = 40

#: Per-source ceilings, where the general one is wrong.
#:
#: **`ext` is the only bounded source, and this bounds only the indexed half.**
#: A machine has perhaps eighty file types and never more; senders and folders
#: have no ceiling at all, which is what the general limit is protecting
#: against. Applying one number to both meant the safe cap for an unbounded
#: column was silently truncating a list that fits on a screen - and truncating
#: it by frequency, so the types somebody had just switched on were the first
#: to be cut.
#:
#: The *configured* half has its own, much tighter ceiling below. One number
#: for both was defensible while the application read 34 text extensions; it
#: reads 405 now, and see `catalogue_limit` for why that changes the answer
#: rather than merely the number.
VALUE_LIMITS: dict[str, int] = {"ext": 120}

#: Characters of prefix before the configured-but-not-indexed tail is offered.
CATALOGUE_PREFIX_CHARS = 2

#: How many of that tail may then be shown.
CATALOGUE_LIMIT_PREFIXED = 40


def catalogue_limit(prefix: str) -> int:
    r"""How many configured-but-unindexed types to offer for this prefix.

    **Zero until two characters are typed, and the reason is ordering rather
    than volume.**

    The indexed suggestions are sorted by frequency: the type somebody wants is
    nearly always one of the three they have thousands of, so the head of the
    menu is genuinely useful. The configured tail has no frequency to sort by -
    nothing has been indexed - so `enabled_extensions` returns it
    alphabetically. With 405 enabled formats that makes the first twelve
    `abap, ada, adb, ads, adoc, ahk...`: not a shortlist, just the front of an
    alphabet, and it would sit above `pdf` and `docx` in the one menu that
    exists to answer "what can I filter by".

    Truncating it to a smaller arbitrary number does not fix that - it is the
    same noise, shorter. What fixes it is a prefix: two characters narrow 405
    formats to a handful, and typing them is exactly the gesture somebody makes
    to check that a format they just switched on is really there. **That check
    is the whole reason the tail exists**, and it still works.

    A pure function so the rule can be argued with, and read, without a menu.
    """
    return CATALOGUE_LIMIT_PREFIXED if len(str(prefix or "").strip()) >= CATALOGUE_PREFIX_CHARS else 0

#: Parsed once. `load_rules` reads and validates two TOML files, and this is
#: reached from a keystroke - see `enabled_extensions`.
_CATALOGUE: tuple[str, ...] | None = None


def clear_format_catalogue() -> None:
    """Forget the cached format list, so the next menu rebuilds it.

    Called when the file-types editor saves. Without it a format switched on
    stays missing from `/type` until the window is restarted, which is the same
    complaint this fixed one layer down.
    """
    global _CATALOGUE
    _CATALOGUE = None


def enabled_extensions() -> tuple[str, ...]:
    """Every file type currently switched on, without dots, alphabetically.

    The registry is the authority on what can be read and configuration is an
    override on top of it - so this asks `FormatRules.describe(REGISTRY)`, which
    is the same answer `app.cli formats` prints. A second list built from either
    half alone would disagree with that command, and a filter menu that
    disagrees with the format report is worse than one that is merely short.

    Imported inside the function: `app.extract` pulls in a registry that imports
    optional libraries, and `presenter` is on the window's startup path. Same
    trade as `preview_loader._extractable`.

    Never raises. A missing or invalid config file costs the extra suggestions
    and nothing else - the index-backed ones are unaffected.
    """
    global _CATALOGUE
    if _CATALOGUE is not None:
        return _CATALOGUE

    found: list[str] = []
    try:
        from app.core.config import load_settings
        from app.core.formats import load_rules
        from app.extract.base import REGISTRY

        rules = load_rules(data_path=load_settings().data_path)
        found = sorted(
            str(row["extension"]).lstrip(".").lower()
            for row in rules.describe(REGISTRY)
            if row.get("enabled") and str(row.get("extension") or "").strip(".")
        )
    except Exception:                           # broad by design - see the docstring
        found = []

    _CATALOGUE = tuple(found)
    return _CATALOGUE


class SlashContext(NamedTuple):
    """What the box is asking for, and what it has already been told.

    A NamedTuple rather than a plain tuple because `context` is the fourth
    element and reading `slash_context(...)[3]` at a call site is how the
    meaning of a position gets forgotten. Unpacking three still fails loudly,
    which is the right way for this to change - silently dropping a filter
    would give a menu that looks right and answers the wrong question.
    """

    head: str
    mode: str
    partial: str
    context: Any = None


def slash_context(text: str, resolve: Any = None) -> SlashContext:
    """Read the word being typed: `(head, mode, partial, context)`.

    `mode` is:

    * ``"command"`` while a `/name` is being typed - offer the filters;
    * ``"value"`` once a `name:` has been settled on - offer its values;
    * ``""`` when neither applies - close the menu.

    `head` is everything before the word, so a caller can rewrite the word
    without touching what was typed before it.

    **One reader for both menus.** The alternative - a pattern per menu, each
    with its own idea of where a word begins - is how `12/03` and `D:/docs`
    end up opening a dropdown over a date and a path. Those are the cases worth
    remembering: a search box that silently rewrites what somebody typed is a
    search box they stop trusting, and this is the function that decides
    whether it is about to.

    Qt-free and here rather than in the widget, so both can be checked without
    a display.
    """
    # `resolve` is how the Code tab reads its own catalogue with this same
    # function: repository search has different switches over a different
    # engine, and one of them - `/api` for `endpoint` - collides with a path in
    # a way the index's catalogue never does.
    if resolve is None:
        from app.search.commands import command_for as resolve

    if not text or text.endswith(" "):
        return SlashContext("", "", "")

    word = text.rpartition(" ")[2]
    head = text[: len(text) - len(word)]

    if ":" in word:
        name, _sep, partial = word.partition(":")
        # A colon that is not one of ours - `D:/docs`, `http://…`, `12:30` -
        # is somebody's text and is left alone.
        if resolve(name) is not None:
            return SlashContext(head, "value", partial, _settled(head))
        return SlashContext("", "", "")

    if word.startswith("/"):
        return SlashContext(head, "command", word[1:])
    return SlashContext("", "", "")


def _settled(head: str) -> Any:
    """The filters already typed, parsed by the one parser this project has.

    **Parsed only in value mode**, which is where it is used: this function
    runs on every keystroke on the UI thread, and the command menu has no use
    for it. Never raises - a half-typed query is the normal state here, and a
    dropdown that throws while somebody is typing is worse than one that offers
    unscoped values.
    """
    text = (head or "").strip()
    if not text:
        return None
    try:
        from app.search.query import parse_query

        return parse_query(text)
    except Exception:                            # noqa: BLE001 - see docstring
        return None


def value_suggestions(store: Any, name: str, prefix: str = "",
                      limit: int = VALUE_LIMIT, resolve: Any = None,
                      lookup: Any = None, catalogue: Any = None,
                      context: Any = None,
                      notes: Optional[list[str]] = None,
                      counts: Optional[dict] = None) -> list[str]:
    """What to offer after `/type `, `/from `, `/repo `, `/after `…

    **The half of the `/` menu that was missing.** The menu said which filters
    exist and then left somebody to guess a value - and a value guessed wrong
    returns nothing, which is indistinguishable from a filter that does not
    work. Offering what is actually in the index closes that gap.

    Three sources, in this order:

    * `command.source` - read from the index, commonest first, through
      `distinct_values`, which is bounded and index-backed because this is
      reached from a keystroke.
    * `command.values` - fixed by the grammar. `/has` has exactly two answers,
      `/type` has the kind words (`excel`, `code`, `mail`) that `_EXT_GROUPS`
      expands, and a date has a handful of spellings easier to pick than to
      recall.
    * `catalogue` - for `/type` only: every format currently switched on,
      whether or not one has been indexed yet. Defaults to
      `enabled_extensions()`; injectable so the merge can be tested without a
      config file on disk.

    Qt-free and here rather than in the widget, so the rule about *what* is
    offered can be tested without a display - which is the same reason
    `FILES_COMMANDS` and its siblings live in this file.

    Never raises. A suggestion list is a convenience; a store that is mid-index,
    locked or closed must cost the suggestions and nothing else.
    """
    if resolve is None:
        from app.search.commands import command_for as resolve

    command = resolve(name)
    if command is None:
        return []

    wanted = str(prefix or "").strip().lower()
    # **Only when the caller left the default.** The per-source ceiling raises
    # a general cap that is too low for `ext`; it must never override a limit
    # somebody asked for, and `max()` on its own did exactly that - a caller
    # asking for seven got a hundred and twenty, and the query it was trying to
    # keep small was pushed down to the database at the larger size.
    if int(limit or 0) == VALUE_LIMIT:
        limit = VALUE_LIMITS.get(command.source, VALUE_LIMIT)
    limit = max(int(limit or 0), 1)
    found: list[str] = []

    #: value -> the `ValueCount` it came from, when it came from one. The
    #: merged list stays strings so every existing caller keeps working; the
    #: counts ride alongside for `value_rows` to pick up.
    counted: dict[str, Any] = {}

    def offer(values: Any, cap: Optional[int] = None) -> None:
        """Add what matches the prefix, keeping the first spelling seen.

        `cap` bounds how many *new* values this source may contribute, which is
        not the same as slicing the input: a source whose first twenty entries
        are already on the list would otherwise spend its whole allowance
        adding nothing.
        """
        added = 0
        for value in values or ():
            if cap is not None and added >= cap:
                return
            plain = isinstance(value, str)
            text = (value if plain else str(getattr(value, "value", value))).strip()
            if not text or (wanted and wanted not in text.lower()):
                continue
            if text not in found:
                found.append(text)
                if not plain:
                    counted[text] = value
                added += 1

    # **Both readers, in order, not one or the other.** The Code tab is a
    # single box over two engines, so its switches are sourced from two places:
    # `/branch` and `/author` only git can answer, `/type` and `/repo` only the
    # index can. Taking `lookup` *instead of* the store left `/type` offering
    # nothing on the one tab where code file types matter most - the menu was
    # there, it opened, and it was empty.
    # **Only the filters this command says may narrow it.** `Command.scoped_by`
    # is the catalogue's answer, so the dropdown, the CLI and the model grammar
    # cannot disagree about it. Narrowing only ever removes values, so a wrong
    # entry there costs a suggestion rather than a wrong answer.
    scope = scope_for(command, context)
    repo = _scope_repo(scope)

    readers = []
    if lookup is not None:
        # git's values are per-checkout; `repo:leasha branch:` means leasha's
        # branches. The lookup already wanted this argument implicitly.
        readers.append(
            (lambda kind, prefix, limit: lookup(kind, prefix, limit, repo=repo))
            if repo and _takes_repo(lookup) else lookup)
    if store is not None:
        # **`within` is passed only when there is one.** Sending `within=None`
        # to a store that has never heard of it raises `TypeError`, which the
        # broad `except` below then swallows - so the menu would quietly lose
        # every indexed value rather than say anything. That is the silent
        # degradation this project has a standing rule against, and it showed
        # up immediately: four existing tests use a double with the old
        # signature, and all four went from offering `pdf` to offering `word`.
        readers.append(lambda kind, prefix, limit: _counted_values(
            store, kind, prefix, limit, scope))

    # 1. What is actually indexed, commonest first. Still first, because the
    #    extension somebody wants is nearly always one of the three they have
    #    thousands of.
    for reader in readers if command.source else ():
        try:
            offer(reader(command.source, wanted, limit))
        except Exception as exc:                # noqa: BLE001 - see the docstring
            _log.debug("no {} suggestions: {}", command.source, exc)
        if len(found) >= limit:
            break

    # **What the *index* said under the scope**, which is not the same as what
    # the menu ends up holding: the grammar's own values and the format
    # catalogue below are facts about the language, and no scope narrows them.
    # `1e` is about the scoped lookup coming back empty, so this is the number
    # it has to watch - the first version gated on the merged list, and a
    # `/type` under a repository with nothing in it still showed `excel` and
    # `word`, so the fallback could not fire on the case it was written for.
    from_index = len(found)

    # 2. The grammar's own values. **After the index, not before**, which is
    #    the one ordering change here: `type:excel` is a real filter and it
    #    should not outrank `pdf` when there are four thousand PDFs. Commands
    #    with no `source` are unaffected - `/has` and `/size` reach this first
    #    and behave exactly as they did.
    offer(command.values)

    # 3. Configured but not yet indexed.
    #
    #    **The gap this whole function existed to close, reopened at the other
    #    end.** `distinct_values` reads `files.ext`, so it can only ever offer a
    #    type somebody has already indexed - and a format switched on in the
    #    file-types editor is invisible in `/type` until the next index run
    #    finds one. Switching a format on and finding no trace of it in the
    #    filter menu reads as the setting not having worked.
    #
    #    Deliberately last. These may return nothing, and the original argument
    #    against offering them stands: a value that matches nothing is how a
    #    working filter looks broken. Ordering answers it - what is in the index
    #    is what is at the top - without going back to pretending the type does
    #    not exist.
    #    **Gated on there being a store, which is not about the store.** The
    #    menu is built twice: once instantly on the interface thread with no
    #    store, and again on a worker once the index has answered. Reading two
    #    TOML files is I/O, and the interface thread does not do I/O - so the
    #    catalogue rides with the pass that is already on a worker. An explicit
    #    `catalogue` overrides the gate, which is what the tests pass.
    #
    #    **And bounded separately from the index, because 34 became 405.**
    #    `3b29b7f` took the readable text types from thirty-four to four
    #    hundred and five. Sharing one ceiling with the indexed half then
    #    inverts the ordering this was built around: a corpus holding forty
    #    types would have a tail of three hundred and sixty-five sitting behind
    #    it, alphabetically, and the menu fills with types the machine does not
    #    have. See `catalogue_limit`.
    tail = catalogue_limit(wanted)
    if tail and command.source == "ext" and (catalogue is not None or store is not None):
        try:
            offer(catalogue() if catalogue is not None else enabled_extensions(),
                  cap=tail)
        except Exception as exc:                # broad by design - see the docstring
            _log.debug("no format catalogue: {}", exc)

    # **1e: an empty menu is indistinguishable from a broken one.** A scope
    # that removes everything is the one case where narrowing has made things
    # worse, so it is undone and said: the unscoped values, with a note the
    # widget renders as a dimmed "(all)". Silence here would be the failure
    # this whole widget exists to prevent, arrived at from the other end.
    if not from_index and scope is not None and command.source:
        _log.debug("no {} values under the typed filters; offering all",
                   command.source or command.name)
        unscoped = value_suggestions(
            store, name, prefix, limit, resolve, lookup, catalogue,
            context=None, notes=None)
        if unscoped:
            _note_all(notes)
        return unscoped

    # **3b, and it is a label rather than a feature.** Typing a date by hand
    # has always worked. Nothing said so, so the menu read as the only way in -
    # and a person who wanted `after:2019-04-01` had no sign the box would take
    # it. Last, because it is the way out rather than an answer.
    if getattr(command, "is_date", False) and not wanted and len(found) < limit:
        found.append(CUSTOM_ROW)

    if counts is not None:
        counts.update(counted)
    return found[:limit]


def _counted_values(store: Any, kind: str, prefix: str, limit: int,
                    scope: Any) -> list[Any]:
    """`ValueCount`s where the store offers them, strings where it does not.

    A store that predates `distinct_value_counts` - a test double, an older
    object - still answers `distinct_values`, and losing its values because it
    has not grown a method is the silent degradation `within=` already had to
    be taught to avoid.
    """
    extra = {"within": scope} if scope is not None else {}
    counts_of = getattr(store, "distinct_value_counts", None)
    if callable(counts_of):
        return counts_of(kind, prefix=prefix, limit=limit, **extra)
    return store.distinct_values(kind, prefix=prefix, limit=limit, **extra)


#: What the widget shows when a scope was dropped. One sentence, in the plain
#: register the rest of the menu uses.
ALL_VALUES_NOTE = "(all)"


def _note_all(notes: Optional[list[str]]) -> None:
    """Record that the scope was dropped. **Never raises**; it is a label."""
    if notes is None:
        return
    try:
        if ALL_VALUES_NOTE not in notes:
            notes.append(ALL_VALUES_NOTE)
    except Exception:                            # noqa: BLE001 - see docstring
        return


def scope_for(command: Any, context: Any) -> Any:
    r"""The part of `context` this command is allowed to be narrowed by.

    Returns None when there is nothing to narrow by - which is the same thing
    `distinct_values` is handed for an unfiltered query, so the unscoped shape
    stays exactly as it was.

    **Built by blanking the fields, not by re-parsing.** A second parser is the
    failure this order's opening paragraph names, and `ParsedQuery` is frozen,
    so `replace` on the fields `scoped_by` does *not* mention is the honest way
    to say "only these".
    """
    if context is None or not getattr(command, "scoped_by", ()):
        return None

    allowed = {name for spelling in command.scoped_by
               for name in _SCOPE_FIELDS.get(spelling, ())}
    if not allowed:
        return None

    try:
        blanks = {field: neutral for field, neutral in _FILTER_FIELDS.items()
                  if field not in allowed}
        narrowed = replace(context, **blanks)
    except Exception as exc:                     # noqa: BLE001 - a menu, not a search
        _log.debug("could not narrow the value scope: {}", exc)
        return None

    return narrowed if getattr(narrowed, "has_filters", False) else None


#: Which `ParsedQuery` fields each `scoped_by` name owns. The catalogue speaks
#: in command names; the parser speaks in field names, and this is the one
#: place the two are put side by side.
_SCOPE_FIELDS: dict[str, tuple[str, ...]] = {
    "repo": ("repos",),
    "type": ("ext",),
    "after": ("after",),
    "before": ("before",),
    "path": ("paths",),
    "from": ("senders",),
    "to": ("recipients",),
}

#: Every filter field, with the value that means "no restriction".
#:
#: **This must mirror `ParsedQuery.has_filters`**, and a test asserts it,
#: because the two are answering the same question from opposite ends: that
#: property lists what counts as a filter, and this lists how to remove one.
#:
#: `scope` is the one that is not empty when it is neutral - it is `"all"`, and
#: the first version of this blanked it to `""`, which `has_filters` then read
#: as a filter. Narrowing `/type` by a `path:` that `scoped_by` does not permit
#: produced a query with no filters in it that nonetheless claimed to have one.
_FILTER_FIELDS: dict[str, Any] = {
    "ext": (),
    "after": None,
    "before": None,
    "paths": (),
    "repos": (),
    "senders": (),
    "recipients": (),
    "subjects": (),
    "names": (),
    "sizes": (),
    "has_attachment": None,
    "scope": "all",
}


#: A value row: the value, then whatever is worth knowing about it.
#:
#: The same shape the command rows use (`_ROW` in `command_popup`), because the
#: two lists sit in the same popup and a second alignment would read as a bug.
VALUE_ROW = "{value:<30} {meta}"

#: What a count counts, per value source. `dave@acme.com  316 files` would be
#: wrong in a way somebody would notice and not be able to explain.
VALUE_NOUNS: dict[str, str] = {
    "sender": "messages",
    "repo": "files",
    "ext": "files",
    "folder": "files",
    # **Adoptions §3, and the noun is the whole honesty of the row.** The
    # number beside a saved search is how many times it has been run, not how
    # many files it would find - counting the second would mean running every
    # saved search behind a keystroke. `invoices   12 runs` says what it is;
    # `invoices   12 files` would be a number somebody would believe and act
    # on, and it would be wrong.
    "saved": "runs",
}


#: The row that leaves the value tier and hands the box back to the person.
#:
#: `3b`: "which is what happens today, made explicit". Typing a date by hand
#: has always worked; nothing said so, so the menu looked like the only way in.
CUSTOM_ROW = "custom…"


def kind_expansion(value: str) -> tuple[str, ...]:
    r"""The extensions a `/type` kind word stands for, or `()`.

    `3a`'s second page. Read from `_EXT_GROUPS` - the parser's own table, which
    is what `type:excel` already expands to - so the page cannot offer a
    spelling the filter would then not match. A second copy of this mapping is
    exactly the "nested sub-key dictionary" this order rules out.
    """
    from app.search.query import _EXT_GROUPS

    return tuple(_EXT_GROUPS.get(str(value or "").strip().lower(), ()))


def value_page(name: str, chosen: str = "", *, resolve: Any = None) -> str:
    """The breadcrumb above a second value page, or `""` on the first.

    Without it the second page is a list of extensions with nothing saying
    which kind they belong to or how to get back - and this order's whole
    subject is a menu that does not say what it is answering.
    """
    if not chosen:
        return ""
    if not kind_expansion(chosen):
        return ""
    return f"/{name} {chosen} — Backspace to go back"


def value_row(value: str, *, count: Optional[int] = None, exact: bool = True,
              noun: str = "files", hint: str = "") -> str:
    """One row of the value menu.

    **The count is shown only when it is exact.** A scoped menu counts the
    first `VALUE_SAMPLE` matching rows, and at that ceiling the number is a
    fraction of the truth - `pdf   200 files` for a corpus holding five
    thousand. Omitting it loses information; printing it states something
    false, and this project's rule is that a label says what is so.

    `hint` wins when both are available, because a date's resolved range says
    more than a count of the files that would match it.
    """
    text = str(value or "")
    meta = str(hint or "")
    if not meta and count is not None and exact:
        number = int(count)
        # "1 files" is the kind of thing that makes a careful interface look
        # careless, and this row sits under somebody's cursor.
        word = noun[:-1] if number == 1 and noun.endswith("s") else noun
        meta = f"{number:,} {word}"
    if not meta:
        return text
    return VALUE_ROW.format(value=text, meta=meta).rstrip()


def value_rows(name: str, values: Sequence[Any], *, resolve: Any = None,
               today: Any = None) -> list[str]:
    r"""The value menu's rows, from either strings or `ValueCount`s.

    Both, because the menu is filled twice: once instantly from the grammar
    with no store, and again from the index on a worker. The first pass has no
    counts to show and must not wait for any.

    Qt-free, so the wording is testable without a display.
    """
    if resolve is None:
        from app.search.commands import command_for as resolve

    command = resolve(name)
    is_date = bool(getattr(command, "is_date", False))
    noun = VALUE_NOUNS.get(getattr(command, "source", "") or "", "files")

    rows = []
    for entry in values or ():
        # **`isinstance` before `getattr`.** A plain string has a `.count` -
        # the method - so `getattr(entry, "count", None)` returns a callable
        # for every value on the instant pass, and `f"{int(count):,}"` then
        # raises. The two shapes are told apart by what they are, not by which
        # attributes they happen to answer to.
        plain = isinstance(entry, str)
        value = entry if plain else str(getattr(entry, "value", entry))
        rows.append(value_row(
            value,
            count=None if plain else getattr(entry, "count", None),
            exact=True if plain else bool(getattr(entry, "exact", True)),
            noun=noun,
            hint=(resolved_period(value, today=today)
                  if getattr(command, "name", "") == "date"
                  else resolved_date(value, today=today)) if is_date else "",
        ))
    return rows


def as_typed_value(value: str) -> str:
    r"""A value, spelled so the parser reads it back as one value.

    **Quoted when it contains a space**, because the tokenizer splits on
    whitespace: picking `last month` from the menu inserted `after:last month`,
    which parses as `after:last` - not a date, so no filter at all - plus a
    loose search for the word *month*. Somebody chose a date from a list and
    got a query that filtered nothing and searched for the wrong thing, with
    nothing on screen to say so.

    Found while building the resolved-date hint (`2c`), which is the point of
    that hint: showing what a value resolves to is how a value that resolves to
    nothing becomes visible.
    """
    text = str(value or "").strip()
    if not text or ('"' in text):
        return text
    return f'"{text}"' if any(c.isspace() for c in text) else text


#: Files tab's volume picker (§3c) - the first entry, meaning "no filter".
ALL_LOCATIONS = "All locations"

#: Strips whatever `on:`/`volume:`/`drive:` term is already typed, so picking
#: a second volume replaces the first rather than ANDing two filters that can
#: never both be true of one file. Every alias `query._FIELD_ALIASES` accepts
#: for "volume" is covered, quoted or bare, negated or not - a person who
#: typed `drive:` by hand and then used the picker must not end up with two
#: terms fighting over the same filter.
_VOLUME_FILTER_TERM = re.compile(
    r'(?:(?<=\s)|^)[-!]?\b(?:on|volume|drive):(?:"[^"]*"|\S+)\s*',
    re.IGNORECASE,
)


def set_volume_filter(text: str, value: str) -> str:
    r"""Files tab's volume picker (§3c): swap whatever volume term is
    already typed for `value`, or drop it entirely when `value` is empty -
    the "All locations" choice. Qt-free, so a re-pick's exact wording is
    checked without a display, the same reason every other value-menu
    decision in this module lives here rather than in the widget.
    """
    cleaned = _VOLUME_FILTER_TERM.sub("", text or "").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if not value:
        return cleaned
    addition = f"on:{as_typed_value(value)}"
    return f"{cleaned} {addition}".strip() if cleaned else addition


def volume_picker_options(values: Sequence[Any]) -> list[tuple[str, str]]:
    r"""Files tab's volume picker (§3c): `(label, raw_value)` pairs, "All
    locations" always first. `values` is `store.distinct_value_counts("on")` -
    see that method's own docstring for why it is index-backed and bounded.

    The raw value travels apart from its label so a name containing an em
    dash or extra spacing is never parsed back out of `value_row`'s own
    display text - `set_volume_filter` quotes the raw value directly.
    """
    options: list[tuple[str, str]] = [(ALL_LOCATIONS, "")]
    for entry in values or ():
        value = str(getattr(entry, "value", entry) or "").strip()
        if not value:
            continue
        label = value_row(
            value, count=getattr(entry, "count", None),
            exact=bool(getattr(entry, "exact", True)), noun="files")
        options.append((label, value))
    return options


def resolved_date(value: str, *, today: Any = None) -> str:
    r"""What a date value actually means, for the hint beside it.

    `/after 30d   (since 28 Jul)`. Qt-free and here rather than in the widget,
    so the wording can be checked without a display - the same reason the rest
    of the menu's decisions live in this file.

    `""` when the value is not a date the parser accepts, which is the honest
    answer and is what tells somebody that `after:lst week` is not going to do
    what they meant.
    """
    from app.search.query import _parse_date

    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    try:
        found = _parse_date(text, today=today)
    except Exception:                            # noqa: BLE001 - a hint
        return ""
    if found is None:
        return ""
    # **No `%-d`.** That is a glibc extension: it strips the leading zero on
    # Linux and raises `ValueError` on Windows, which is the only platform this
    # ships to. The day is formatted by hand instead.
    return f"(since {found.day} {found:%b %Y})"


def resolved_period(value: str, *, today: Any = None) -> str:
    r"""What a `/date` value covers, for the hint beside it. `""` if unreadable.

    **Not `resolved_date`**, whose "(since 28 Jul)" is right for `/after` and
    wrong here: `date:today` is today, not everything since this morning.
    Read through the parser's own `_parse_span`, so the hint cannot describe a
    period the filter would not then apply.
    """
    from app.search.query import _parse_span

    try:
        found = _parse_span(str(value or ""), today=today)
    except Exception:                            # noqa: BLE001 - a hint
        return ""
    if found is None:
        return ""
    first, last, _raw_first, _raw_last = found

    def day(moment: Any) -> str:
        # By hand, for the reason `resolved_date` gives: no `%-d` on Windows.
        return f"{moment.day} {moment:%b %Y}"

    if first is not None and last is None:
        return f"(since {day(first)})"
    if first is None and last is not None:
        return f"(up to {day(last)})"
    if first == last or (first.year, first.month, first.day) == (last.year, last.month, last.day):
        return f"({day(first)})"
    return f"({day(first)} to {day(last)})"


def scope_key(name: str, context: Any, resolve: Any = None) -> str:
    """A stable string for the scope `name`'s values would be fetched under.

    The TTL cache in the popup is keyed on this alongside the command name.
    Keyed on the name alone, a global answer fetched for `/from` would be
    served under `repo:leasha from:` for the next two minutes - and a wrong
    answer with a lifetime is worse than a slow one, because nothing about it
    looks wrong.

    Built from the *narrowed* scope rather than from the whole query, so typing
    more free text after a filter does not throw the cache away for no reason.
    """
    if resolve is None:
        from app.search.commands import command_for as resolve

    command = resolve(name)
    if command is None:
        return ""
    scope = scope_for(command, context)
    if scope is None:
        return ""
    return "|".join(
        f"{field}={getattr(scope, field, None)!r}"
        for field in sorted(_FILTER_FIELDS)
        if getattr(scope, field, None) != _FILTER_FIELDS[field]
    )


def _scope_repo(scope: Any) -> str:
    """The single repository a scope names, if it names exactly one."""
    names = tuple(getattr(scope, "repos", ()) or ())
    return str(names[0]) if len(names) == 1 else ""


def _takes_repo(lookup: Any) -> bool:
    """Does this reader accept the repository argument it implicitly wanted?

    Asked rather than assumed, because `lookup` is injected by three callers
    and one of them is a test double.
    """
    import inspect

    try:
        return "repo" in inspect.signature(lookup).parameters
    except (TypeError, ValueError):
        return False
