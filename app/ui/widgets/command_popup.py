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

Thin, like every widget here. The catalogue lives in `app/search/commands.py`
beside the parser that has to agree with it; this arranges a `QCompleter` over
whatever that returns.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QEvent, QObject, QStringListModel, Qt, pyqtSignal
from PyQt6.QtWidgets import QCompleter, QLineEdit

from app.search.commands import COMMANDS, matching

__all__ = ["CommandPopup", "attach_to"]

#: Shown per row: what to type, then what it does. Wide enough that the example
#: and the description both fit without the popup becoming a wall of text.
_ROW = "{example:<22} {summary}"


class CommandPopup(QCompleter):
    """Completes `/type`, `/from`, `/after`… inside a `QLineEdit`.

    Inserts `name:` rather than `/name`, so what lands in the box is the syntax
    the parser reads and the user can edit by hand afterwards. The slash is the
    doorway, not the grammar.
    """

    chosen = pyqtSignal(str)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._model = QStringListModel(self)
        self.setModel(self._model)
        self.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # Unfiltered: this widget decides what to show, because the match is
        # against aliases too - `/f` must offer `from`, and QCompleter's own
        # prefix matching only ever sees the display string.
        self.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.setFilterMode(Qt.MatchFlag.MatchContains)
        self.show_all()

    def show_all(self) -> None:
        self.set_prefix("")

    def set_prefix(self, prefix: str) -> None:
        """Narrow the list to commands matching what has been typed after `/`."""
        self._matches = matching(prefix)
        self._model.setStringList([
            _ROW.format(example=f"/{command.name} <{command.name}>",
                        summary=command.summary)
            for command in self._matches
        ])

    def command_at(self, row: int):
        """The `Command` behind a row, or None if the row is stale."""
        if 0 <= row < len(self._matches):
            return self._matches[row]
        return None

    @property
    def has_matches(self) -> bool:
        return bool(self._matches)


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


def attach_to(line_edit: QLineEdit) -> CommandPopup:
    """Wire a `CommandPopup` to a search box. Returns it, for tests and teardown.

    Kept as a function rather than a subclass of `QLineEdit` so the search view
    keeps a plain line edit - everything else about it (placeholder, clear
    button, Enter handling) stays exactly as it was, and removing this feature
    would be deleting one line.
    """
    popup = CommandPopup(line_edit)
    popup.setWidget(line_edit)

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

    def on_text(text: str) -> None:
        head, sep, tail = text.rpartition("/")
        # Only when the slash starts a word: `12/03` and `D:/docs` must not
        # open a menu.
        if not sep or (head and not head[-1].isspace()) or " " in tail:
            popup.popup().hide()
            return
        popup.set_prefix(tail)
        if popup.has_matches:
            popup.complete()
        else:
            popup.popup().hide()

    def on_activated(row_text: str) -> None:
        """Replace the half-typed `/name` with `name:` and leave the cursor after it."""
        name = row_text.split()[0].lstrip("/")
        command = next((c for c in COMMANDS if c.name == name), None)
        if command is None:
            return
        text = line_edit.text()
        head, sep, _tail = text.rpartition("/")
        line_edit.setText(f"{head}{command.name}:" if sep else text)
        line_edit.setCursorPosition(len(line_edit.text()))

    line_edit.textEdited.connect(on_text)
    popup.activated[str].connect(on_activated)
    return popup
