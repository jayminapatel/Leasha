"""The `/` dropdown: the filters, shown at the moment somebody could use one.

Layer: L5

Every filter in this list has worked since Layer 4. `type:pdf from:dave` parsed
correctly, was tested, and shipped - and in practice nobody used it, because
nothing in the application ever said it existed. A feature that is built, tested
and invisible delivers exactly nothing, and it is worse than an unbuilt one
because it also costs maintenance.

So the point of this widget is not convenience. It is the difference between a
search box that is a bag of words and one that can answer "the PDF Dave sent in
June".

**Triggered by `/`, and only by `/`.** Not by every keystroke, not by a help
button somebody has to find. `/` is the convention every chat tool has taught
people, it costs nothing to ignore, and it puts the list in front of you at the
exact moment you are deciding what to type. A path or a date containing a slash
is untouched: an unrecognised `/word` is left exactly as typed, because silently
altering a query is how a search box loses trust.

**Two menus, not one.** Choosing `/from` used to insert `from:` and stop, which
answered "which filters exist" and left the harder question - *what do I put
here* - to guesswork. A guessed value returns nothing, and a filter that returns
nothing is indistinguishable from a filter that does not work. So the moment a
command is chosen the list stays open and offers **values read from the index**:
the extensions actually present, the people who actually sent mail, the
repositories actually found.

*Off the interface thread.* Those come from `store.distinct_values`, which is
index-backed and bounded, and it is still run on a worker and cached - the first
non-negotiable is that no unbounded work sits behind a keystroke, and a store
read between two letters is the freeze this application has a standing rule
against. The static values appear instantly; the index's own arrive when they
arrive.

Thin, like every widget here. The catalogue lives in `app/search/commands.py`
beside the parser that has to agree with it, what to offer for a value is
decided in `presenter.value_suggestions` where it can be tested without a
display, and this arranges a `QCompleter` over the result.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence

from PyQt6.QtCore import QEvent, QObject, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import QCompleter, QLineEdit

from app.search.commands import COMMANDS, matching
from app.ui.presenter import (
    CODE_COMMANDS, CUSTOM_ROW, FILES_COMMANDS, MAIL_COMMANDS, as_typed_value,
    kind_expansion, scope_key, slash_context, value_page, value_rows,
    value_suggestions,
)
from app.ui.widgets.command_icon import icon_for, text_colour

__all__ = [
    "CommandPopup", "attach_to", "CODE_COMMANDS", "FILES_COMMANDS",
    "MAIL_COMMANDS", "VALUE_ICON", "OFFER_ICON", "SUGGEST_TTL_S",
]

#: Shown per row: what to type, **what it expects**, and what it does.
#:
#: Asked for: *"in the dropdown / command should indicate what it expects as an
#: argument"*. It used to read `/type <type>`, which says a value goes there and
#: nothing about which - and a value guessed wrong returns nothing, which is
#: indistinguishable from a filter that does not work. `/type <pdf, docx, …>`
#: answers the question the menu is open to answer.
_ROW = "{example:<26} {hint:<34} {summary}"

#: How much of a value hint fits before the description is pushed off the row.
#: The hints are written longest-useful-part-first for this reason - "pdf, docx,
#: xlsx, email, code - or several: pdf,docx" still reads correctly cut at the
#: comma.
HINT_CHARS = 32

#: The glyph beside a *value*. One for all of them: the row is a value of the
#: command already chosen, and repeating that command's icon down the list says
#: nothing the heading above has not.
VALUE_ICON = "▹"

#: The glyph beside something the box is offering back - a search this person
#: ran before, or one they saved. Different from the value glyph on purpose:
#: these are *theirs*, and the list is not answering a question they asked.
OFFER_ICON = "↺"

#: How long a fetched value list is trusted. Long enough that arrowing through
#: a menu costs one query rather than one per keystroke; short enough that an
#: index run finishing while the window is open is reflected without a restart.
SUGGEST_TTL_S = 120.0

# `CODE_COMMANDS` and friends are re-exported from `presenter.py`, which is
# where they can be tested: the rule they encode - search offers the union, each
# tab a subset - is about the grammar, not about Qt, and asserting it should not
# need a display.


def _hint(command: Any) -> str:
    """What this switch expects, short enough to sit in a row.

    A switch that takes no value says so rather than showing an empty column -
    "no value needed" is the answer to "what do I put here" for `/history`, and
    a blank is not.
    """
    text = str(getattr(command, "value_hint", "") or "").strip()
    if not text:
        return f"<{command.name}>"
    if text.startswith("no value"):
        return "(no value)"
    if len(text) > HINT_CHARS:
        # Cut at a comma when there is one near the end, so the hint reads as a
        # shortened list rather than a word chopped in half.
        cut = text.rfind(",", 0, HINT_CHARS)
        text = text[:cut] + ", …" if cut > 12 else text[:HINT_CHARS - 1] + "…"
    return f"<{text}>"


class CommandPopup(QCompleter):
    """Completes `/type`, `/from`, `/after`… and then their values.

    Inserts `name:` rather than `/name`, so what lands in the box is the syntax
    the parser reads and the user can edit by hand afterwards. The slash is the
    doorway, not the grammar.
    """

    chosen = pyqtSignal(str)

    def __init__(self, parent: Optional[Any] = None,
                 only: Optional[Sequence[str]] = None,
                 catalogue: Optional[Sequence[Any]] = None,
                 matcher: Any = None) -> None:
        super().__init__(parent)
        #: Which set of switches this box offers. The Code tab's repository
        #: search has its own - a different engine over a different store - and
        #: mixing the two would offer `/history` in a box that cannot answer it.
        self._catalogue = tuple(catalogue) if catalogue is not None else COMMANDS
        self._matcher = matcher or matching
        #: Command names this box actually honours, or None for all of them.
        #:
        #: **A list is not an affordance if half of it does nothing.** Every
        #: input in this application opens a menu on `/`, and it must, or the
        #: one that does not looks broken. But a repository list cannot answer
        #: `/from` or `/subject`, and offering them there would be a dropdown
        #: full of commands that quietly fail - which is worse than no dropdown
        #: at all, because it is discovered one disappointment at a time.
        self._only = tuple(only) if only else None
        self._matches: list[Any] = []
        #: The command whose *values* are on show, or "" in command mode. The
        #: view needs it to know what a chosen row means.
        self.value_of = ""
        self._values: list[str] = []
        self._rows: list[str] = []
        #: The kind word whose extensions are showing, or "" on the first page.
        self._second_page: str = ""
        #: Heading rows currently on show. Non-empty only in first-contact
        #: mode, which is what tells the three modes apart.
        self._headings: set = set()

        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # Unfiltered: this widget decides what to show, because the match is
        # against aliases too - `/f` must offer `from`, and QCompleter's own
        # prefix matching only ever sees the display string.
        self.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.setFilterMode(Qt.MatchFlag.MatchContains)
        # **Qt shows seven rows by default, and there are more commands than
        # that.** The four below the fold were reachable by scrolling and
        # invisible in every other sense - the menu exists for discovery, so a
        # list that hides a third of itself on the keystroke meant to reveal it
        # defeats the whole feature. Sized to what this box offers, so adding a
        # command to the catalogue never quietly hides another one.
        self.setMaxVisibleItems(max(len(self._catalogue), 1))
        self.show_all()

    # -- command mode --------------------------------------------------------

    def show_all(self) -> None:
        self.set_prefix("")

    def set_prefix(self, prefix: str) -> None:
        """Narrow the list to commands matching what has been typed after `/`.

        **The row count is restored here, and that is a fix.** `set_values`
        lowers `maxVisibleItems` to fit a value list, and nothing put it back -
        so after using `/type` once, the *command* menu came back one row tall
        for the rest of the session. Reported as *"the / comands dont work
        after first use on code windows"*, and from the outside that is exactly
        what a one-line dropdown looks like.
        """
        self.value_of = ""
        self._headings = set()
        self.setMaxVisibleItems(max(len(self._catalogue), 1))
        self._matches = [
            command for command in self._matcher(prefix)
            if self._only is None or command.name in self._only
        ]
        self._fill([
            (command.icon,
             _ROW.format(example=f"/{command.name}",
                         hint=_hint(command),
                         summary=command.summary))
            for command in self._matches
        ])

    # -- first-contact mode (search-experience §2e) ---------------------------

    def show_offers(self, sections: Any) -> bool:
        r"""What an empty, focused box offers: recent searches, then saved.

        `sections` is `((heading, ((label, insert), …)), …)` from
        `first_contact.sections`. Returns True if there was anything to show.

        **A third mode on the same popup, not a second popup.** One
        `QLineEdit` can only sensibly own one dropdown; two would race to
        appear on the same keystroke and the loser would flicker. The three
        modes are told apart the way the first two already are - by which
        list is filled - and `value_of` stays the marker for value mode.
        """
        self.value_of = ""
        self._second_page = ""
        self._matches = []
        self._values = []
        self._headings = set()
        rows: list = []
        for heading, entries in sections or ():
            if not entries:
                continue
            # A heading is a row Qt can draw and `value_for_row` maps back to
            # itself, so choosing one completes nothing - the same trick the
            # second page's breadcrumb uses.
            self._headings.add(str(heading))
            self._values.append(str(heading))
            rows.append(("—", str(heading)))
            for label, insert in entries:
                self._values.append(str(insert))
                rows.append((OFFER_ICON, str(label)))
        self._rows = [text for _glyph, text in rows]
        if not rows:
            return False
        self.setMaxVisibleItems(max(1, min(len(rows), 12)))
        self._fill(rows)
        return True

    @property
    def offering(self) -> bool:
        """Is the box showing what it has to offer rather than a menu?"""
        return bool(self._headings)

    def is_heading(self, row_text: str) -> bool:
        """Headings are read, never chosen."""
        return str(row_text) in self._headings

    # -- value mode ----------------------------------------------------------

    def set_values(self, name: str, values: Sequence[str]) -> None:
        """Offer these as values of `name`.

        Called twice per command in the normal case - once with what the
        grammar knows, again when the index answers. Redrawing a list that is
        already open is deliberate: a menu that waits for a query before showing
        anything is a menu that feels broken on a cold cache.
        """
        self.value_of = str(name or "")
        self._second_page = ""
        self._headings = set()
        self._matches = []
        # **The bare value is what gets inserted; the row is what is read.**
        # A `ValueCount` carries a number the row shows and the query must
        # never contain, so the two are kept apart here rather than stripped
        # apart again in `on_activated`.
        self._values = [
            value if isinstance(value, str) else str(getattr(value, "value", value))
            for value in values
        ]
        self._rows = value_rows(self.value_of, list(values or ()))
        # A value list can be forty long where the command list is eleven.
        # Capped rather than sized to it: a menu taller than the window is not
        # more discoverable, and scrolling is the right answer past a dozen.
        self.setMaxVisibleItems(max(1, min(len(self._values), 12)))
        self._fill([(VALUE_ICON, row) for row in self._rows])

    def _fill(self, rows: Sequence[tuple[str, str]]) -> None:
        ink = text_colour(self.popup())
        self._model.clear()
        for glyph, text in rows:
            item = QStandardItem(icon_for(glyph, ink), text)
            item.setEditable(False)
            self._model.appendRow(item)

    # -- what a row means ----------------------------------------------------

    def command_at(self, row: int):
        """The `Command` behind a row, or None if the row is stale."""
        if 0 <= row < len(self._matches):
            return self._matches[row]
        return None

    @property
    def on_second_page(self) -> bool:
        """Is the menu showing a kind's extensions rather than the kinds?"""
        return bool(self._second_page)

    def open_second_page(self, name: str, chosen: str,
                         values: Sequence[str]) -> None:
        """`3a`: the extensions a kind word stands for, with a breadcrumb.

        Bounded to the one case the order allows. Full recursive nesting is out
        of scope: no filter here has a grammar deep enough for it, and a
        generic tree invites this popup to become a query builder.
        """
        self._second_page = str(chosen)
        self.value_of = str(name or "")
        self._matches = []
        self._values = [str(value) for value in values]
        self._rows = value_rows(self.value_of, list(values))
        crumb = value_page(self.value_of, chosen)
        self.setMaxVisibleItems(max(1, min(len(self._values) + 1, 12)))
        rows = [(VALUE_ICON, row) for row in self._rows]
        if crumb:
            # First, so it reads as a heading rather than an afterthought. It
            # is not selectable as a value - `value_for_row` maps it back to
            # itself and `on_activated` finds no expansion, so choosing it
            # completes nothing.
            rows.insert(0, ("←", crumb))
        self._fill(rows)

    def leave_second_page(self) -> bool:
        """Backspace out of the extensions, back to the kinds. True if it did."""
        if not self._second_page:
            return False
        self._second_page = ""
        return True

    def value_for_row(self, row_text: str) -> str:
        """The value a row stands for, without the metadata beside it.

        Matched by position rather than by splitting the text: a value with two
        spaces in it - `last month`, a folder called `Site Photos 2024` - would
        not survive a `split()`, and this is the string that goes into the
        query.
        """
        try:
            return self._values[self._rows.index(str(row_text))]
        except (ValueError, IndexError):
            return str(row_text).strip()

    @property
    def has_matches(self) -> bool:
        return self._model.rowCount() > 0


class _TabAccepts(QObject):
    """Makes Tab and Shift+Tab behave inside a completion popup.

    Owned by the completer so it lives exactly as long as the popup does. An
    event filter that is garbage collected stops filtering silently, and the
    bug would come back looking intermittent.
    """

    def __init__(self, completer: QCompleter) -> None:
        super().__init__(completer)
        self._completer = completer

    def eventFilter(self, watched: Any, event: Any) -> bool:   # noqa: N802 - Qt's naming
        if event.type() != QEvent.Type.KeyPress:
            return False
        key = event.key()

        # **3a: Backspace comes back from a kind's extensions.** Only while
        # the second page is showing, and only when the box ends at the kind
        # word - otherwise Backspace is what it always is, a character being
        # deleted, and stealing it would be far worse than not offering it.
        if key == Qt.Key.Key_Backspace:
            completer = self._completer
            if getattr(completer, "on_second_page", False):
                completer.leave_second_page()
                return False                     # the character still deletes

        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            popup = self._completer.popup()
            index = popup.currentIndex()
            if not index.isValid():
                # Nothing highlighted yet - Tab picks the first row, which is
                # what a completion list is for. Refusing here would make the
                # key do nothing at all on the most common path.
                index = self._completer.completionModel().index(0, 0)
                if not index.isValid():
                    return False
            # Through the completer's own signal, so the insertion logic lives
            # in exactly one place and a mouse click and a Tab cannot drift.
            self._completer.activated[str].emit(index.data())
            popup.hide()
            return True

        return False


class _OffersOnFocus(QObject):
    """Shows what the box has to offer when it is empty and focused. §2e.

    An event filter on the line edit rather than a `focusInEvent` override,
    because the search box is a plain `QLineEdit` on purpose - everything
    else about it (placeholder, clear button, Enter) stays exactly as it was,
    and removing this feature is deleting one line.
    """

    def __init__(self, line_edit: QLineEdit, show: Any) -> None:
        super().__init__(line_edit)
        self._line_edit = line_edit
        self._show = show

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt's naming
        if (event.type() == QEvent.Type.FocusIn
                and not self._line_edit.text().strip()):
            self._show()
        return False


def attach_to(line_edit: QLineEdit,
              only: Optional[Sequence[str]] = None,
              store: Any = None,
              catalogue: Optional[Sequence[Any]] = None,
              matcher: Any = None,
              resolve: Any = None,
              lookup: Any = None,
              offers: Any = None) -> CommandPopup:
    """Wire a `CommandPopup` to a search box. Returns it, for tests and teardown.

    Kept as a function rather than a subclass of `QLineEdit` so the search view
    keeps a plain line edit - everything else about it (placeholder, clear
    button, Enter handling) stays exactly as it was, and removing this feature
    would be deleting one line.

    `store` is optional and only makes the value menu better: without it the
    grammar's own values are still offered, which is all `/has`, `/after` and
    `/size` ever had. A box with no store is not a box with a broken menu.
    """
    popup = CommandPopup(line_edit, only=only, catalogue=catalogue,
                         matcher=matcher)
    popup.setWidget(line_edit)

    def suggest(name: str, partial: str, use_store: bool = True,
                context: Any = None, notes: Any = None) -> list:
        """The values, carrying their counts where the store supplied any.

        Strings for anything the grammar or the catalogue contributed - those
        are facts about the language and have nothing to count.
        """
        counts: dict = {}
        found = value_suggestions(store if use_store else None, name, partial,
                                  resolve=resolve,
                                  lookup=lookup if use_store else None,
                                  context=context, notes=notes, counts=counts)
        return [counts.get(value, value) for value in found]

    # **Tab has to pick the highlighted command.**
    #
    # QCompleter's popup handles Enter and Return and nothing else, so Tab fell
    # through to the line edit and moved focus to the next control - the list
    # vanished and the person was somewhere else entirely. Reported as "you use
    # a keyboard and press tab, it does not select; it needs to be clicked by
    # mouse", which in a project whose spec requires keyboard-only operation end
    # to end is a plain failure rather than a rough edge.
    #
    # Tab is the completion key everywhere a completion list exists - shells,
    # editors, every IDE. Enter still works; this adds the one people reach for
    # first.
    popup.popup().installEventFilter(_TabAccepts(popup))

    #: (name, scope) -> (fetched_at, values). One query per command per scope
    #: per two minutes, rather than one per keystroke.
    #:
    #: **The scope is in the key, and that is not tidiness.** Keyed on the name
    #: alone, the global answer fetched for `/from` would be served under
    #: `repo:leasha from:` for the next two minutes - a wrong answer with a
    #: hundred-and-twenty-second lifetime, which is worse than a slow one
    #: because nothing about it looks wrong.
    cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

    def offer_values(name: str, partial: str, context: Any = None) -> None:
        """Show what the grammar knows now, and what the index knows shortly."""
        popup.set_values(name, suggest(name, partial, use_store=False))

        key = (name, scope_key(name, context, resolve))
        cached = cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < SUGGEST_TTL_S:
            _deliver(name, partial, cached[1])
            return
        if store is None and lookup is None:
            _show_or_hide()
            return

        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(
            suggest, name, "", True, context, component="ui.commands")
        worker.signals.finished.connect(
            lambda found, k=key, n=name, p=partial: _fetched(k, n, p, found))
        # A menu is a convenience. A store that is mid-index or closed costs
        # the suggestions and nothing else - the static values are already up.
        worker.signals.failed.connect(lambda _error: None)
        run(QThreadPool.globalInstance(), worker)
        _show_or_hide()

    def _fetched(key: tuple[str, str], name: str, partial: str,
                 found: Any) -> None:
        cache[key] = (time.monotonic(), list(found or []))
        _deliver(name, partial, cache[key][1])

    def _deliver(name: str, partial: str, values: Sequence[str]) -> None:
        # **Only if the box is still asking the same question.** A slow answer
        # arriving after somebody has typed on is the stale-result problem every
        # other worker in this application guards against, and here it would
        # replace the menu under their fingers.
        _head, mode, now, _context = slash_context(line_edit.text(), resolve)
        if mode != "value" or popup.value_of != name:
            return
        wanted = now.strip().lower()
        popup.set_values(name, [
            value for value in values if not wanted or wanted in value.lower()
        ] or suggest(name, now, use_store=False))
        _show_or_hide()

    def _show_or_hide() -> None:
        if popup.has_matches:
            popup.complete()
        else:
            popup.popup().hide()

    def on_text(text: str) -> None:
        _head, mode, partial, context = slash_context(text, resolve)
        if mode == "command":
            popup.set_prefix(partial)
            _show_or_hide()
        elif mode == "value":
            name = text.rpartition(" ")[2].partition(":")[0]
            offer_values(name, partial, context)
        else:
            popup.popup().hide()

    def on_activated(row_text: str) -> None:
        """Insert what was chosen, and decide whether the menu stays open."""
        text = line_edit.text()
        word = text.rpartition(" ")[2]
        head = text[: len(text) - len(word)]

        # **§2e, and it is checked first.** A row here is a whole query, not
        # a fragment of one: it replaces the box rather than completing the
        # word under the cursor, which is the opposite of what the other two
        # modes do.
        if popup.offering:
            if popup.is_heading(row_text):
                return                           # a heading is read, not chosen
            line_edit.setText(popup.value_for_row(row_text))
            line_edit.setCursorPosition(len(line_edit.text()))
            popup.popup().hide()
            # As if they had pressed Enter, because they chose a finished
            # question. `setText` does not emit `textEdited`, so nothing else
            # would have run it.
            line_edit.returnPressed.emit()
            return

        if popup.value_of:
            chosen_value = popup.value_for_row(row_text)

            # **3b: `custom…` hands the box back.** Typing a date by hand has
            # always worked; nothing said so, which made the menu look like the
            # only way in. Picking this leaves `after:` in the box with the
            # accepted forms in the hint and gets out of the way.
            if chosen_value == CUSTOM_ROW:
                line_edit.setText(f"{head}{popup.value_of}:")
                line_edit.setCursorPosition(len(line_edit.text()))
                popup.popup().hide()
                return

            # **3a: a kind word opens its extensions rather than closing.**
            # `/type excel` is a real filter and picking it must still work -
            # so the *second page* is offered and the box is left showing the
            # kind, which means Enter on nothing still finishes the job.
            expansion = kind_expansion(chosen_value)
            if expansion and not popup.on_second_page:
                line_edit.setText(f"{head}{popup.value_of}:{chosen_value}")
                line_edit.setCursorPosition(len(line_edit.text()))
                popup.open_second_page(popup.value_of, chosen_value, expansion)
                _show_or_hide()
                return

            # A value: complete the whole `name:value` and get out of the way.
            # The trailing space is what says "this filter is finished" - and
            # it is also what stops `_tail` reopening the menu immediately.
            # `row_text` is the row, which may carry a count or a resolved
            # date; the value is the first column. And it is quoted when it
            # holds a space - picking `last month` used to insert
            # `after:last month`, which parses as `after:last` plus a loose
            # search for the word *month*.
            chosen = as_typed_value(popup.value_for_row(row_text))
            line_edit.setText(f"{head}{popup.value_of}:{chosen} ")
            line_edit.setCursorPosition(len(line_edit.text()))
            popup.popup().hide()
            return

        name = row_text.split()[0].lstrip("/")
        command = next((c for c in popup._catalogue if c.name == name), None)
        if command is None:
            return
        line_edit.setText(f"{head}{command.name}:")
        line_edit.setCursorPosition(len(line_edit.text()))
        # **Straight into the value menu.** Choosing a filter and being left
        # with a colon and a blank is the half of this feature that was
        # missing: the question "what can I put here" is the harder one.
        offer_values(command.name, "")

    def show_offers() -> None:
        """§2e: an empty, focused box offers this person their own searches.

        **Never raises**, and silent when there is nothing: a box that pops an
        empty list open on every click would be worse than one that offers
        nothing at all.
        """
        try:
            if offers is None or line_edit.text().strip():
                return
            if popup.show_offers(offers()):
                popup.complete()
        except Exception:                        # noqa: BLE001 - see docstring
            return

    if offers is not None:
        line_edit.installEventFilter(_OffersOnFocus(line_edit, show_offers))

    line_edit.textEdited.connect(on_text)
    popup.activated[str].connect(on_activated)
    return popup
