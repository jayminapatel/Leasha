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
    CODE_COMMANDS, FILES_COMMANDS, MAIL_COMMANDS, slash_context,
    value_suggestions,
)
from app.ui.widgets.command_icon import icon_for, text_colour

__all__ = [
    "CommandPopup", "attach_to", "CODE_COMMANDS", "FILES_COMMANDS",
    "MAIL_COMMANDS", "VALUE_ICON", "SUGGEST_TTL_S",
]

#: Shown per row: what to type, then what it does. Wide enough that the example
#: and the description both fit without the popup becoming a wall of text.
_ROW = "{example:<22} {summary}"

#: The glyph beside a *value*. One for all of them: the row is a value of the
#: command already chosen, and repeating that command's icon down the list says
#: nothing the heading above has not.
VALUE_ICON = "▹"

#: How long a fetched value list is trusted. Long enough that arrowing through
#: a menu costs one query rather than one per keystroke; short enough that an
#: index run finishing while the window is open is reflected without a restart.
SUGGEST_TTL_S = 120.0

# `CODE_COMMANDS` and friends are re-exported from `presenter.py`, which is
# where they can be tested: the rule they encode - search offers the union, each
# tab a subset - is about the grammar, not about Qt, and asserting it should not
# need a display.


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
        """Narrow the list to commands matching what has been typed after `/`."""
        self.value_of = ""
        self._matches = [
            command for command in self._matcher(prefix)
            if self._only is None or command.name in self._only
        ]
        self._fill([
            (command.icon,
             _ROW.format(example=f"/{command.name} <{command.name}>",
                         summary=command.summary))
            for command in self._matches
        ])

    # -- value mode ----------------------------------------------------------

    def set_values(self, name: str, values: Sequence[str]) -> None:
        """Offer these as values of `name`.

        Called twice per command in the normal case - once with what the
        grammar knows, again when the index answers. Redrawing a list that is
        already open is deliberate: a menu that waits for a query before showing
        anything is a menu that feels broken on a cold cache.
        """
        self.value_of = str(name or "")
        self._matches = []
        self._values = [str(value) for value in values]
        # A value list can be forty long where the command list is eleven.
        # Capped rather than sized to it: a menu taller than the window is not
        # more discoverable, and scrolling is the right answer past a dozen.
        self.setMaxVisibleItems(max(1, min(len(self._values), 12)))
        self._fill([(VALUE_ICON, value) for value in self._values])

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


def attach_to(line_edit: QLineEdit,
              only: Optional[Sequence[str]] = None,
              store: Any = None,
              catalogue: Optional[Sequence[Any]] = None,
              matcher: Any = None,
              resolve: Any = None,
              lookup: Any = None) -> CommandPopup:
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

    def suggest(name: str, partial: str, use_store: bool = True) -> list:
        return value_suggestions(store if use_store else None, name, partial,
                                 resolve=resolve, lookup=lookup if use_store else None)

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

    #: name -> (fetched_at, values). One query per command per two minutes,
    #: rather than one per keystroke.
    cache: dict[str, tuple[float, list[str]]] = {}

    def offer_values(name: str, partial: str) -> None:
        """Show what the grammar knows now, and what the index knows shortly."""
        popup.set_values(name, suggest(name, partial, use_store=False))

        cached = cache.get(name)
        if cached is not None and time.monotonic() - cached[0] < SUGGEST_TTL_S:
            _deliver(name, partial, cached[1])
            return
        if store is None and lookup is None:
            _show_or_hide()
            return

        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(
            suggest, name, "", component="ui.commands")
        worker.signals.finished.connect(
            lambda found, n=name, p=partial: _fetched(n, p, found))
        # A menu is a convenience. A store that is mid-index or closed costs
        # the suggestions and nothing else - the static values are already up.
        worker.signals.failed.connect(lambda _error: None)
        run(QThreadPool.globalInstance(), worker)
        _show_or_hide()

    def _fetched(name: str, partial: str, found: Any) -> None:
        cache[name] = (time.monotonic(), list(found or []))
        _deliver(name, partial, cache[name][1])

    def _deliver(name: str, partial: str, values: Sequence[str]) -> None:
        # **Only if the box is still asking the same question.** A slow answer
        # arriving after somebody has typed on is the stale-result problem every
        # other worker in this application guards against, and here it would
        # replace the menu under their fingers.
        _head, mode, now = slash_context(line_edit.text(), resolve)
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
        _head, mode, partial = slash_context(text, resolve)
        if mode == "command":
            popup.set_prefix(partial)
            _show_or_hide()
        elif mode == "value":
            name = text.rpartition(" ")[2].partition(":")[0]
            offer_values(name, partial)
        else:
            popup.popup().hide()

    def on_activated(row_text: str) -> None:
        """Insert what was chosen, and decide whether the menu stays open."""
        text = line_edit.text()
        word = text.rpartition(" ")[2]
        head = text[: len(text) - len(word)]

        if popup.value_of:
            # A value: complete the whole `name:value` and get out of the way.
            # The trailing space is what says "this filter is finished" - and
            # it is also what stops `_tail` reopening the menu immediately.
            line_edit.setText(f"{head}{popup.value_of}:{row_text} ")
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

    line_edit.textEdited.connect(on_text)
    popup.activated[str].connect(on_activated)
    return popup
