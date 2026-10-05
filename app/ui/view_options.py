"""What a list shows, how tightly, and how big.

Layer: L5 (the decisions; no Qt in the top half)

Three settings that belong together because they answer one question - *how much
do I want on screen at once* - and because all three are per-list preferences
rather than application-wide ones. Somebody scanning a mailbox for an attachment
wants six columns and compact rows; the same person reading search results wants
large text and room to breathe.

**Columns are offered only when the data can fill them.** A "To" column over a
mailbox where no message has recipients is a column of blanks that pushes the
useful ones off the screen, and it invites the reasonable conclusion that the
index is broken. `available_columns` therefore takes the rows and reports which
columns have something in them, so the chooser offers a real choice rather than
a menu of disappointments.

**Nothing here is destructive.** Hiding a column hides a column; the query is
unchanged, the data is unchanged, and turning it back on costs one click. That
is the difference between a view option and a filter, and it is why these live
in `index_state` rather than in `.env` - they are how one person likes to look
at their own screen, not configuration anybody needs to maintain.

The bottom of the file has the Qt parts. Everything above it is plain data, so
the rules can be tested without a display.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.core.logging import logger

_log = logger.bind(component="ui.view")

__all__ = [
    "load_prefs", "save_prefs", "build_menu", "apply_to_table", "apply_to_tree",
    "button", "save_prefs_later", "preview_toggle", "add_view_controls",
    "retint_toggles", "PREVIEW_TOGGLE_TIP",
    "Density", "ViewPreferences", "DEFAULT_FONT_PT", "FONT_RANGE",
    "available_columns", "visible_columns", "row_height_for", "parse_prefs",
    "prefs_to_state", "DENSITIES", "Metrics", "remember_widths",
    "column_cap", "MAX_COLUMN_SHARE", "MIN_COLUMN_CAP_PX", "weak_slot",
]


class Density:
    """How much vertical room a row gets."""

    COMPACT = "compact"
    NORMAL = "normal"


#: (value, label, row-height multiplier). The multiplier is applied to the font
#: metrics rather than being a fixed pixel count, so compact stays legible when
#: the font is turned up and does not clip descenders at 16pt.
DENSITIES: tuple[tuple[str, str, float], ...] = (
    (Density.COMPACT, "Compact", 1.35),
    (Density.NORMAL, "Normal", 1.85),
)

#: Point size for list and result text. Not the whole application: the buttons
#: and labels follow the operating system, and enlarging those too turns a
#: preference about reading density into a broken layout.
DEFAULT_FONT_PT = 0          # 0 means "whatever the system font is"
FONT_RANGE = (7, 20)


@dataclass(frozen=True, slots=True)
class ViewPreferences:
    """One list's preferences. Frozen, so a stale copy cannot drift."""

    #: Column keys to show, in order. Empty means "the defaults for this list" -
    #: distinct from "show nothing", which nobody wants and which would look
    #: exactly like a bug.
    columns: tuple[str, ...] = ()
    density: str = Density.NORMAL
    #: 0 to follow the system font. An explicit number overrides it.
    font_pt: int = DEFAULT_FONT_PT
    #: One row per document rather than one per matching chunk.
    #:
    #: On by default, because chunk-level rows are the complaint this answers -
    #: a long PDF matching in five places took five of the top ten rows. Off is
    #: still offered: somebody comparing two passages of the same document wants
    #: to see them side by side, and taking that away would be a different
    #: complaint.
    group_by_document: bool = True
    #: Show the match explanation and score inline on every row.
    #:
    #: Off by default and moved to the tooltip. "Why is this here" is where
    #: trust comes from and must stay reachable - but it does not need to be the
    #: second thing the eye lands on, on every row, forever.
    show_scores: bool = False
    #: The preview pane, beside the results.
    #:
    #: **Off by default**, and not out of caution about the widget: a pane that
    #: appears uninvited halves the width of the list somebody came to read, and
    #: it reads a file for every row the selection touches. Somebody who wants
    #: it asks for it, with `Ctrl+P` or the View menu.
    preview: bool = False
    #: One row per conversation rather than one per message. Order 0z F2.
    #:
    #: **Off by default.** The Mail list is a table people scan and sort, one
    #: message a row, and the Search list already folds a document's passages;
    #: folding replies together is a second thing to ask for, from the View
    #: menu. Nothing is hidden by it: the summary still counts messages, and a
    #: folded row says how many it stands for.
    group_by_conversation: bool = False
    #: `{column key: pixels}` for columns somebody has dragged.
    #:
    #: **Empty means "fit to the contents", which is the starting state.** A
    #: table that opens with every column the same arbitrary width truncates
    #: the one you came to read and leaves an ocean beside the one you did not,
    #: and Qt's default does exactly that. Fitting once, on the first fill, is
    #: what makes the list readable without anybody touching it - and a width
    #: that has been dragged is a decision, so it survives a restart.
    #:
    #: Only dragged columns are stored, so a column added in a later version
    #: fits itself rather than inheriting a width nobody chose for it.
    widths: tuple[tuple[str, int], ...] = ()

    def with_width(self, key: str, pixels: int) -> "ViewPreferences":
        """One column's width, replacing any previous value for it."""
        kept = [(name, size) for name, size in self.widths if name != key]
        if pixels > 0:
            kept.append((key, int(pixels)))
        return replace(self, widths=tuple(sorted(kept)))

    def with_column(self, key: str, shown: bool, *, order: Sequence[str]) -> "ViewPreferences":
        """Turn one column on or off, keeping the canonical column order.

        Order comes from the list's own definition rather than from click
        history: a table whose columns reshuffle themselves as you toggle them
        is disorienting, and the order was chosen to be scanned.
        """
        current = set(self.columns or order)
        if shown:
            current.add(key)
        else:
            current.discard(key)
        # Never all-off. A table with no columns is indistinguishable from a
        # broken one, and there is no way back from it through the same menu.
        if not current:
            current = {order[0]}
        return replace(
            self, columns=tuple(key for key in order if key in current))


@dataclass(frozen=True, slots=True)
class Metrics:
    """Every number the layout uses. One source, so measuring and painting agree."""

    pad_x: int = 10
    pad_y: int = 6
    gap: int = 3
    #: How far a chunk row is indented under its document.
    indent: int = 26
    #: Points added to the base font for the name.
    #:
    #: **0, not 2.** The name used to render two points larger than everything
    #: else on the page, on every density - `for_density` only ever touched
    #: padding, never this - so Results was the one list in the app whose text
    #: did not match its own "system font size" preference. Files and Mail
    #: draw every cell at exactly the applied font with no per-field bump, and
    #: a person moving between the three lists felt Results as "too big" for
    #: exactly that reason. The name keeps its bold weight below - that alone
    #: is enough hierarchy without also changing size.
    name_bump: int = 0
    #: Points removed for the grey lines. **0, to match**: the meta line was a
    #: point smaller than the same-page body text for the same reason the name
    #: was two points larger - an old fixed delta `for_density` never revisited.
    meta_drop: int = 0

    @classmethod
    def for_density(cls, density: str) -> "Metrics":
        if density == Density.COMPACT:
            # Padding shrinks, text does not. The point of a compact list is
            # more rows on screen; text you cannot read is not more information.
            return cls(pad_y=2, gap=1)
        return cls()


def available_columns(
    rows: Iterable[Any], columns: Sequence[tuple[str, str]], *, always: Sequence[str] = (),
    kept: Optional[set] = None,
) -> tuple[str, ...]:
    """Which columns have data in them, given the rows on screen.

    `columns` is `(key, attribute)` pairs. A column is available when at least
    one row has a non-empty value for it.

    **Why not just offer all of them?** Because a column of blanks is worse than
    a missing column: it takes width from the ones that matter and it reads as a
    failure of the index rather than an absence in the data. Mail extracted
    without recipients is a real case - some PST exports carry no To line at all.

    `always` names columns that stay on offer whatever the rows say, for the
    ones a list would be nonsense without.

    *Added 4 October 2026, the owner: "for any search the columns should
    remain same".* `kept` is the list's own set of every column it has shown;
    a column in it stays, and one found now is added to it. Without it a
    search whose rows happen to be empty in a column took the column away -
    `from:` himself, mostly "Accepted:" replies with no To line, lost **To**.
    A mailbox with no To line anywhere still never shows the column.
    """
    materialised = list(rows)
    found: list[str] = []
    for key, attribute in columns:
        if key in always or (kept is not None and key in kept):
            found.append(key)
            continue
        for row in materialised:
            value = getattr(row, attribute, None) if not isinstance(row, Mapping) else row.get(attribute)
            if value not in (None, "", 0, False):
                found.append(key)
                break
    if kept is not None:
        kept.update(found)
    return tuple(found)


def visible_columns(
    prefs: ViewPreferences, order: Sequence[str], available: Sequence[str]
) -> tuple[str, ...]:
    """The columns to actually draw: chosen, minus unavailable, in canonical order.

    An empty preference means the defaults, which is every available column.
    A preference naming a column that has since become unavailable keeps it in
    the *preference* and drops it from the *drawing* - so indexing a mailbox
    that does have recipients brings the "To" column back on its own, rather
    than requiring somebody to remember they once turned it off.
    """
    chosen = set(prefs.columns) if prefs.columns else set(available)
    visible = tuple(key for key in order if key in chosen and key in available)
    # Never nothing. If a preference has been outlived entirely by the data,
    # showing the available columns beats showing an empty table.
    return visible or tuple(key for key in order if key in available)


def row_height_for(density: str, font_height_px: int) -> int:
    """Row height in pixels, from the font's own metrics.

    Derived rather than fixed so that turning the font up does not clip
    descenders, and turning it down does not leave rows floating in space.
    """
    multiplier = next(
        (value for name, _label, value in DENSITIES if name == density),
        DENSITIES[-1][2],
    )
    return max(14, int(round(max(1, font_height_px) * multiplier)))


def parse_prefs(state: Mapping[str, str], prefix: str) -> ViewPreferences:
    """Read one list's preferences out of `index_state`. Never raises.

    A malformed value means "the default", not an exception: these are read
    while the window is being built, and a bad row in a settings table must not
    be the reason the application will not open.
    """
    raw_columns = str(state.get(f"{prefix}:columns", "") or "")
    columns = tuple(part for part in (piece.strip() for piece in raw_columns.split(",")) if part)

    density = str(state.get(f"{prefix}:density", "") or Density.NORMAL)
    if density not in {name for name, _label, _mult in DENSITIES}:
        density = Density.NORMAL

    try:
        font_pt = int(state.get(f"{prefix}:font_pt", "") or 0)
    except (TypeError, ValueError):
        font_pt = DEFAULT_FONT_PT
    if font_pt and not (FONT_RANGE[0] <= font_pt <= FONT_RANGE[1]):
        font_pt = DEFAULT_FONT_PT

    def flag(key: str, default: bool) -> bool:
        raw = str(state.get(f"{prefix}:{key}", "") or "").strip().lower()
        if raw in ("on", "true", "1", "yes"):
            return True
        if raw in ("off", "false", "0", "no"):
            return False
        return default

    widths: list[tuple[str, int]] = []
    for piece in str(state.get(f"{prefix}:widths", "") or "").split(","):
        key, _sep, value = piece.partition("=")
        try:
            pixels = int(value)
        except (TypeError, ValueError):
            continue                    # a bad row is a default, not an error
        if key.strip() and pixels > 0:
            widths.append((key.strip(), pixels))

    return ViewPreferences(
        columns=columns, density=density, font_pt=font_pt,
        group_by_document=flag("group", True),
        show_scores=flag("scores", False),
        preview=flag("preview", False),
        group_by_conversation=flag("conversations", False),
        widths=tuple(sorted(widths)),
    )


def prefs_to_state(prefs: ViewPreferences, prefix: str) -> dict[str, str]:
    """The inverse of `parse_prefs`, for one batched write."""
    return {
        f"{prefix}:columns": ",".join(prefs.columns),
        f"{prefix}:density": prefs.density,
        f"{prefix}:font_pt": str(int(prefs.font_pt)),
        f"{prefix}:group": "on" if prefs.group_by_document else "off",
        f"{prefix}:scores": "on" if prefs.show_scores else "off",
        f"{prefix}:preview": "on" if prefs.preview else "off",
        f"{prefix}:conversations": "on" if prefs.group_by_conversation else "off",
        f"{prefix}:widths": ",".join(f"{key}={int(size)}"
                                     for key, size in prefs.widths),
    }


def load_prefs(store: Any, prefix: str) -> ViewPreferences:
    """Read from the store, defaulting on any failure.

    This runs while the window is being built. A locked or missing database is
    a reason to open with the default layout, never a reason not to open.
    """
    try:
        return parse_prefs(store.all_state(), prefix)
    except Exception:                                # noqa: BLE001
        return ViewPreferences()


def save_prefs(store: Any, prefix: str, prefs: ViewPreferences) -> bool:
    """Persist in one transaction. False if it could not be written.

    Three keys, one commit - see `set_states`. Failing to save a column
    preference must not interrupt what somebody was doing, so this reports
    rather than raises: the change is already on screen either way.
    """
    try:
        store.set_states(prefs_to_state(prefs, prefix))
        return True
    except Exception as exc:                         # noqa: BLE001
        # Logged, because this now runs on the state writer's thread (see
        # `save_prefs_later`) where nobody reads the False.
        _log.warning("view preferences for {} not saved: {}", prefix, exc)
        return False


# ---------------------------------------------------------------------------
# The Qt half: a menu, and applying the result to a table
# ---------------------------------------------------------------------------

def save_prefs_later(store: Any, prefix: str, prefs: ViewPreferences) -> None:
    """`save_prefs`, queued on the ordered state writer. **Never blocks.**

    Bug 3a: a column dragged or a density picked during an index run waited
    for the indexer's write transaction, because `set_states` takes the same
    process-wide write lock as every index batch. `remember_width` fires on
    every pixel of a drag, so that was a frozen drag, not a frozen click.

    Built as `CallableWorker(save_prefs, ...)` here rather than inside
    `state_writes`, so `test_ui_never_blocks` can see structurally that
    `save_prefs` is a worker body.
    """
    from app.ui.state_writes import start
    from app.ui.workers import CallableWorker

    if store is None:
        return
    start(CallableWorker(save_prefs, store, prefix, prefs, component="ui.view"))


def build_menu(
    parent: Any,
    prefs: ViewPreferences,
    *,
    columns: Sequence[tuple[str, str]],
    available: Sequence[str],
    on_change: Any,
    grouping: bool = False,
    on_fit: Any = None,
    conversations: bool = False,
) -> Any:
    """The "View" menu: which columns, how tight, how big.

    `columns` is `(key, heading)` in canonical order. `on_change` takes the new
    `ViewPreferences` - the caller persists and redraws, because this module
    knows nothing about stores or tables.

    `on_fit` is "somebody asked for the columns to be fitted again". It takes no
    arguments and is separate from `on_change` because fitting is not a change
    to the preferences alone: it also has to reset the table's own `FITTED`
    flag, and only the caller can reach the table.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction, QActionGroup
    from PySide6.QtWidgets import QMenu, QWidgetAction, QSpinBox, QLabel, QWidget, QHBoxLayout

    order = [key for key, _heading in columns]
    shown = set(visible_columns(prefs, order, available))
    menu = QMenu("View", parent)
    # **Deleted when it closes.** Parented to the View button so it is styled
    # and positioned correctly, which also means Qt keeps it alive for the
    # lifetime of the *button* - and one is built per click. A session of
    # fiddling with columns left a menu per open, each holding its actions, its
    # spin box and its labels. `WA_DeleteOnClose` gives it the lifetime it
    # actually has.
    menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    menu.addSection("Columns")
    for key, heading in columns:
        action = QAction(heading, menu)
        action.setCheckable(True)
        action.setChecked(key in shown)
        if key not in available:
            # Offered but disabled, with the reason. Silently omitting it
            # leaves somebody hunting a menu for a column that is not there.
            action.setEnabled(False)
            action.setToolTip("Nothing in the current results has this")
        action.toggled.connect(
            lambda checked, k=key: on_change(prefs.with_column(k, checked, order=order))
        )
        menu.addAction(action)

    menu.addSection("Rows")
    group = QActionGroup(menu)
    group.setExclusive(True)
    for name, label, _multiplier in DENSITIES:
        action = QAction(label, menu)
        action.setCheckable(True)
        action.setChecked(prefs.density == name)
        group.addAction(action)
        # `replace`, never the constructor. Building `ViewPreferences(columns,
        # density, font_pt)` positionally silently drops every field after the
        # third - so changing the row height turned grouping back on and threw
        # away "show why each result matched". A dataclass gaining a field is
        # routine; a constructor call that enumerates fields by position turns
        # that into a silent reset somewhere else in the file.
        action.triggered.connect(
            lambda _checked, n=name: on_change(replace(prefs, density=n))
        )
        menu.addAction(action)

    if grouping:
        # Only where grouping means anything. A table of files has one row per
        # file already, and offering to "group" it would be offering nothing.
        menu.addSection("Results")
        group = QAction("One row per document", menu)
        group.setCheckable(True)
        group.setChecked(prefs.group_by_document)
        group.setToolTip(
            "A long document matching in five places takes five rows without this."
        )
        group.toggled.connect(
            lambda checked: on_change(replace(prefs, group_by_document=checked)))
        menu.addAction(group)

        scores = QAction("Show why each result matched", menu)
        scores.setCheckable(True)
        scores.setChecked(prefs.show_scores)
        scores.setToolTip(
            "The match reason and score, on every row.\n"
            "They are always in the tooltip and the right-click menu."
        )
        scores.toggled.connect(
            lambda checked: on_change(replace(prefs, show_scores=checked)))
        menu.addAction(scores)

    if grouping or conversations:
        # Order 0z F2. Wherever mail is listed: Search (beside "One row per
        # document") and Mail. A list of files has no conversations to fold.
        if not grouping:
            menu.addSection("Results")
        threads = QAction("One row per conversation", menu)
        threads.setCheckable(True)
        threads.setChecked(prefs.group_by_conversation)
        threads.setToolTip(
            "Replies are folded under one message of their conversation.\n"
            "The row says how many it stands for, and the count under the "
            "list still counts every message."
        )

        def fold(checked: bool) -> None:
            chosen = replace(prefs, group_by_conversation=checked)
            # Somebody who has picked their columns has a list of them, and a
            # column added since is not on it - so folding would hide the one
            # column that says how many messages a row stands for.
            if checked and chosen.columns and "messages" in order:
                chosen = chosen.with_column("messages", True, order=order)
            on_change(chosen)

        threads.toggled.connect(fold)
        menu.addAction(threads)

    # **Outside `if grouping:`, and it should never have been inside it.**
    #
    # "One row per document" and "Show why each result matched" are about
    # ranked search results, so they belong to the grouping branch. The preview
    # pane is about reading the selected row and applies to every list there is
    # - but it sat in the same block, so Files, Mail and Code offered no way to
    # switch it on. Each of those views builds a preview pane and wires it up;
    # the only thing missing was the menu entry that reveals it.
    #
    # Reported as: "the option in the view menu is only on the main search tab,
    # it should be on all tabs."
    menu.addSection("Reading")
    preview = QAction("Preview pane", menu)
    preview.setCheckable(True)
    preview.setChecked(prefs.preview)
    # **Not `Ctrl+P`, which this said for weeks and never did.** The window
    # binds `Ctrl+P` to "go to Files" - the shortcut every editor uses for
    # "go to file" - and a window-level action wins over one on a menu that
    # only exists while it is open. So the menu advertised a key that did
    # nothing, which is worse than advertising none: somebody presses it,
    # lands on another tab, and concludes the preview is broken.
    preview.setShortcut("Ctrl+Shift+P")
    preview.setToolTip(
        "Read the selected result beside the list, without opening the "
        "application that owns it."
    )
    preview.toggled.connect(
        lambda checked: on_change(replace(prefs, preview=checked)))
    menu.addAction(preview)

    if columns:
        # **A way back from a column somebody dragged too narrow.** Widths are
        # remembered, which is what makes dragging worth doing and also what
        # makes a mistake permanent - a column pulled to nothing stays at
        # nothing across restarts, and the handle to pull it back out is one
        # pixel wide. Clearing them restores the fitted layout.
        fit = QAction("Fit columns to contents", menu)
        fit.setToolTip(
            "Sizes every column to what is in it, and forgets any width you "
            "have dragged.\n\nColumns fit themselves until you drag one; "
            "after that yours is kept.")
        # **`on_fit`, not `on_change`.** Clearing the widths is only half of
        # what this item promises; the other half is re-measuring, and that
        # needs the `FITTED` flag on the table cleared. The table is not
        # reachable from here, so the caller supplies a function that can do
        # both - see `button.refit`. Falling back to `on_change` keeps a menu
        # built without one working exactly as it did.
        fit.triggered.connect(
            lambda _checked=False: (on_fit or (
                lambda: on_change(replace(prefs, widths=()))))())
        menu.addAction(fit)

    menu.addSection("Text size")
    box = QWidget(menu)
    row = QHBoxLayout(box)
    row.setContentsMargins(12, 2, 12, 2)
    row.addWidget(QLabel("Size"))
    spin = QSpinBox(box)
    spin.setRange(FONT_RANGE[0] - 1, FONT_RANGE[1])
    spin.setSpecialValueText("System")      # the range's minimum means "follow the OS"
    spin.setValue(prefs.font_pt or FONT_RANGE[0] - 1)
    spin.setToolTip(
        "Text size for this list only. Buttons and labels follow Windows, so "
        "turning this up makes results easier to read without breaking the "
        "layout around them. 'System' follows the operating system.")
    spin.setKeyboardTracking(False)
    spin.valueChanged.connect(
        lambda value: on_change(replace(
            prefs, font_pt=0 if value < FONT_RANGE[0] else value,
        ))
    )
    # No arrows, and a back-to-default button (owner, 2026-09-29). The default
    # is "System", the range's minimum - not the size this menu opened on.
    from app.ui.widgets.number_field import fit
    fit(spin, default=FONT_RANGE[0] - 1)
    row.addWidget(spin)
    holder = QWidgetAction(menu)
    holder.setDefaultWidget(box)
    menu.addAction(holder)

    return menu


def _weakly(callback: Any) -> Any:
    """`callback`, without keeping its owner alive. **A bound method only.**

    The View button lives as long as its view and its closures used to hold the
    view's own `_prefs_changed`, which made a cycle - view, button, closure,
    view. A view somebody had let go of then stayed alive until the cyclic
    garbage collector found it, with its 600ms width watcher still ticking on
    its tables, and the collector deletes the C++ widget wherever an allocation
    happens to trigger it: mid event-loop turn, in the middle of some other
    widget's timer. One native crash between two tests was that. Held weakly,
    a view that is let go is deleted at once, by reference count, with its
    timers, at a point that is not inside anything else.

    A plain function (or a lambda, which is what a test passes) is returned as
    it is: nothing owns those but the caller, so there is no cycle to break.
    """
    import inspect
    import weakref

    if callback is None or not inspect.ismethod(callback):
        return callback
    ref = weakref.WeakMethod(callback)

    def call(*args: Any) -> None:
        method = ref()
        if method is not None:
            method(*args)

    return call


def weak_slot(owner: Any, function: Any) -> Any:
    """`function(owner, *args)` as a callback that does not keep `owner` alive.

    For the places a view hands one of its own children something to call
    back - a signal's slot, a provider - that is not simply one of its methods.
    Written as `lambda: self.something()`, that callback closes over `self`,
    the child holds the callback and the view holds the child: a cycle, so the
    view outlives its last reference until the cyclic collector gets to it.
    And while the width watcher's registry (`_WATCHERS`) could reach such a
    callback through the table's header, the collector never got to it at all.

        header.customContextMenuRequested.connect(
            weak_slot(self, lambda view, point: view.view_button.show_menu(...)))

    **The function takes the owner as its first argument and must not mention
    `self`**, or the cycle is back; `test_views_are_freed.py` fails if one does.
    A method whose arguments already fit needs none of this: `connect(self.x)`
    does not keep `self` alive. Once the owner has gone the callback does
    nothing and returns None - by then whatever would call it is going too.
    """
    import weakref

    ref = weakref.ref(owner)

    def call(*args: Any) -> Any:
        alive = ref()
        return function(alive, *args) if alive is not None else None

    return call


def button(
    parent: Any,
    store: Any,
    prefix: str,
    *,
    columns: Sequence[tuple[str, str]] = (),
    on_change: Any = None,
    grouping: bool = False,
    table: Any = None,
    conversations: bool = False,
) -> Any:
    """A "View" button that owns its own preferences, menu and persistence.

    Three views needed the same four things - load, show a menu, apply, save -
    and writing that out in each one pushed two of them past the length a view
    is allowed to be. The length guard was right: it is the same logic three
    times, and it belongs here beside the rules it applies.

    The returned button carries `prefs` and `available`; setting `available`
    from fresh rows and calling `refresh()` is how a view keeps the column list
    honest as the data changes. `on_change` is called after each change with
    the new preferences, for the view to redraw itself.
    """
    from PySide6.QtWidgets import QToolButton

    import weakref

    on_change = _weakly(on_change)
    #: **Weakly**: the table's width watcher refers to this button, so a strong
    #: reference back made button -> table -> watcher -> button a cycle (and,
    #: through `_WATCHERS`, a path from the registry to everything the table's
    #: slots close over). The table is the view's; it outlives this button.
    table_ref = weakref.ref(table) if table is not None else None
    widget = QToolButton(parent)
    widget.setText("View")
    widget.setToolTip(
        "Which columns, how tightly packed, and how big" if columns
        else "Text size and row spacing"
    )
    widget.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    _as_icon(widget)
    widget.prefs = load_prefs(store, prefix) if store is not None else ViewPreferences()
    widget.available = tuple(key for key, _heading in columns)
    watchers: list = []

    def changed(prefs: ViewPreferences) -> None:
        widget.prefs = prefs
        if store is not None:
            save_prefs_later(store, prefix, prefs)
        if on_change is not None:
            on_change(prefs)
        for watcher in list(watchers):
            try:
                watcher(prefs)
            except RuntimeError:                 # a mirror whose C++ side has gone
                watchers.remove(watcher)

    def refit() -> None:
        """Forget every dragged width and measure the columns again.

        **Both halves, which is what the menu item always promised.** Clearing
        the preference alone left `_apply_widths` with nothing to do: its
        re-measure is gated on the table's `FITTED` flag, so the columns stayed
        exactly as dragged while the setting that produced them disappeared -
        the preference and the screen disagreeing, which is worse than either.
        """
        fitted = table_ref() if table_ref is not None else None
        if fitted is not None:
            try:
                fitted.setProperty(FITTED, False)
            except RuntimeError:                 # the C++ side has gone
                pass
        changed(replace(widget.prefs, widths=()))

    def menu_for(parent: Any) -> Any:
        """This tab's View menu, built as a click builds it - for the window's
        own View menu too (2026-10-04), so the two can never say different
        things."""
        return build_menu(
            parent, widget.prefs, columns=columns,
            available=widget.available, on_change=changed, grouping=grouping,
            on_fit=refit, conversations=conversations,
        )

    def show(at: Any = None) -> None:
        menu = menu_for(widget)
        menu.exec(at or widget.mapToGlobal(widget.rect().bottomLeft()))

    def toggle_preview() -> None:
        """Flip the preview pane, saving and redrawing as a menu click would.

        Here rather than in the window because `changed` is what saves and
        redraws, and it is a closure over this button. A shortcut that set
        `prefs` directly would show the pane and forget it by the next launch.
        """
        changed(replace(widget.prefs, preview=not widget.prefs.preview))

    def remember_width(key: str, pixels: int) -> None:
        """Record a column somebody dragged. **Saved, not redrawn.**

        Deliberately not `changed()`: that persists *and* calls `on_change`,
        which redraws the table - and a redraw re-applies the widths, which is
        both a full re-fit on every pixel of a drag and the recursion that
        crashed the process. The column is already the width they dragged it
        to; there is nothing on screen to update.
        """
        widget.prefs = widget.prefs.with_width(key, pixels)
        if store is not None:
            save_prefs_later(store, prefix, widget.prefs)

    widget.show_menu = show
    widget.menu_for = menu_for
    widget.toggle_preview = toggle_preview
    widget.remember_width = remember_width
    widget.refit = refit
    # 2026-10-04: anything that mirrors the preferences on screen (the
    # Preview icon toggle, `preview_toggle`) is told after each change; and
    # `add_to(layout)` puts that toggle and this button on a tab's top row -
    # a method rather than an import, because the three views that call it
    # are at their line limit and an import line would put one over.
    widget.watchers = watchers
    # Weakly, as `on_change` is held: the button is the view's child, and a
    # closure here that held the view would be the cycle `_weakly` describes
    # (`test_views_are_freed.py` found it on the first attempt).
    parent_ref = weakref.ref(parent) if parent is not None else (lambda: None)
    widget.add_to = lambda layout: add_view_controls(layout, parent_ref())
    # **The button owns the preferences, so it owns the wiring that writes
    # them.** Passing the table here rather than making every view call
    # `remember_widths` itself is what keeps this one line instead of four in
    # each of them - and `mail_view.py` was one line over the length guard,
    # which is the guard doing its job.
    if table is not None and columns:
        remember_widths(table, widget, columns)
    widget.clicked.connect(lambda: show())
    return widget


# ---------------------------------------------------------------------------
# The Preview icon toggle, the same on every tab (2026-10-04)
# ---------------------------------------------------------------------------
#
# The owner: "there are icons in the search page for preview etc .. why are
# they not in the other tabs? i.e consistent". The Search bar's icon toggles
# came with its redesign (`widgets/search_bar.py`, order 202626160950 §3e)
# and Files, Mail and Code had the same preference one click further away,
# inside the View menu. This is that toggle, made once for every tab that
# has a preview. Pinned, Timeline and Grid stay Search's: the panels they
# show exist nowhere else, and a toggle that does nothing is forbidden.

#: 2026-10-04, the owner: "the view in each tab should be in the view in the
#: menu and should be dynamic ... if the view has to stay on each tab it
#: should be a icon similar to preview consistent across all". It stays - a
#: right-click on a column heading opens it - so it is an icon, drawn and
#: retinted exactly as the Preview toggle is, and the window's View menu shows
#: the same options for whichever tab is in front (`MainWindow._fill_view_menu`).
VIEW_ICON = "sliders-horizontal"


def _as_icon(widget: Any) -> None:
    """Make a tab's View button an icon beside Preview's, the same size and
    the same colours. Its text stays "View", which is what a screen reader and
    every test that finds it by name read."""
    from PySide6.QtCore import Qt

    from app.ui.widgets.icons import icon

    widget.setProperty("iconToggle", True)
    widget.setAutoRaise(True)
    widget.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    widget.setAccessibleName("View options")
    widget.icon_name = VIEW_ICON

    def paint(colours: Any = None) -> None:
        if colours is None:
            from app.ui.theme import theme_colours

            colours = theme_colours()
        widget.setIcon(icon(VIEW_ICON, colours.get("text_dim", "#888888")))

    widget.retint = paint
    paint()


#: What the toggle says. The tooltip names the shortcut the window binds.
PREVIEW_TOGGLE_TIP = "Show or hide the preview pane beside the results  Ctrl+Shift+P"


def preview_toggle(view: Any, *, checked: Any = None, on_toggle: Any = None) -> Any:
    """A checkable icon button that shows or hides a tab's preview pane.

    With a `view.view_button` (Files, Mail, Code, Search) it reads and flips
    that button's preference, so the pane is remembered exactly as the View
    menu remembers it, and it follows a change made from the menu or the
    shortcut (`watchers`). A tab with no preferences (Chat) passes `checked`
    and `on_toggle(on)` instead. Paints its own icon, and repaints it when
    it is flipped and when the window changes theme (`retint_toggles`).
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolButton

    from app.ui.widgets.icons import icon

    # A bound method of the view (Chat's `show_preview`) is held weakly, as
    # `button()` holds `on_change`: the toggle is the view's child, and a
    # closure on it that held the view would keep the view alive.
    on_toggle = _weakly(on_toggle)
    toggle = QToolButton()
    toggle.setObjectName("toggle_inspector")
    toggle.setProperty("iconToggle", True)
    toggle.setCheckable(True)
    toggle.setAutoRaise(True)
    toggle.setText("Preview")
    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    toggle.setToolTip(PREVIEW_TOGGLE_TIP)
    toggle.setAccessibleName("Preview pane")
    toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    toggle.icon_name = "panel-right"

    def paint(colours: Any = None) -> None:
        if colours is None:
            from app.ui.theme import theme_colours

            colours = theme_colours()
        dim = colours.get("text_dim", "#888888")
        text = colours.get("accent_text", "#ffffff")
        toggle.setIcon(icon("panel-right", text if toggle.isChecked() else dim))

    def quietly(on: bool) -> None:
        if toggle.isChecked() != bool(on):
            toggle.blockSignals(True)
            toggle.setChecked(bool(on))
            toggle.blockSignals(False)
        paint()

    button = getattr(view, "view_button", None)
    if on_toggle is None and button is not None:
        toggle.setChecked(bool(getattr(getattr(button, "prefs", None), "preview", False)))
        toggle.clicked.connect(lambda _c=False: button.toggle_preview())
        watchers = getattr(button, "watchers", None)
        if watchers is not None:
            watchers.append(lambda prefs: quietly(bool(getattr(prefs, "preview", False))))
    else:
        toggle.setChecked(bool(checked))
        if on_toggle is not None:
            toggle.clicked.connect(lambda _c=False: on_toggle(toggle.isChecked()))
    toggle.toggled.connect(lambda _on: paint())
    toggle.retint = paint
    toggle.set_quietly = quietly
    paint()
    return toggle


def add_view_controls(layout: Any, view: Any) -> Any:
    """Put the Preview toggle and the View button at the end of a tab's top
    row, and name the toggle in `view.toggles` for the window's retint. One
    line in a view, in place of `layout.addWidget(view.view_button)` - those
    views are at their line limit."""
    if view is None:                                 # the view has gone
        return None
    toggle = preview_toggle(view)
    layout.addWidget(toggle)
    layout.addWidget(view.view_button)
    view.toggles = {**getattr(view, "toggles", {}), "inspector": toggle,
                    "view": view.view_button}            # 2026-10-04: an icon too
    return toggle


def retint_toggles(view: Any, colours: dict) -> None:
    """Redraw a tab's icon toggles for a palette - what the window does for
    every tab on a theme change."""
    for toggle in dict(getattr(view, "toggles", {}) or {}).values():
        repaint = getattr(toggle, "retint", None)
        if repaint is not None:
            try:
                repaint(colours)
            except RuntimeError:                 # the C++ side has gone
                pass


#: Set on a table while this module is sizing its columns, so the handler that
#: records a *dragged* width can tell the two apart. See `_apply_widths`.
APPLYING = "leasha_applying_widths"

#: Set once a table's columns have been fitted, so it happens on the first fill
#: and not on every one. Cleared by "Fit columns to contents".
FITTED = "leasha_columns_fitted"

#: The most of a table any single column may occupy.
#:
#: **A share, not a pixel count.** A fixed cap is wrong at one end or the other:
#: 400px is most of the window at the 720px minimum and a third of it on a wide
#: monitor. A share is right at both, and it is the same number everywhere in
#: the application - Files, Mail, Search, Code - because "one column has eaten
#: the row" looks and feels identical in all of them.
MAX_COLUMN_SHARE = 0.40

#: Below this, the share is ignored and this is used instead. On a very narrow
#: window 40% of the table is a few dozen pixels, which truncates every value
#: to an ellipsis and makes the list useless in a different way than the
#: problem being fixed.
MIN_COLUMN_CAP_PX = 140


def column_cap(available: int) -> int:
    """The widest any one column may be, given the space the table has.

    Pure, so the arithmetic can be tested without a display - which is the same
    reason `visible_columns` is a function rather than a method.

    Returns 0 when there is no width to divide, meaning "do not cap": a table
    that has not been laid out yet reports a width of zero, and capping against
    that would set every column to the floor before the window is even shown.
    """
    try:
        width = int(available)
    except (TypeError, ValueError):
        return 0
    if width <= 0:
        return 0
    return max(MIN_COLUMN_CAP_PX, int(width * MAX_COLUMN_SHARE))


def _available_width(widget: Any) -> int:
    """The width a header has to divide up, or 0 if it is not laid out yet."""
    try:
        viewport = widget.viewport()
        width = int(viewport.width()) if viewport is not None else 0
        return width if width > 0 else int(widget.width())
    except (AttributeError, RuntimeError):
        return 0


def _last_column_is_chosen(
    order: Sequence[str], shown: Sequence[str], widths: Mapping[str, int]
) -> bool:
    """Has somebody dragged a width for the *last visible* column specifically?

    Added 2026-09-05, alongside the bug it answers - see the dated note in
    `_apply_widths` where this is called. Walks `order` the same way
    `_cap_columns` does, so the two functions can never disagree about which
    column counts as "last".
    """
    visible = [key for key in order if key in shown]
    return bool(visible) and visible[-1] in widths


def _cap_columns(widget: Any, order: Sequence[str], shown: Sequence[str],
                 chosen: Sequence[str] = ()) -> None:
    """Stop an *automatically fitted* column from taking the whole row.

    **The cap governs fitting, never choosing, and that distinction is the whole
    of this function.** It was added because `resizeColumnsToContents` over a
    corpus of long Windows paths produced a Name column that ate the row - a
    measurement nobody asked for and nobody wanted. Applying the same ceiling to
    a width somebody *dragged* is a different act entirely: it overrules a
    deliberate choice, and it does so silently.

    That is what "the UI still does not remember column widths" turned out to
    be. The cap is 40% of the **viewport** - on a 900px table that is 250px - so
    a column dragged to 321 was stored as 250 and restored as 250. It snapped
    back on every drag and on every launch, which is indistinguishable from the
    preference never having been saved. It was saved. It was overruled.

    `chosen` is the set of columns with a saved width. They are exempt: a person
    who drags a column to four fifths of the table has said what they want, and
    the application's job at that point is to remember it. What stops the stuck
    case - a width dragged on a wide monitor, restored on a narrow one - is
    `_bound_to_table`, which is a much higher ceiling and is about usability
    rather than taste.

    **Called with `APPLYING` already set.** Nothing is connected to
    `sectionResized` any more - see `remember_widths` for the crash that
    settled that - but the width watcher still reads the flag to tell this
    module's own sizing from a person dragging a column. This function never
    sets the flag itself, so that it cannot be called from somewhere the guard
    is missing and appear to work.

    Two columns stay exempt for their own reasons. **The last visible one**,
    because `setStretchLastSection` owns its width and capping it is a fight
    this would lose on the next repaint. And **a table showing a single
    column**, where the cap would mean four fifths of the table is permanently
    blank.
    """
    visible = [key for key in order if key in shown]
    if len(visible) < 2:
        return

    cap = column_cap(_available_width(widget))
    if cap <= 0:
        return

    last = visible[-1]
    deliberate = set(chosen)
    for index, key in enumerate(order):
        if key not in shown or key == last or key in deliberate:
            continue
        try:
            if widget.columnWidth(index) > cap:
                widget.setColumnWidth(index, cap)
        except RuntimeError:                 # the C++ side went away mid-apply
            return


def _bound_to_table(width: int, available: int) -> int:
    r"""A chosen width, kept reachable. **A ceiling only, never a floor.**

    This had `max(MIN_COLUMN_CAP_PX, ...)` in it, and that one call is the whole
    of the third column-width bug. It is a *floor*: it forced every stored width
    up to at least 140px. The owner's log said so in five consecutive lines -

        column seen width saved as 140
        column size width saved as 140
        column kind width saved as 140
        column repo width saved as 140
        column name width saved as 140

    - five different columns, five different drags, one number. Saved, restored
    faithfully, and wrong, which from the outside is indistinguishable from not
    being saved at all. That is the third report of "it does not remember my
    columns" and the second time my own guard was the thing overruling the
    person it was meant to serve.

    **A width somebody dragged to needs no floor.** If they want a 40px column
    showing an icon, that is a choice. The only defensible bound is a ceiling,
    and only because a column wider than its table cannot always be scrolled
    back into view - so a width chosen on a wide monitor and restored on a
    narrow one would arrive genuinely stuck.
    """
    try:
        room = int(available)
    except (TypeError, ValueError):
        return int(width)
    if room <= 0:
        return int(width)                    # not laid out yet; nothing to judge
    return min(int(width), room)


#: How often the width watcher looks. Milliseconds.
#:
#: **Not a signal, and that is the entire point** - see `remember_widths`.
#: Two thirds of a second is below the pause anybody makes after letting go of
#: a column, and a dozen integer reads at that rate is nothing.
WATCH_MS = 600

#: Every running width watcher: `id(look) -> (its timer, look)`.
#:
#: **This is what stops the native crash of 2026-09-27, and it is only a
#: reference.** `look` is a closure, and it sits in a reference cycle with its
#: own timer (`look` holds `watcher`; PyQt tells the garbage collector that the
#: timer's wrapper holds the slot). When a view is let go, the cyclic collector
#: can find that cycle unreachable while the C++ timer is still alive and
#: ticking. The collector then *clears* the function - its globals become
#: NULL - and the next tick calls it: a segfault on entry, before one line of
#: it runs. A core dump from the full suite showed exactly that frame:
#: `remember_widths.<locals>.look`, globals 0x0, called from QTimer::timeout.
#:
#: Held here, `look` is always reachable from this module, so the collector
#: never picks it (or anything it holds) to clear. Entries go when their timer
#: has been deleted - pruned on the next `remember_widths` - or when `look`
#: stops its own timer because the table has gone.
#:
#: **Which is why `look` may hold nothing of the view strongly (2026-09-30).**
#: Anything reachable from here lives for as long as the entry does. `look`
#: used to close over the table's header and the View button, and from those
#: the whole view could be reached - header -> the view's own context-menu
#: slot -> `self` for Files and Mail; button -> its `refit` -> the table -> a
#: slot closing over the results widget -> the view for Code (traced with
#: `gc.get_referents`). A view that was let go was then never freed, not even
#: by `gc.collect()`: the crash had become a leak. `look` now holds its timer,
#: its own small dictionaries, and weak references to the table and the button.
#: `tests/unit/test_views_are_freed.py` pins it.
_WATCHERS: dict = {}


def _keep_watcher(timer: Any, look: Any) -> None:
    """Register a watcher, first dropping those whose timer no longer exists."""
    from app.ui import qtsip as sip

    for key, (other, _look) in list(_WATCHERS.items()):
        try:
            gone = sip.isdeleted(other)
        except TypeError:
            gone = True
        if gone:
            _WATCHERS.pop(key, None)
    _WATCHERS[id(look)] = (timer, look)


def remember_widths(table: Any, button: Any, columns: Sequence[tuple[str, str]]) -> None:
    r"""Save a column width when somebody drags it, and only then.

    **This does not listen to `sectionResized`, and that is a fix rather than
    an oversight.** It used to, and the connection killed the process.

    The history is worth keeping because it took three goes. Leasha died in
    C++ with no Python exception, first inside `MainWindow.__init__` and later
    while results were being drawn - eight access violations across 26 and 27
    August 2026, every one of them at the *same* offset in `python312.dll`,
    and every crash dump naming the slot connected to `sectionResized` at an
    **unknown line**: the frame faulted on entry, before a single bytecode.

    What was tried, in order:

    1. *Guard the body* - make the slot's first statement read a Python bool
       and return. Changed nothing. A Python bool cannot fault, so the crash
       was never in the body.
    2. *Defer the `connect()`* past construction and the first theme pass. The
       window then opened, and it looked solved for a fortnight. It was not:
       it only moved the crash to every later resize.
    3. *Block the header's signals while the view is applied.* Measurably
       removed four invocations per fill - and the next two dumps showed the
       slot reached straight from `application.exec()` instead, with nothing
       of ours in between.

    The experiment that actually settled it was an environment switch that
    skipped the `connect()` entirely: with no connection, the window is fine.
    So the fault is *invoking this Python slot from `sectionResized` at all* -
    Qt calling into PyQt's glue from inside `QHeaderView`'s own layout - and
    no amount of care inside the slot can help, because the slot never runs.

    **So nothing is connected. A timer looks instead.** A `QTimer` fires from
    the event loop, between events, when Qt is idle and no layout is running -
    which is where every other slot in this application already runs safely.

    Reading widths on a timer also turns out to be *better* at the job the
    signal was bad at. `sectionResized` cannot tell a drag from a fit, and the
    old code guessed with `QApplication.mouseButtons()` - a guess its own note
    admitted could not be tested offscreen. The watcher does not guess:

    * A width that changed **while this module was applying the view** is ours.
      The `APPLYING` flag says so, and the baseline is resynced afterwards.
    * A width that changed **in the same tick the viewport changed** is Qt
      stretching the last column to fit a resized window. Not a choice.
    * A width that changed and then **stayed put for a whole tick** is a
      person who dragged a column and let go. That is the one worth saving.
    """
    if table.horizontalHeader() is None:
        return
    order = [key for key, _heading in columns]

    import weakref

    from PySide6.QtCore import QTimer

    #: **Held weakly** - see `_weakly`. `table.leasha_resync_widths` is this
    #: module's closure stored on the table, so a strong reference here made
    #: table <-> closure a cycle that only the cyclic collector could end.
    table_ref = weakref.ref(table)
    #: **The button too, and the header is asked for each time rather than
    #: kept** - see `_WATCHERS`: these closures are reachable from that
    #: registry, and both of those led from it to the whole view.
    try:
        button_ref: Any = weakref.ref(button)
    except TypeError:                            # a stand-in with no weak side
        button_ref = lambda: button              # noqa: E731

    def live() -> Any:
        found = table_ref()
        if found is None:
            raise RuntimeError("the table has been collected")
        return found

    #: Widths as this module last left them. Anything else is somebody else.
    baseline: dict = {}
    #: A width seen changing, waiting to see whether it settles.
    settling: dict = {}
    #: The width of the viewport at the last look, so a window resize can be
    #: told apart from a drag without asking Qt about the mouse.
    viewport: list = [-1]

    def widths_now() -> dict:
        view = live()
        header = view.horizontalHeader()
        return {index: int(header.sectionSize(index))
                for index in range(header.count())
                if not view.isColumnHidden(index)}

    def resync() -> None:
        """Take the current widths as the new normal. Never raises.

        Called at the end of `apply_to_table`, because everything it did was
        this module's doing and none of it is a preference.
        """
        try:
            baseline.clear()
            baseline.update(widths_now())
            settling.clear()
            viewport[0] = _available_width(live())
        except RuntimeError:
            return

    def look() -> None:
        """One pass. **Never raises** - it runs forever, unattended."""
        try:
            if live().property(APPLYING):
                return                           # mid-apply; ours, not theirs

            room = _available_width(live())
            current = widths_now()
            if not baseline:
                baseline.update(current)
                viewport[0] = room
                return

            if room != viewport[0]:
                # The window changed size. `setStretchLastSection` moves a
                # column for its own reasons and nobody chose that width.
                viewport[0] = room
                baseline.update(current)
                settling.clear()
                return

            settled = []
            for index, width in current.items():
                if width == baseline.get(index):
                    settling.pop(index, None)
                    continue
                if settling.get(index) != width:
                    settling[index] = width      # still moving; look again
                    continue
                settled.append(index)            # a drag, and it has stopped

            # **The stretched last column moved because another one did.**
            # With `setStretchLastSection` on, narrowing column one widens the
            # last to fill the gap - so it settles too, and would be recorded
            # as a width somebody chose. It is dropped only when something
            # *else* settled alongside it: if the last column is the only one
            # that moved, it really was dragged, and refusing it there would
            # mean the last column could never be sized at all.
            if (len(settled) > 1 and current
                    and live().horizontalHeader().stretchLastSection()):
                last = max(current)
                if last in settled:
                    settled.remove(last)
                    baseline[last] = current[last]

            for index in settled:
                width = current[index]
                settling.pop(index, None)
                baseline[index] = width
                if 0 <= index < len(order) and width > 0:
                    # **Stored raw.** Bounding belongs at *restore*, against
                    # the window on screen then - not here, against whatever
                    # the window happened to be during the drag. Doing it here
                    # is how a value gets baked in wrong and stays wrong: the
                    # store and the screen then agree on a number the person
                    # never chose. `_cap_columns` leaves chosen columns alone,
                    # so the two agree at the width asked for.
                    owner = button_ref()
                    if owner is None:            # the View button has gone
                        continue
                    owner.remember_width(order[index], width)
                    _log.debug("column {} width saved as {} (table {}px)",
                               order[index], width, room)
        except RuntimeError:
            # The table's C++ side went away - a tab closing, or shutdown.
            watcher.stop()
            _WATCHERS.pop(id(look), None)        # nothing left to watch
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.debug("could not check the column widths: {}", exc)

    # **Parented to the table**, so it dies with it and cannot outlive the
    # widget it reads - the failure the old `RuntimeError` guard existed for.
    watcher = QTimer(table)
    watcher.setInterval(WATCH_MS)
    watcher.timeout.connect(look)
    # Kept reachable for as long as the timer lives - see `_WATCHERS`.
    _keep_watcher(watcher, look)
    watcher.start()

    # `apply_to_table` calls this so a fitted width is never mistaken for a
    # chosen one. Without it, the first result set would pin every column for
    # ever - which is the original bug this whole function exists around.
    table.leasha_resync_widths = resync


def apply_font(widget: Any, font_pt: int) -> None:
    """Set (or clear) a point-size override on one widget, via its stylesheet.

    A stylesheet rule wins over `setFont`, and `theme.py` sets `font-size: 13px`
    on every QWidget - so this is the only way a size preference actually takes
    effect. An empty override removes the rule and lets the theme decide again,
    rather than freezing whatever size happened to be set last.
    """
    try:
        size = int(font_pt or 0)
    except (TypeError, ValueError):
        size = 0
    # Qt warns and ignores anything <= 0. A widget sized from a px stylesheet
    # reports pointSize() == -1, which is how a -1 reached setPointSize at all.
    widget.setStyleSheet(f"font-size: {size}pt;" if size > 0 else "")


def _has_rows(table: Any) -> bool:
    """Does the table hold any rows to measure? True when it cannot say, so
    an unfamiliar table keeps the old once-and-done fitting."""
    try:
        return int(table.rowCount()) > 0
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return True


def _apply_widths(table: Any, prefs: ViewPreferences,
                  order: Sequence[str], shown: Sequence[str]) -> None:
    """Restore dragged widths; fit the rest to their contents.

    **Fitting is the default and dragging is the exception**, which is the way
    round that needs no explanation: a table nobody has touched shows every
    column at the width its content needs, and a column somebody has sized
    stays where they put it.

    `resizeColumnsToContents` measures the visible rows only, so this is cheap
    on a five-hundred-row table and is why it can run on every fill rather than
    once. A column with a saved width is set afterwards, so the two never
    fight.
    """
    from PySide6.QtWidgets import QHeaderView

    header = table.horizontalHeader()
    if header is None:
        return

    # Interactive: the point of the exercise is that a width can be dragged.
    # `ResizeToContents` as a *mode* would re-measure on every repaint and undo
    # the drag; fitting once, here, gives the same starting layout without
    # taking the control away.
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    # The last visible column takes the slack, so a fitted table has no dead
    # strip on the right - which is what "fit to contents" looks like wrong.
    #
    # **But only while nobody has dragged anything.** `setStretchLastSection`
    # owns the last column's width outright: Qt recomputes it on every layout,
    # so a width dragged there is overwritten within the same repaint and a
    # saved one is overwritten on restore. The column simply refuses to keep a
    # size, which reads as the whole feature having forgotten - and on a table
    # whose last column is the one worth widening, that is the entire
    # experience of it.
    #
    # Stretching is the right default and the wrong override, so it holds until
    # somebody takes control and stops the moment they do. `_cap_columns` makes
    # the same exemption for the same reason, and now the two agree about when
    # it applies.
    #
    # **Dated note, 2026-09-05 - "somebody takes control" was read as "the
    # preference is non-empty", and that is a different, wider condition than
    # the one that matters here.** Dragging *any other* column already makes
    # `prefs.widths` non-empty, so this line turned stretch off for the last
    # column too, even though nobody had chosen a width for *it*. Measured: on
    # a three-column, 900px-wide table, dragging the middle column to 320px and
    # reopening restores the middle column at 320 exactly as promised - and the
    # last column, which had been filling the remaining ~500px, comes back at
    # its bare fitted width instead (67px in the same run), leaving hundreds of
    # pixels of dead table on the right. That is what the fifth report of
    # "column widths are not remembered" turned out to be on inspection: not a
    # width lost, but a *different* column's implicit fill width silently given
    # up, because this line could not tell "somebody chose a width" from
    # "somebody chose a width, for some other column".
    #
    # So this now asks about the last visible column specifically, through
    # `_last_column_is_chosen`, rather than about the preference as a whole.
    header.setStretchLastSection(
        not _last_column_is_chosen(order, shown, dict(prefs.widths)))

    # **The flag is not tidiness; without it this segfaults.**
    #
    # `resizeColumnsToContents` emits `sectionResized`. `remember_widths`
    # listens to that, saves the width, and saving calls `on_change`, which is
    # the view's "redraw with these preferences" - which lands back here. That
    # is unbounded recursion into C++, and it does not raise: it exhausts the
    # stack and the process dies. Caught by running the Qt suite for real,
    # after it had been written and reasoned about and looked correct.
    #
    # A mouse-button check was the first guard and is not one: a header click
    # holds the button down, which is exactly when a sort resizes columns.
    was_applying = bool(table.property(APPLYING))
    table.setProperty(APPLYING, True)
    try:
        # **Once per table, not once per fill.** "Initially auto fit" is what
        # was asked for and is also the cheaper reading: re-measuring every
        # column on every result set fights the person who has just dragged
        # one, costs a full re-layout per keystroke on a debounced list, and -
        # measured here - eventually takes the process down inside Qt's own
        # layout code. The menu's "Fit columns to contents" is how somebody
        # asks for it again, which is the only time they want it.
        #
        # **That menu item did nothing, and this condition is why.** It used to
        # read `or prefs.widths`, on the reasoning that clearing the saved
        # widths would ask for a re-measure. It does the opposite: once
        # `FITTED` is set, an empty `widths` makes the whole condition false, so
        # "Fit columns to contents" cleared the preference and then skipped the
        # only line that fits anything. The columns stayed exactly as dragged -
        # verified on a real table, `[250, 47, 588]` before and after.
        #
        # The flag is now cleared by the button before it calls back, which is
        # the honest expression of "somebody asked for a fit": an explicit
        # request, not a state the restore code has to infer.
        #
        # **Dated note, 2026-09-27 (order 0x section 9, review finding 3) -
        # "once per table" was spent on an empty table.** Files and Mail apply
        # their preferences in `__init__`, before their first query returns, so
        # the one fit measured nothing but the headings and set `FITTED`; the
        # rows arrived a moment later and were never measured. Every column
        # opened at its heading's width - Name 63px, "12 Mar ..." cut short -
        # while the stretched last column took the rest (probed on the grab
        # fixture: fit at 0 rows, widths [63, 52, 83, 56, 754]).
        #
        # So a fit over **no rows** does not use the table's one fit up -
        # **but only while nothing has been saved for this table.** With a
        # saved width, the flag is set exactly as before, rows or not, so a
        # table somebody has sized behaves identically to how it always has;
        # that is the whole of the scope this was allowed. The fit that then
        # happens on the first fill runs here, under `APPLYING` and with the
        # header's signals blocked, and `apply_to_table` resyncs the watcher's
        # baseline afterwards - so it is never recorded as a width somebody
        # chose. `_cap_columns` below still holds any fitted column to 40%.
        #
        # **Dated note, 2026-10-05 (the UI review) - that scope left the fault
        # in place for anybody who had ever dragged a column.** With one width
        # saved, the one fit was still spent on the empty table, so every
        # *other* column opened at its heading's width on every start: measured
        # on a three-column table with `folder=221` saved, Name 47px for names
        # needing 142, and seen in the user guide's own picture of Files
        # ("boiler-..."). The saved widths are put back by the loop below,
        # after the fit, and named to `_cap_columns` as chosen - so fitting on
        # the first fill cannot move a width somebody chose; it only gives the
        # columns nobody sized the width of what is in them. So rows are now
        # the only thing that uses the fit up
        # (`test_one_saved_width_does_not_leave_the_other_columns_at_their_headings`).
        if not table.property(FITTED):
            table.resizeColumnsToContents()
            if _has_rows(table):
                table.setProperty(FITTED, True)

        saved = dict(prefs.widths)
        room = _available_width(table)
        for index, key in enumerate(order):
            if key in saved and key in shown:
                table.setColumnWidth(index, _bound_to_table(saved[key], room))

        # **After the saved widths, not before**, so a column that was fitted
        # rather than chosen is what gets trimmed. The chosen ones are named so
        # this leaves them exactly where they were put - see `_cap_columns` for
        # why overruling them was the whole of the "it forgets my columns" bug.
        _cap_columns(table, order, shown, chosen=[k for k in saved if k in shown])
    finally:
        # **Restored, not cleared.** This now runs nested inside
        # `apply_to_table`, which sets the same flag for a wider span; forcing
        # it False here would drop the guard for the rest of that span while
        # the caller still believed it was up.
        table.setProperty(APPLYING, bool(was_applying))


def apply_to_table(
    table: Any, prefs: ViewPreferences, *, columns: Sequence[tuple[str, str]],
    available: Sequence[str],
) -> tuple[str, ...]:
    """Show the chosen columns at the chosen size and spacing. Returns what is visible.

    Columns are hidden rather than removed: the model keeps every column, so
    turning one back on needs no re-query and the row data stays addressable by
    a stable index.
    """
    order = [key for key, _heading in columns]
    shown = visible_columns(prefs, order, available)

    # **Nothing Python may run from `sectionResized` while this is working.**
    #
    # This function hides columns, sets a stylesheet and restores widths, and
    # every one of those makes the header resize sections. Each resize invoked
    # the `resized` slot in `remember_widths` - measured, four of them on a
    # three-column table - which is the exact condition the long note in
    # `remember_widths` names as the cause of an access violation: *Qt calling
    # into PyQt's glue during a layout pass the header has not finished.*
    #
    # That note's fix deferred the `connect()` past construction and the first
    # theme pass, and it worked for start-up. It did not cover this path,
    # which runs on **every fill**, long after the connection is live. Leasha
    # died here twice on 2026-08-27 - the crash dump reads
    # `_fill -> show_rows -> apply_prefs -> apply_to_table -> resized`, with
    # `resized` at an unknown line because the frame faulted on entry, before
    # a single bytecode ran. The same signature as the original.
    #
    # An early return inside the slot cannot fix it: that note records trying
    # exactly that and changing nothing, because a Python bool cannot fault
    # and the crash was never in the body. The invocation is the fault, so the
    # invocation is what has to stop. Blocking the header's signals means Qt
    # never reaches PyQt at all, which is the stronger form of what the
    # APPLYING flag was reaching for - and the flag is still set, because
    # `record` runs later from a timer and has to know these were ours.
    header = table.horizontalHeader()
    blocked = header is not None and not header.signalsBlocked()
    if blocked:
        header.blockSignals(True)
    table.setProperty(APPLYING, True)
    try:
        return _apply_to_table(table, prefs, order, shown)
    finally:
        # **The baseline, before the flag drops.** Every width this function
        # touched is this module's, not a preference - and the watcher in
        # `remember_widths` would otherwise see a fitted column settle and
        # record it as though somebody had dragged it, pinning every column on
        # the first result set. That is the original bug this whole area
        # exists around, so the resync is not optional.
        resync = getattr(table, "leasha_resync_widths", None)
        if callable(resync):
            resync()
        table.setProperty(APPLYING, False)
        if blocked:
            header.blockSignals(False)


def _apply_to_table(table: Any, prefs: ViewPreferences, order: Sequence[str],
                    shown: Sequence[str]) -> tuple[str, ...]:
    """The body of `apply_to_table`, with the header's signals already off."""
    from PySide6.QtGui import QFontMetrics

    for index, key in enumerate(order):
        table.setColumnHidden(index, key not in shown)

    # **Through the stylesheet, not through QFont.**
    #
    # `theme.py` sets `font-size: 13px` on QWidget, and a stylesheet rule beats
    # anything `setFont` does - so the text-size preference was being applied
    # and then silently overridden, doing nothing at all on a table.
    #
    # It also caused `QFont::setPointSize: Point size <= 0 (-1)` on startup: a
    # widget whose size came from a px stylesheet reports `pointSize() == -1`,
    # and copying that font carried the -1 along.
    apply_font(table, prefs.font_pt)

    _apply_widths(table, prefs, order, shown)

    header = table.verticalHeader()
    if header is not None:
        height = row_height_for(prefs.density, QFontMetrics(table.font()).height())
        header.setDefaultSectionSize(height)
        # Fixed, not ResizeToContents: the latter measures every row on every
        # repaint, which on five hundred rows is a visible stutter while
        # scrolling and defeats the point of a compact mode.
        from PySide6.QtWidgets import QHeaderView
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

    return shown


def apply_to_tree(
    tree: Any, prefs: ViewPreferences, *, columns: Sequence[tuple[str, str]],
    available: Sequence[str],
) -> tuple[str, ...]:
    """The same preferences, on a `QTreeWidget`. Returns what is visible.

    A separate function rather than a branch inside `apply_to_table`, because
    the one thing that differs is real: a tree has no vertical header, so row
    height comes from `setUniformRowHeights` and the item delegate's size hint
    rather than from a default section size. Everything else - which columns,
    which order, what font - is identical, and shared through `visible_columns`.

    **Density is applied to the font, not to the row.** Qt sizes a uniform tree
    row from the font, so a compact tree follows from a compact font without a
    delegate; the alternative is a custom `sizeHint` for a two-pixel difference.
    """
    order = [key for key, _heading in columns]
    shown = visible_columns(prefs, order, available)
    for index, key in enumerate(order):
        tree.setColumnHidden(index, key not in shown)
    apply_font(tree, prefs.font_pt)

    # The same cap as every table. A tree has no saved widths to restore, so
    # there is nothing to guard against here - but `setColumnWidth` still
    # emits `sectionResized`, and setting the flag costs nothing and means the
    # two paths cannot diverge if a tree ever gains them.
    tree.setProperty(APPLYING, True)
    try:
        _cap_columns(tree, order, shown)
    finally:
        tree.setProperty(APPLYING, False)
    return shown
