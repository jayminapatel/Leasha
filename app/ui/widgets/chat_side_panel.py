"""The Chat tab's side strip: two vertical tabs, Sources and Preview.

Layer: L5 view

2026-10-08, the owner, looking at the Chat tab with the preview stacked under the
sources list: "that should i think should be its own vertical tab and the source
tab viewing should be able to turn off and on too". So the right edge carries a
slim strip, the way an editor's side bar does: **click a tab to show that panel;
click the one already showing to put the whole panel away** and give the
conversation the full width. The decision is `presenter.chat.panel_after_click`;
`SideTabs` draws the strip and says which tab was clicked, and `PanelSwitch` keeps
the strip, the panel, the splitter and the toolbar toggles in step, and remembers
which page showed, whether it showed and how wide it was (`PANEL_KEY`). Ctrl+B
(bound by the window) calls the view's `toggle_panel`.

`panel_toggle` is the toolbar twin of one tab - the same icon-only, checkable
button the other tabs' Preview toggle is (`view_options.preview_toggle`), for the
Sources tab that toggle never had.

Styled here, not in `theme.py`: the colours are the theme's own tokens, read when
the strip is built and again on every theme change (`retint`, which the window
calls through the view's `toggles`).
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Mapping, Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QToolButton, QVBoxLayout, QWidget

from app.ui.presenter.chat import (
    PANEL_KEY, PREVIEW_TAB, SOURCES_TAB, TAB_LABELS, TAB_TIPS, PanelState, panel_after_click,
    panel_state_from_text, panel_state_text,
)
from app.ui.state_writes import save_state
from app.ui.widgets.icons import icon

__all__ = ["SideTabs", "PanelSwitch", "panel_toggle", "side_toggles", "TAB_ICONS",
           "WIDTH_SETTLE_MS"]

#: How long the panel's width must stay put before it is saved: a drag of the
#: splitter sends a move for every pixel.
WIDTH_SETTLE_MS = 600

#: The icon each tab carries, from the shipped Lucide set (`icons.ICON_NAMES`).
TAB_ICONS = {"sources": "list", "preview": "eye"}

#: The strip's own look: no box, a hairline against the conversation, and the tab
#: that is showing on the accent's soft ground - the rail's "you are here".
_SHEET = """
#chatSideTabs {{ background: {window}; border: none; border-left: 1px solid {divider}; }}
#chatSideTabs QToolButton {{
    border: none; border-radius: 8px; background: transparent;
    color: {text_dim}; padding: 6px 2px 5px 2px; margin: 0px;
}}
#chatSideTabs QToolButton:hover {{ background: {surface_hover}; color: {text}; }}
#chatSideTabs QToolButton:checked {{ background: {accent_soft}; color: {accent_text}; font-weight: 600; }}
#chatSideTabs QToolButton:focus {{ outline: none; border: 1px solid {focus_ring}; }}
"""


def _colours(colours: Optional[Mapping[str, str]] = None) -> dict:
    if colours is None:
        from app.ui.theme import theme_colours

        colours = theme_colours()
    base = {"window": "#f7f7f8", "divider": "#ebecef", "text_dim": "#585e66",
            "text": "#1b1d20", "surface_hover": "#e8eaed", "accent_soft": "#e9e4fb",
            "accent_text": "#15084b", "focus_ring": "#4a37b0"}
    return {**base, **{k: v for k, v in dict(colours).items() if isinstance(v, str)}}


class SideTabs(QFrame):
    """A column of checkable tabs on the window's right edge. At most one is checked:
    the panel that is showing, or none when the panel is put away."""

    #: The name of the tab that was clicked ("sources" or "preview").
    clicked = Signal(str)

    def __init__(self, labels: Mapping[str, str], tips: Mapping[str, str],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatSideTabs")
        self.setAccessibleName("Side panel tabs")
        self.buttons: dict[str, QToolButton] = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(4, 8, 4, 8)
        column.setSpacing(4)
        for name, words in labels.items():
            tab = QToolButton()
            tab.setText(words)
            tab.setToolTip(tips.get(name, words))
            tab.setAccessibleName(f"{words} panel")
            tab.setCheckable(True)
            tab.setAutoRaise(True)
            tab.setCursor(Qt.CursorShape.PointingHandCursor)
            tab.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            tab.setIconSize(QSize(20, 20))
            tab.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            tab.setProperty("sideTab", name)
            # `clicked`, not `toggled`: Qt flips the check before the slot runs, and
            # the view decides what is checked afterwards (`set_current`).
            tab.clicked.connect(lambda _c=False, n=name: self.clicked.emit(n))
            tab.toggled.connect(lambda _on: self.retint())
            column.addWidget(tab)
            self.buttons[name] = tab
        column.addStretch(1)
        self.retint()
        width = max((b.sizeHint().width() for b in self.buttons.values()), default=48)
        self.setFixedWidth(max(56, width + 12))
        for tab in self.buttons.values():
            tab.setMinimumWidth(width)

    def set_current(self, name: str) -> None:
        """Check `name` alone (or none, for ""), without anybody hearing a click."""
        for key, tab in self.buttons.items():
            if tab.isChecked() != (key == name):
                tab.blockSignals(True)
                tab.setChecked(key == name)
                tab.blockSignals(False)
        self.retint()

    def current(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "")

    def retint(self, colours: Optional[Mapping[str, str]] = None) -> None:
        """Repaint for the palette in use: the sheet, and each tab's icon in the
        colour its words are drawn in."""
        palette = _colours(colours)
        sheet = _SHEET.format(**palette)
        if self.styleSheet() != sheet:
            self.setStyleSheet(sheet)
        for name, tab in self.buttons.items():
            ink = palette["accent_text"] if tab.isChecked() else palette["text_dim"]
            tab.setIcon(icon(TAB_ICONS.get(name, "list"), ink))


class PanelSwitch(QObject):
    """Shows, hides and sizes the side panel, and remembers it. Not a widget: it
    holds the splitter, the panel, the strip and the two toolbar toggles, and keeps
    them in step - the strip and toggles checked for the page that shows, the
    conversation given the room when the panel is away, the width put back when it
    returns. What a click *means* is `presenter.chat.panel_after_click`."""

    #: Whether the Preview page is what shows - the controller remembers it as
    #: the Preview toggle's state, as it did before the panel had pages.
    preview_toggled = Signal(bool)

    def __init__(self, view: Any) -> None:
        """Gives `view` its strip (`side_tabs`) and its `toggles`; `lay_out` then
        places the strip once the view has built its splitter."""
        super().__init__(view)
        self.view = view
        self.split: Any = None
        self.panel = view.sources
        self.tabs = view.side_tabs = SideTabs(TAB_LABELS, TAB_TIPS)
        self.toggles = view.toggles = side_toggles(view, self)
        self.store: Any = None
        self.state = PanelState(open=False)
        self._width_wanted = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(WIDTH_SETTLE_MS)
        self._timer.timeout.connect(self._width_settled)

    def lay_out(self, outer: Any, split: Any) -> None:
        """The splitter, then the strip on the window's edge - outside the splitter,
        so it stays in reach while the panel is away - and the wiring."""
        self.split = split
        outer.setSpacing(0)
        margins = outer.contentsMargins()
        outer.setContentsMargins(margins.left(), margins.top(), 0, margins.bottom())
        outer.addWidget(split, stretch=1)
        outer.addWidget(self.tabs)
        split.splitterMoved.connect(lambda _pos, _index: self._timer.start())
        self.tabs.clicked.connect(self.tab_clicked)
        self.panel.preview_wanted.connect(lambda: self.show_tab(PREVIEW_TAB))
        self.preview_toggled.connect(self.view.preview_toggled)
        self.view.installEventFilter(self)
        self.apply(PanelState(), save=False)

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt
        if event.type() == QEvent.Type.Show:
            QTimer.singleShot(0, self, self.apply_width)     # the width, once laid out
        return False

    def preview_result(self, row: Any) -> None:
        """A result row inside an answer was picked: previewed, Preview brought forward."""
        self.panel.preview_row(row)
        if row is not None:
            self.show_tab(PREVIEW_TAB)

    def show_sources(self, on: bool) -> None:
        self.toolbar(SOURCES_TAB, on)

    # -- what the person does ---------------------------------------------------
    def restore(self, store: Any) -> None:
        """Put the panel back as it was left. One keyed row, read as every
        remembered view choice is read; a store that cannot answer leaves the default."""
        self.store = store
        try:
            saved = store.get_state(PANEL_KEY, "") if store is not None else ""
        except Exception:                                 # noqa: BLE001 - a preference
            saved = ""
        if saved:
            self._width_wanted = True
            self.apply(panel_state_from_text(saved), save=False)

    def tab_clicked(self, name: str) -> None:
        """A tab on the strip: show it, or put the panel away if it is showing."""
        self.apply(panel_after_click(self.current(), name))

    def show_tab(self, name: str) -> None:
        """Show page `name`, bringing the panel out if it was away."""
        self.apply(PanelState(True, name, self.current().width))

    def hide(self) -> None:
        state = self.current()
        self.apply(PanelState(False, state.tab, state.width))

    def toggle(self) -> None:
        """Put the panel away, or bring it back on the page it last showed."""
        state = self.current()
        self.apply(PanelState(not state.open, state.tab, state.width))

    def toolbar(self, name: str, on: bool) -> None:
        """A toolbar toggle: its page, or the panel away if that page is showing."""
        if on:
            self.show_tab(name)
        elif self.state.open and self.state.tab == name:
            self.hide()

    # -- keeping it all in step ---------------------------------------------------
    def current(self) -> PanelState:
        """The state, with the width the person has dragged the panel to."""
        width = self._width()
        state = self.state
        return PanelState(state.open, state.tab, width) if width else state

    def _width(self) -> int:
        if not self.state.open or not self.panel.isVisible():
            return 0
        sizes = self.split.sizes()
        return int(sizes[2]) if len(sizes) > 2 and sizes[2] > 0 else 0

    def apply(self, state: PanelState, *, save: bool = True) -> None:
        was_preview = self.state.open and self.state.tab == PREVIEW_TAB
        opening, closing = state.open and not self.state.open, self.state.open and not state.open
        before = self.split.sizes()
        self.state = state
        self.panel.show_page(state.tab if state.open else "")
        self.panel.setVisible(state.open)
        if closing and len(before) > 2 and sum(before) > 0:
            # The room goes to the conversation, not to the list of chats.
            self.split.setSizes([before[0], before[1] + before[2], 0])
        self.tabs.set_current(state.tab if state.open else "")
        for name, key in ((SOURCES_TAB, "sources"), (PREVIEW_TAB, "inspector")):
            self.toggles[key].set_quietly(state.open and state.tab == name)
        if opening:
            self._width_wanted = True
            self.apply_width()
        now_preview = state.open and state.tab == PREVIEW_TAB
        if now_preview != was_preview:
            self.preview_toggled.emit(now_preview)
        if save:
            self._save()

    def apply_width(self) -> None:
        """Give the panel the width it was left at, once the window has one to share."""
        if not self._width_wanted or not self.state.open:
            return
        sizes = self.split.sizes()
        total = sum(sizes)
        if len(sizes) < 3 or total <= 0 or not self.split.isVisible():
            return                                    # not laid out yet: on the next show
        self._width_wanted = False
        width = min(int(self.state.width), max(0, total - sizes[0] - 320))
        if width > 0:
            self.split.setSizes([sizes[0], max(1, total - sizes[0] - width), width])

    def _width_settled(self) -> None:
        width = self._width()
        if width and width != self.state.width:
            self.state = PanelState(self.state.open, self.state.tab, width)
            self._save()

    def _save(self) -> None:
        save_state(self.store, PANEL_KEY, panel_state_text(self.current()), component="ui.chat")


def side_toggles(view: Any, switch: PanelSwitch) -> dict:
    """The Chat view's `toggles`: the Sources toggle, the Preview toggle every tab
    carries (`view_options.preview_toggle`), and - not toggles, but repainted on a
    theme change the same way (`retint_toggles`) - the strip and the shelf."""
    from app.ui.view_options import preview_toggle

    return {
        "sources": panel_toggle("sources", "list", tip=TAB_TIPS[SOURCES_TAB],
                                accessible="Sources panel", on_toggle=switch.show_sources),
        "inspector": preview_toggle(view, checked=False, on_toggle=view.show_preview),
        "side_tabs": view.side_tabs,
        "shelf": view.shelf,
    }


def panel_toggle(name: str, icon_name: str, *, tip: str, accessible: str,
                 on_toggle: Callable[[bool], Any]) -> QToolButton:
    """An icon-only checkable toolbar button for one side-panel tab.

    The same control as `view_options.preview_toggle` - object name
    `toggle_<name>`, `iconToggle` for the sheet, `retint` and `set_quietly` -
    so it sits beside the Preview toggle and looks like its twin."""
    toggle = QToolButton()
    toggle.setObjectName(f"toggle_{name}")
    toggle.setProperty("iconToggle", True)
    toggle.setCheckable(True)
    toggle.setAutoRaise(True)
    toggle.setText(accessible)
    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    toggle.setToolTip(tip)
    toggle.setAccessibleName(accessible)
    toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    toggle.icon_name = icon_name

    def paint(colours: Any = None) -> None:
        palette = _colours(colours)
        ink = palette.get("accent_text") if toggle.isChecked() else palette.get("text_dim")
        toggle.setIcon(icon(icon_name, ink or "#888888"))

    def quietly(on: bool) -> None:
        if toggle.isChecked() != bool(on):
            toggle.blockSignals(True)
            toggle.setChecked(bool(on))
            toggle.blockSignals(False)
        paint()

    # A bound method of the view is held weakly, as `preview_toggle` holds its
    # own: the toggle is the view's child, and a strong hold would keep it alive.
    held = weakref.WeakMethod(on_toggle) if hasattr(on_toggle, "__self__") else (lambda: on_toggle)

    def clicked(_c: bool = False) -> None:
        target = held()
        if target is not None:
            target(toggle.isChecked())

    toggle.clicked.connect(clicked)
    toggle.toggled.connect(lambda _on: paint())
    toggle.retint = paint
    toggle.set_quietly = quietly
    paint()
    return toggle
