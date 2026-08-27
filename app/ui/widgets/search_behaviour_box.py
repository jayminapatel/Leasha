r"""What search may do for you — six switches, and what each tab does with them.

Layer: L6 (UI)

**A switch and a grid, and the grid is the honest half.** Six switches on their
own would be a lie by omission: "Fix obvious spelling" is on, and it still does
nothing on the Code tab, because a misspelt identifier may be exactly what is
in the codebase. Somebody who switches it on and finds no correction there has
been misled by a control that was telling the truth.

So the grid shows the *effect* per tab, read straight from
`app/search/policy.py`. It is not editable: what a person controls is whether a
behaviour is allowed at all, and each tab's contract is a design decision this
application stands behind. Making every cell editable would be twenty-four
controls in place of six, and twenty-three of them would be wrong to change.

**"Reset search behaviour to defaults" is one click**, the sibling of Index
Tuning's Return to automatic. Support at a distance depends on it: "press that
button and tell me what happens" is a sentence somebody can follow over the
telephone, and the alternative is six.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QLineEdit,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.result_table import align_headers
from app.search.policy import (
    BEHAVIOURS,
    SAFETY,
    SURFACE_LABELS,
    SURFACES,
    for_surface,
)

__all__ = ["SearchBehaviourBox"]

#: `behaviour -> registry key`. Written out because the two names differ on
#: purpose - see `policy.SETTING_FIELDS` for the same reasoning.
_KEYS = {
    "typo_correction": "SEARCH_FIX_SPELLING",
    "relax_on_empty": "SEARCH_RELAX_ON_EMPTY",
    "auto_chips": "SEARCH_AUTO_CHIPS",
    "recency_blend": "SEARCH_RECENCY_BLEND",
    "version_folding": "SEARCH_VERSION_FOLDING",
    "notice_register": "SEARCH_PLAIN_WORDS",
    "explain_results": "SEARCH_EXPLAIN_RESULTS",
}


def _cell(value: Any) -> str:
    """One grid cell: what this behaviour does on this tab, in two words.

    Words rather than ticks. A tick means "on", and three of these have a third
    state - *suggest* - which is the whole difference between the universal tab
    and the power ones. A column of ticks would hide exactly the distinction
    the grid exists to show.
    """
    if value in (True, "plain"):
        return "Yes"
    if value in (False, "off", "technical"):
        return "No"
    return str(value).capitalize()


class SearchBehaviourBox(QGroupBox):
    """The six switches, the effect grid, and the reset."""

    #: `{registry key: value}` - the shape `_settings_changed` writes.
    changed = pyqtSignal(dict)

    def __init__(self, settings: Any = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__("What search may do for you", parent)

        self.controls: dict = {}
        form = QFormLayout()

        for name, label, help_text in BEHAVIOURS:
            if name == "typo_correction":
                control: Any = QComboBox()
                control.addItem("Use the closest word and say so", "auto")
                control.addItem("Suggest it, change nothing", "suggest")
                control.addItem("Never", "off")
                control.currentIndexChanged.connect(
                    lambda _i: self._emit())
                form.addRow(label, control)
            else:
                control = QCheckBox(label)
                control.stateChanged.connect(lambda _s: self._emit())
                form.addRow(control)
            control.setObjectName(_KEYS[name])
            control.setToolTip(help_text)
            self.controls[name] = control

        # Object names, spelled out as literals so `test_settings_reachable`
        # can find them: it greps the source rather than building the widget,
        # which is what lets it run on a machine with no display. Naming them
        # from `_KEYS` above works at runtime and is invisible to that test -
        # the same trap the tuning screen's AutoSpin fell into.
        self.controls["typo_correction"].setObjectName("SEARCH_FIX_SPELLING")
        self.controls["relax_on_empty"].setObjectName("SEARCH_RELAX_ON_EMPTY")
        self.controls["auto_chips"].setObjectName("SEARCH_AUTO_CHIPS")
        self.controls["recency_blend"].setObjectName("SEARCH_RECENCY_BLEND")
        self.controls["version_folding"].setObjectName("SEARCH_VERSION_FOLDING")
        self.controls["notice_register"].setObjectName("SEARCH_PLAIN_WORDS")
        self.controls["explain_results"].setObjectName("SEARCH_EXPLAIN_RESULTS")

        # **Not a `SearchPolicy` behaviour, and deliberately outside the
        # grid.** The seven above are per-surface contracts about *what a
        # search may do*, and `test_policy_reaches_the_engine` requires each
        # of them to change the answer that comes back. This changes nothing
        # about any answer - it decides whether an empty box offers you your
        # own past questions - so making it an eighth behaviour would have
        # meant a row in the grid with the same word in all four columns and
        # a guard nobody could satisfy honestly.
        self.offer_recent = QCheckBox("Offer what you searched for before")
        self.offer_recent.setObjectName("SEARCH_OFFER_RECENT")
        self.offer_recent.setToolTip(
            "Clicking into an empty search box shows the last few things you "
            "looked for, and the searches you saved. Switch it off on a "
            "screen other people can see.")
        self.offer_recent.stateChanged.connect(lambda _s: self._emit())
        form.addRow(self.offer_recent)

        # **Workspace §3a.** A global shortcut is the most intrusive thing
        # this application does to a machine, so it is switchable and its
        # combination is typed rather than fixed. The line under it says
        # whether the operating system actually granted it - a shortcut that
        # silently does not work is indistinguishable from a broken
        # application.
        self.mini_search = QCheckBox("Search from anywhere with a shortcut")
        self.mini_search.setObjectName("MINI_SEARCH_ENABLED")
        self.mini_search.setToolTip(
            "Press the shortcut in any application and a small search box "
            "appears. Type, press Enter, and the document opens.")
        self.mini_search.stateChanged.connect(lambda _s: self._emit())
        form.addRow(self.mini_search)

        self.mini_hotkey = QLineEdit()
        self.mini_hotkey.setObjectName("MINI_SEARCH_HOTKEY")
        # A form label is not an accessible name on every reader, and this box
        # holds the one setting somebody using a screen reader is most likely
        # to want - the shortcut that opens search without the mouse.
        self.mini_hotkey.setAccessibleName("The shortcut that opens search")
        self.mini_hotkey.setPlaceholderText("Ctrl+Alt+L")
        self.mini_hotkey.setToolTip(
            "Something like Ctrl+Alt+L. It needs at least one of Ctrl, Alt, "
            "Shift or Win. If another program is already using it, Leasha "
            "says so and nothing changes.")
        self.mini_hotkey.editingFinished.connect(self._emit)
        form.addRow("The shortcut that opens it", self.mini_hotkey)

        self.mini_status = QLabel("")
        self.mini_status.setObjectName("settingsHint")
        self.mini_status.setWordWrap(True)
        form.addRow("", self.mini_status)

        self.grid = self._build_grid()

        self.reset = QPushButton("Reset search behaviour to defaults")
        self.reset.setObjectName("reset-search-behaviour")
        self.reset.setToolTip(
            "Put all six back to how they arrived. Nothing else changes - "
            "your folders, your index and everything you have found stay "
            "exactly as they are.")
        self.reset.clicked.connect(lambda _c=False: self.restore_defaults())

        note = QLabel("Each tab uses these differently, and the table shows "
                      "how. Switching one off here switches it off "
                      "everywhere.\n\n" + "\n".join(f"• {line}"
                                                    for line in SAFETY))
        note.setWordWrap(True)
        note.setObjectName("searchBehaviourNote")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.grid)
        layout.addWidget(self.reset)
        layout.addWidget(note)

        if settings is not None:
            self.load(settings)

    # -- the grid ------------------------------------------------------------

    def _build_grid(self) -> QTableWidget:
        """One row per behaviour, one column per tab, read from the policy."""
        grid = QTableWidget(len(BEHAVIOURS), len(SURFACES) + 1, self)
        grid.setObjectName("searchBehaviourGrid")
        grid.setHorizontalHeaderLabels(
            ["Behaviour"] + [SURFACE_LABELS[name] for name in SURFACES])
        grid.verticalHeader().setVisible(False)
        # §2b. Not sortable: the rows are a fixed matrix of behaviour by tab,
        # so there is no ordering question to answer - but a centred heading
        # over a left-aligned column is the same mismatch everywhere.
        align_headers(grid)
        # Read-only on purpose: what somebody controls is whether a behaviour
        # is allowed at all. Each tab's contract is a decision this
        # application stands behind, and twenty-four editable cells would be
        # twenty-three ways to make search worse.
        grid.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        grid.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        policies = {name: for_surface(name) for name in SURFACES}
        for row, (name, label, _help) in enumerate(BEHAVIOURS):
            grid.setItem(row, 0, QTableWidgetItem(label))
            for column, surface in enumerate(SURFACES, start=1):
                value = getattr(policies[surface], name, None)
                grid.setItem(row, column, QTableWidgetItem(_cell(value)))
        grid.resizeColumnsToContents()
        return grid

    # -- state ---------------------------------------------------------------

    def load(self, settings: Any) -> None:
        """Fill from Settings without emitting on the way in."""
        for name, control in self.controls.items():
            control.blockSignals(True)
        try:
            found = self.controls["typo_correction"].findData(
                str(getattr(settings, "search_fix_spelling", "auto") or "auto"))
            self.controls["typo_correction"].setCurrentIndex(
                found if found >= 0 else 0)
            for name, field in (
                ("relax_on_empty", "search_relax_on_empty"),
                ("auto_chips", "search_auto_chips"),
                ("recency_blend", "search_recency_blend"),
                ("version_folding", "search_version_folding"),
                ("notice_register", "search_plain_words"),
                ("explain_results", "search_explain_results"),
            ):
                self.controls[name].setChecked(
                    bool(getattr(settings, field, True)))
            self.offer_recent.blockSignals(True)
            self.offer_recent.setChecked(
                bool(getattr(settings, "search_offer_recent", True)))
            self.offer_recent.blockSignals(False)
            for control, value in (
                (self.mini_search,
                 bool(getattr(settings, "mini_search_enabled", True))),
                (self.mini_hotkey,
                 str(getattr(settings, "mini_search_hotkey", "") or "")),
            ):
                control.blockSignals(True)
                if isinstance(value, bool):
                    control.setChecked(value)
                else:
                    control.setText(value)
                control.blockSignals(False)
            self.say_hotkey(getattr(settings, "mini_search_hotkey", ""))
        finally:
            for control in self.controls.values():
                control.blockSignals(False)

    def values(self) -> dict:
        """`{registry key: value}` for the writer.

        **Spelled out rather than built from `_KEYS`.** A loop is shorter and
        invisible to `test_settings_reachable`, which greps the source for the
        key so that it can run on a machine with no display - and a control
        that test cannot see is a control that can quietly stop saving. Six
        literal lines is the price of the guard having teeth.
        """
        return {
            "SEARCH_FIX_SPELLING": str(
                self.controls["typo_correction"].currentData() or "auto"),
            "SEARCH_RELAX_ON_EMPTY": bool(
                self.controls["relax_on_empty"].isChecked()),
            "SEARCH_AUTO_CHIPS": bool(self.controls["auto_chips"].isChecked()),
            "SEARCH_RECENCY_BLEND": bool(
                self.controls["recency_blend"].isChecked()),
            "SEARCH_VERSION_FOLDING": bool(
                self.controls["version_folding"].isChecked()),
            "SEARCH_PLAIN_WORDS": bool(
                self.controls["notice_register"].isChecked()),
            "SEARCH_EXPLAIN_RESULTS": bool(
                self.controls["explain_results"].isChecked()),
            "SEARCH_OFFER_RECENT": bool(self.offer_recent.isChecked()),
            "MINI_SEARCH_ENABLED": bool(self.mini_search.isChecked()),
            "MINI_SEARCH_HOTKEY": self.mini_hotkey.text().strip(),
        }

    def restore_defaults(self) -> None:
        """All six back to how they arrived, in one click.

        **Nothing else changes**, and the tooltip says so: somebody reaching
        for a reset button while search is behaving oddly needs to know it will
        not touch their index. That fear is why reset buttons go unpressed.
        """
        for control in self.controls.values():
            control.blockSignals(True)
        try:
            self.controls["typo_correction"].setCurrentIndex(0)
            for name in ("relax_on_empty", "auto_chips", "recency_blend",
                         "version_folding", "notice_register",
                         "explain_results"):
                self.controls[name].setChecked(True)
            self.offer_recent.blockSignals(True)
            self.offer_recent.setChecked(True)
            self.offer_recent.blockSignals(False)
        finally:
            for control in self.controls.values():
                control.blockSignals(False)
        self._emit()

    def say_hotkey(self, text: Any, *, registered: bool = True) -> None:
        """Put the plain sentence about the shortcut under the box.

        Pushed in by the window, because whether the operating system granted
        the combination is something only the thing that asked for it knows.
        """
        from app.ui.hotkey import describe

        self.mini_status.setText(describe(text, registered=registered))

    def _emit(self) -> None:
        self.changed.emit(self.values())
