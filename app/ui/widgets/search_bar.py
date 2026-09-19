"""The controls beside the search box: scope, Interpret, rerank, view.

Layer: L5

Its own module for the reason `indexing_settings.py` and `model_box.py` are:
`search_view.py` had grown past the length a view is allowed to be, and the rule
that keeps views short is the rule that keeps logic out of them. Four controls
with four tooltips is sixty lines of construction that says nothing about how
searching works.

**Every tooltip here is load-bearing.** Each of these controls changes what a
search does, and none of them is self-explanatory from its label — "Rerank" in
particular is a word nobody outside this project has met. A control whose effect
you cannot predict is a control people stop touching.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QWidget

from app.ui.widgets.segmented import SegmentedControl

__all__ = [
    "build_input", "build_scope", "build_interpret", "build_rerank",
    "build_controls", "build_toolbar", "SCOPES",
]


def build_input(parent: Optional[QWidget], on_typed: Any, on_submit: Any,
                store: Any = None, on_scope: Any = None) -> Any:
    """The search box, its `/` dropdown, and what it offers when empty.

    Returns `(line_edit, completer, saved)`. The completer must be kept alive
    by the caller: a `QCompleter` that is garbage collected stops completing,
    silently, and the dropdown would simply never appear again.

    **`SavedSearches` is built here rather than in the view**, and that is not
    tidiness: `search_view.py` sits at the 250-line guard, and the object is
    only ever reached through this box - it decides what an empty one offers
    (§2e) and what `saved:name` expands to (Adoptions §3). `on_scope` is what
    a saved search calls when it carries one.

    **The placeholder advertises the doorway, not the grammar.** Every filter
    worked since Layer 4 and nothing in the application had ever mentioned them,
    so the box was in practice a bag of words. Five operators do not fit in a
    placeholder and nobody read them when they were there; `/` does fit, and it
    is the convention every chat tool has already taught people.
    """
    from PyQt6.QtWidgets import QLineEdit

    from app.ui.widgets.command_popup import attach_to

    box = QLineEdit(parent)
    box.setObjectName("searchBox")          # UI Redesign §1b/§3: the one box
    # **The thinnest placeholder in the application, on the box that most needs
    # a good one.** "press / for filters" says a menu exists and nothing about
    # what is in it; Files, Mail and Code all name real examples, and this is
    # the box somebody meets first. Naming three switches from three different
    # sources is also the only thing on screen that says the box reaches mail
    # and repositories at all.
    box.setPlaceholderText(
        "Search everything — files, mail and code.  "
        "Or / for filters: /type pdf  /from dave  /newest")
    box.setClearButtonEnabled(True)
    box.textChanged.connect(on_typed)
    box.returnPressed.connect(on_submit)
    # **Every switch there is, index and repository alike.** Asked for as
    # *"Search should have all switches"*, and honest only because the box can
    # now answer the repository ones: `app/search/federate.py` runs them, on the
    # full tier, over the repositories in the index. Offering them before that
    # existed would have been the failure this application keeps finding - a
    # menu row that quietly does nothing.
    #
    # The same list the Code tab uses. One catalogue, because the two boxes now
    # reach the same two engines and a second list would only be somewhere for
    # them to disagree.
    #
    # **`SEARCH_CATALOGUE`, not `ALL_CATALOGUE`.** Adoptions §3 adds `/saved`,
    # which is an action rather than a filter and only this box can run one -
    # it re-executes through the main engine and carries a scope, and the Code
    # box has neither. Offering it there would be a row that quietly does
    # nothing, which this widget's own opening note forbids.
    from app.ui.widgets.code_commands import (
        SEARCH_CATALOGUE, search_command_for, search_matching,
    )

    # `store` only makes the *value* half of the menu better - which extensions
    # exist, who has sent mail, which repositories were found. Without it the
    # grammar's own values are still offered, so a box with no store is not a
    # box with a broken menu.
    #
    # No `lookup`: the Code tab can offer real branch and author names because
    # it knows which repository is selected, and this box does not. The grammar's
    # own suggestions still appear, which is the honest half of that menu.
    from app.ui.saved_box import SavedSearches

    saved = SavedSearches(store, on_scope)
    saved.refresh()
    return box, attach_to(
        box, store=store, catalogue=SEARCH_CATALOGUE,
        matcher=search_matching, resolve=search_command_for,
        # §2e: the last few things this person searched for, and the searches
        # they saved. Asked for at the moment the box is focused and empty,
        # answered from lists already in memory - see `saved_box`.
        offers=saved.sections,
    ), saved

#: (label, value). Value travels into `ParsedQuery.scope`.
#:
#: A filter, not a mode: nobody should have to decide whether a thing was an
#: email or a document *before* typing, because the usual answer is "I do not
#: remember, that is why I am searching".
SCOPES: tuple[tuple[str, str], ...] = (
    ("Everything", "all"),
    ("Mail only", "mail"),
    ("Documents only", "documents"),
    ("Code only", "code"),
)


def build_scope(parent: Optional[QWidget], on_change: Any) -> SegmentedControl:
    """The scope chips.

    **"Everything" is a union, not a fallback.** This box narrows; it never
    adds. Whatever a focused tab can find, the search box finds too - so a
    filter learned anywhere works here, and there is never a reason to go to
    another tab to ask a question this one cannot.

    UI Redesign (202626160950 §3c): a segmented control under the box rather
    than a combo beside it. Same four strings, same signal, same value - the
    `SegmentedControl` keeps the combo's surface so nothing downstream moved.
    """
    scope = SegmentedControl(parent)
    for label, value in SCOPES:
        scope.addItem(label, value)
    scope.setToolTip(
        "Narrow the search. Everything searches all of it.\n\n"
        "Mail only — messages, showing who sent them and when instead of a path.\n"
        "Documents only — files on disk that are not in a repository.\n"
        "Code only — anything inside a code repository, whatever its type:\n"
        "a README in a repository counts, a .py file in Downloads does not.\n\n"
        "For 'files that look like code' wherever they are, type type:code instead."
    )
    scope.currentIndexChanged.connect(on_change)
    return scope


def scope_value(scope: Any) -> str:
    """The selected scope, defaulting to everything."""
    return str(scope.currentData() or "all")


def select_scope(scope: Any, value: str) -> None:
    """Select a scope by value. An unknown value is ignored.

    Ignored rather than raised: this is driven across a tab boundary with a
    plain string - the Code tab asks for `code` when it hands a repository to
    the search box - and a stale caller must not be able to close the window.
    """
    index = scope.findData(value)
    if index >= 0:
        scope.setCurrentIndex(index)


def build_interpret(parent: Optional[QWidget], on_click: Any) -> QAction:
    """The Interpret button.

    **Explicit, never automatic.** Plain Enter runs what was typed, exactly as
    it always has; this button is the person choosing to spend a second on a
    model. Silently interpreting every search would make results unpredictable,
    and unpredictable search over your own archive is worse than blunt search,
    because you stop trusting it.

    Hidden entirely when interpretation is switched off — see
    `SearchView.set_interpret_enabled`. A greyed-out button is a permanent
    question with no answer visible on the screen it appears on.
    """
    # UI Redesign (202626160950 §3e, owner decision [FINALISE 3]): a QAction
    # in the toolbar's `⋯` menu rather than a button beside the box. Same
    # label, same tooltip, same Ctrl+Enter; `setVisible`/`isVisible`/
    # `setEnabled` are the surface `search_view` and `interpret_into` use, and
    # a QAction has all three.
    button = QAction("Interpret", parent)
    button.setToolTip(
        "Turn a sentence into a search query using a local model.  Ctrl+Enter\n\n"
        "The query it builds goes into the box so you can read and edit it.\n"
        "If the model is unavailable, your words are searched for unchanged."
    )
    button.triggered.connect(on_click)
    return button


def build_rerank(parent: Optional[QWidget], on_change: Any) -> QAction:
    r"""The rerank toggle.

    **Off by default since work order 0 section 6's P8** - see the
    `rerank_enabled` field in `app.core.config.Settings` for the measurement
    that closed it (0.46s to 8s per search against a 300ms warm budget,
    against a quality gain nobody has measured yet). Offered at all because
    it is the single biggest cost in the pipeline, and somebody who has
    measured their own corpus and wants the extra precision should be able
    to have it without editing configuration or restarting.

    **It is not the source of truth, and pretending otherwise made it lie.**
    This box was hard-coded checked and never persisted, while the Settings
    checkbox beside it *was* persisted - so somebody who turned reranking off in
    Settings came back to a toolbar claiming it was on, and every search from
    that box asked for it again. Two controls for one setting that never agreed:
    the same shape as the bug fixed in `set_message` the day before.
    `shell` now initialises both from `ui:rerank_enabled` and routes both
    through one handler - and, now that the hard-coded value here agrees with
    `Settings.rerank_enabled`'s own default, a fresh install shows the same
    "off" on both controls rather than a toolbar that briefly disagreed with
    itself before the stored state (if any) was read.
    """
    # UI Redesign §3e: a checkable QAction in the `⋯` menu. `toggled`,
    # `isChecked`, `setChecked` and `blockSignals` are what the window uses
    # (`_set_toolbar_rerank`), and a QAction has all four.
    toggle = QAction("Rerank", parent)
    toggle.setCheckable(True)
    toggle.setToolTip(
        "Slower but more precise ordering. Turning it off does not need a "
        "restart, and it is the same setting as the one on the Settings page."
    )
    toggle.setChecked(False)
    toggle.toggled.connect(on_change)
    return toggle


def build_toolbar(view: Any, *, controls: Any, status: Any, body: Any) -> Any:
    r"""Lay the search page out: the home state, the compact bar, the results.

    **Assembly, not decision** - and here because this module is already "the
    search bar and its controls", while `search_view.py` is held under 250 lines
    by `test_every_qt_view_keeps_its_logic_in_the_presenter`.

    UI Redesign (202626160950 §3). Two states, one box:

    * **home** (§3a) - nothing typed: `SearchHome` holds the box in its slot.
    * **compact** (§3b) - the box at the top at full width; under it one row
      with the segmented scope (§3c), the filter chips (§3d), the icon toggles
      and the `⋯` menu (§3e); then the summary line (§3f), the notice bar
      (§3g, unchanged) and the results.

    The switch is driven from the box's own `textChanged`, so the view file
    gained no lines for it. `controls` is `(interpret, scope, rerank, view)`
    as the view has always passed it; the actions are already in the menu.

    Returns the notice bar, which the view needs to write degradations into.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

    from app.ui.widgets.chips import ChipRow
    from app.ui.widgets.notice_bar import NoticeBar
    from app.ui.widgets.search_home import SearchHome

    scope = next(c for c in controls if isinstance(c, SegmentedControl))
    view_button = next(c for c in controls if isinstance(c, QWidget) and c is not scope)
    view_button.setText("View")

    notices = NoticeBar(view)
    chips = ChipRow(view)
    view.chips = chips

    # -- the compact bar ------------------------------------------------------
    bar = QWidget(view)
    bar.setObjectName("searchBar")
    bar_layout = QVBoxLayout(bar)
    bar_layout.setContentsMargins(0, 0, 0, 0)
    bar_layout.setSpacing(8)
    box_slot = QHBoxLayout()
    bar_layout.addLayout(box_slot)

    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(scope)
    row.addWidget(chips, 1)
    row.addStretch(1)
    view.toggles = _mirror_switches(view, row, body)
    row.addWidget(view_button)
    row.addWidget(view.more_button)
    bar_layout.addLayout(row)

    summary = QHBoxLayout()
    summary.addWidget(status, 1)
    hint = QLabel("Interpret  Ctrl+Enter")
    hint.setObjectName("searchStatus")
    hint.setAlignment(Qt.AlignmentFlag.AlignRight)
    interpret = next((c for c in controls if isinstance(c, QAction) and c.text() == "Interpret"), None)
    if interpret is not None:
        hint.setVisible(interpret.isVisible())
        interpret.changed.connect(lambda: hint.setVisible(interpret.isVisible()))
    summary.addWidget(hint)
    bar_layout.addLayout(summary)
    # §3f: the results pane's own summary label hides when it would repeat
    # the status line.
    fold = getattr(getattr(view, "results", None), "fold_summary_with", None)
    if callable(fold):
        fold(status)

    # -- the home state -------------------------------------------------------
    def type_into(text: str) -> None:
        view.input.setText(text)
        view.search_now()

    home = SearchHome(on_type=type_into,
                      sections=getattr(getattr(view, "saved", None), "sections", lambda: ()))
    view.home = home

    layout = QVBoxLayout(view)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(8)
    layout.addWidget(home, 1)
    layout.addWidget(bar)
    # Above the results and below the status line: a degradation is about the
    # results, so it belongs where the eye lands before reading them.
    layout.addWidget(notices)
    layout.addWidget(body, stretch=1)

    state = {"home": None}

    def show_home(on: bool) -> None:
        if state["home"] is on:
            return
        state["home"] = on
        had_focus = view.input.hasFocus()
        if on:
            home.refresh()
            home.lend(view.input)
        else:
            view.input.setMaximumWidth(16777215)
            view.input.setProperty("empty_state", False)
            view.input.style().unpolish(view.input)
            view.input.style().polish(view.input)
            box_slot.addWidget(view.input, 1)
        home.setVisible(on)
        bar.setVisible(not on)
        notices.setVisible(not on)
        body.setVisible(not on)
        if had_focus:
            view.input.setFocus()

    def on_text(text: str) -> None:
        chips.show_for(text)
        show_home(not text.strip())

    view.input.textChanged.connect(on_text)
    chips.text_edited.connect(view.input.setText)
    show_home(True)
    return notices


def _mirror_switches(view: Any, row: Any, body: Any) -> dict:
    """§3e: the results pane's checkboxes become icon toggles and menu items.

    The `QCheckBox`es built by `result_tools._switches` stay the owners of the
    label, the tooltip, the persisted flag and the wiring; they are hidden and
    each is mirrored two ways - into a checkable icon button on the bar
    (pinned, timeline, grid) or into a checkable action in the `⋯` menu
    (dragging out, drawings). The mirror's tooltip is the checkbox's own text
    followed by its own tooltip, so nothing anybody learned is lost.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QToolButton

    switches = getattr(body, "switches", None)
    boxes = getattr(switches, "boxes", {}) if switches is not None else {}
    if switches is not None:
        switches.setVisible(False)
    icons = {"timeline": "chart-no-axes-column", "pinned": "pin", "grid": "layout-grid"}
    made: dict = {}

    def bind(box: Any, target: Any) -> None:
        target.setChecked(box.isChecked())
        target.toggled.connect(box.setChecked)
        box.toggled.connect(target.setChecked)

    for key in ("pinned", "timeline", "grid"):
        box = boxes.get(key)
        if box is None:
            continue
        toggle = QToolButton()
        toggle.setObjectName(f"toggle_{key}")
        toggle.setProperty("iconToggle", True)
        toggle.setCheckable(True)
        toggle.setAutoRaise(True)
        toggle.setText(box.text())
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toggle.setToolTip(f"{box.text()}\n\n{box.toolTip()}")
        toggle.setAccessibleName(box.text())
        toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        toggle.icon_name = icons[key]
        bind(box, toggle)
        row.addWidget(toggle)
        made[key] = toggle

    # The preview pane is a view preference (`view_options`); this toggle
    # only calls the same `toggle_preview` Ctrl+Shift+P calls.
    inspector = QToolButton()
    inspector.setObjectName("toggle_inspector")
    inspector.setProperty("iconToggle", True)
    inspector.setCheckable(True)
    inspector.setAutoRaise(True)
    inspector.setText("Preview")
    inspector.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    inspector.setToolTip("Show or hide the preview pane beside the results  Ctrl+Shift+P")
    inspector.setAccessibleName("Preview pane")
    inspector.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    inspector.icon_name = "panel-right"
    prefs = getattr(getattr(view, "view_button", None), "prefs", None)
    inspector.setChecked(bool(getattr(prefs, "preview", False)))
    toggle_preview = getattr(getattr(view, "view_button", None), "toggle_preview", None)
    if callable(toggle_preview):
        inspector.clicked.connect(lambda _c=False: toggle_preview())
    changed = getattr(view, "view_preferences_changed", None)
    if changed is not None:
        changed.connect(lambda p: _set_silently(inspector, bool(getattr(p, "preview", False))))
    row.addWidget(inspector)
    made["inspector"] = inspector

    menu = getattr(view, "more_menu", None)
    if menu is not None:
        menu.addSeparator()
        for key in ("drag", "drawings"):
            box = boxes.get(key)
            if box is None:
                continue
            action = QAction(box.text(), view)
            action.setCheckable(True)
            action.setToolTip(box.toolTip())
            bind(box, action)
            menu.addAction(action)
            made[key] = action
    return made


def _set_silently(button: Any, checked: bool) -> None:
    if button.isChecked() != checked:
        button.blockSignals(True)
        button.setChecked(checked)
        button.blockSignals(False)


def retint_toolbar(view: Any, colours: dict) -> None:
    """Re-render the toolbar's icons for a palette (called by the window)."""
    from app.ui.widgets.icons import icon

    dim, text = colours.get("text_dim", "#888888"), colours.get("accent_text", "#ffffff")
    for toggle in getattr(view, "toggles", {}).values():
        name = getattr(toggle, "icon_name", None)
        if name and hasattr(toggle, "setIcon"):
            toggle.setIcon(icon(name, text if toggle.isChecked() else dim))
    more = getattr(view, "more_button", None)
    if more is not None:
        more.setIcon(icon("ellipsis", dim))
    button = getattr(view, "view_button", None)
    if button is not None and hasattr(button, "setIcon"):
        button.setIcon(icon("sliders-horizontal", dim))
    for action, name in ((getattr(view, "interpret_button", None), "sparkles"),
                         (getattr(view, "rerank_toggle", None), "refresh-cw")):
        if action is not None:
            action.setIcon(icon(name, dim))


def build_controls(view: Any, *, on_scope: Any, on_interpret: Any,
                   on_rerank: Any, on_view: Any) -> tuple:
    r"""The scope, the `⋯` menu's actions, the View button and the status line.

    Returns `(scope, interpret, rerank, view_button, status)`. Since the UI
    Redesign (202626160950 §3c/§3e) `scope` is a `SegmentedControl`, and
    `interpret` and `rerank` are `QAction`s in the `⋯` menu the toolbar draws;
    every name and signal the window used is still there.

    **The keyboard shortcuts are built here too**, deliberately: `Ctrl+Enter` is
    the Interpret button by another name, and a shortcut that lives apart from
    the control it duplicates is how the two come to disagree about whether the
    feature is switched on.

    The view button carries the results pane's own text size and spacing.
    Results are the one place in this window people *read* rather than scan, and
    the size that suits a paragraph of snippet is not the size that suits a
    toolbar - so it is this pane's setting rather than an application-wide zoom.
    No columns: a result is not a table.
    """
    from PyQt6.QtGui import QKeySequence, QShortcut
    from PyQt6.QtWidgets import QLabel

    from app.ui.view_options import button as view_button

    scope = build_scope(view, on_scope)
    interpret = build_interpret(view, lambda _checked=False: on_interpret())
    rerank = build_rerank(view, lambda _state: on_rerank())
    # **Gated on the button being visible.** Interpret is hidden when Ollama is
    # switched off or absent - `set_interpret_enabled` - and the shortcut was
    # not, so Ctrl+Enter on a machine with no Ollama started a translation that
    # could only fail, from a feature the person had switched off or never had.
    # `interpret.isVisible()` is the same condition the button is drawn by, so
    # the two cannot drift apart.
    # **And disabled, not just inert, while it is hidden**: a shortcut that
    # fires and does nothing still consumes the key, and Ctrl+Enter is also
    # "reveal this result" in the box (`SearchView.eventFilter`, item 6a),
    # which therefore never worked on a machine without Interpret.
    chords = []
    for keys in ("Ctrl+Return", "Ctrl+Enter"):
        chords.append(QShortcut(
            QKeySequence(keys), view,
            activated=lambda: on_interpret() if interpret.isVisible() else None))

    def _follow_interpret() -> None:
        for chord in chords:
            chord.setEnabled(interpret.isVisible())

    interpret.changed.connect(_follow_interpret)
    _follow_interpret()

    chooser = view_button(view, None, "", on_change=on_view, grouping=True)
    status = QLabel("")
    status.setObjectName("searchStatus")
    # The `⋯` menu (§3e). Interpret and Rerank live here; `build_toolbar`
    # adds the two switches that used to be checkboxes on the results pane.
    view.more_button, view.more_menu = _build_more(view, interpret, rerank)
    return scope, interpret, rerank, chooser, status


def _build_more(view: Any, interpret: QAction, rerank: QAction) -> tuple:
    """A QToolButton opening the menu that holds the less-used controls."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QMenu, QToolButton

    menu = QMenu(view)
    menu.addAction(interpret)
    menu.addAction(rerank)
    button = QToolButton(view)
    button.setObjectName("moreButton")
    button.setProperty("iconToggle", True)
    button.setText("More")
    button.setToolTip("More search options: Interpret, Rerank, dragging results out")
    button.setAccessibleName("More search options")
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    button.setMenu(menu)
    return button, menu
