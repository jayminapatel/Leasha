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
from typing import Any, Iterable, Mapping, Sequence

from app.core.logging import logger

_log = logger.bind(component="ui.view")

__all__ = [
    "load_prefs", "save_prefs", "build_menu", "apply_to_table", "apply_to_tree",
    "button",
    "Density", "ViewPreferences", "DEFAULT_FONT_PT", "FONT_RANGE",
    "available_columns", "visible_columns", "row_height_for", "parse_prefs",
    "prefs_to_state", "DENSITIES", "Metrics", "remember_widths",
    "column_cap", "MAX_COLUMN_SHARE", "MIN_COLUMN_CAP_PX",
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
    name_bump: int = 2
    #: Points removed for the grey lines.
    meta_drop: int = 1

    @classmethod
    def for_density(cls, density: str) -> "Metrics":
        if density == Density.COMPACT:
            # Padding shrinks, text does not. The point of a compact list is
            # more rows on screen; text you cannot read is not more information.
            return cls(pad_y=2, gap=1)
        return cls()


def available_columns(
    rows: Iterable[Any], columns: Sequence[tuple[str, str]], *, always: Sequence[str] = ()
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
    """
    materialised = list(rows)
    found: list[str] = []
    for key, attribute in columns:
        if key in always:
            found.append(key)
            continue
        for row in materialised:
            value = getattr(row, attribute, None) if not isinstance(row, Mapping) else row.get(attribute)
            if value not in (None, "", 0, False):
                found.append(key)
                break
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
    except Exception:                                # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# The Qt half: a menu, and applying the result to a table
# ---------------------------------------------------------------------------

def build_menu(
    parent: Any,
    prefs: ViewPreferences,
    *,
    columns: Sequence[tuple[str, str]],
    available: Sequence[str],
    on_change: Any,
    grouping: bool = False,
    on_fit: Any = None,
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
    from PyQt6.QtGui import QAction, QActionGroup
    from PyQt6.QtWidgets import QMenu, QWidgetAction, QSpinBox, QLabel, QWidget, QHBoxLayout

    order = [key for key, _heading in columns]
    shown = set(visible_columns(prefs, order, available))
    menu = QMenu("View", parent)

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
    spin.setKeyboardTracking(False)
    spin.valueChanged.connect(
        lambda value: on_change(replace(
            prefs, font_pt=0 if value < FONT_RANGE[0] else value,
        ))
    )
    row.addWidget(spin)
    holder = QWidgetAction(menu)
    holder.setDefaultWidget(box)
    menu.addAction(holder)

    return menu


def button(
    parent: Any,
    store: Any,
    prefix: str,
    *,
    columns: Sequence[tuple[str, str]] = (),
    on_change: Any = None,
    grouping: bool = False,
    table: Any = None,
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
    from PyQt6.QtWidgets import QToolButton

    widget = QToolButton(parent)
    widget.setText("View")
    widget.setToolTip(
        "Which columns, how tightly packed, and how big" if columns
        else "Text size and row spacing"
    )
    widget.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    widget.prefs = load_prefs(store, prefix) if store is not None else ViewPreferences()
    widget.available = tuple(key for key, _heading in columns)

    def changed(prefs: ViewPreferences) -> None:
        widget.prefs = prefs
        if store is not None:
            save_prefs(store, prefix, prefs)
        if on_change is not None:
            on_change(prefs)

    def refit() -> None:
        """Forget every dragged width and measure the columns again.

        **Both halves, which is what the menu item always promised.** Clearing
        the preference alone left `_apply_widths` with nothing to do: its
        re-measure is gated on the table's `FITTED` flag, so the columns stayed
        exactly as dragged while the setting that produced them disappeared -
        the preference and the screen disagreeing, which is worse than either.
        """
        if table is not None:
            try:
                table.setProperty(FITTED, False)
            except RuntimeError:                 # the C++ side has gone
                pass
        changed(replace(widget.prefs, widths=()))

    def show(at: Any = None) -> None:
        menu = build_menu(
            widget, widget.prefs, columns=columns,
            available=widget.available, on_change=changed, grouping=grouping,
            on_fit=refit,
        )
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
            save_prefs(store, prefix, widget.prefs)

    widget.show_menu = show
    widget.toggle_preview = toggle_preview
    widget.remember_width = remember_width
    widget.refit = refit
    # **The button owns the preferences, so it owns the wiring that writes
    # them.** Passing the table here rather than making every view call
    # `remember_widths` itself is what keeps this one line instead of four in
    # each of them - and `mail_view.py` was one line over the length guard,
    # which is the guard doing its job.
    if table is not None and columns:
        remember_widths(table, widget, columns)
    widget.clicked.connect(lambda: show())
    return widget


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

    **Called with `APPLYING` already set.** `setColumnWidth` emits
    `sectionResized`, which `remember_widths` listens to - see the note in
    `_apply_widths` about the recursion that kills the process. This function
    never sets the flag itself, so that it cannot be called from somewhere the
    guard is missing and appear to work.

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
    """A chosen width, kept usable. **Not the fitting cap.**

    A column wider than the table it lives in cannot be scrolled back into view
    on some layouts, so a width dragged on a wide monitor and restored on a
    narrow one arrives genuinely stuck. This is the only ceiling a deliberate
    width gets, and it is deliberately generous: it says "not wider than the
    window", not "not wider than we would have chosen".
    """
    try:
        room = int(available)
    except (TypeError, ValueError):
        return int(width)
    if room <= 0:
        return int(width)                    # not laid out yet; nothing to judge
    return max(MIN_COLUMN_CAP_PX, min(int(width), room))


def remember_widths(table: Any, button: Any, columns: Sequence[tuple[str, str]]) -> None:
    """Save a column width when somebody drags it, and only then.

    **`sectionResized` cannot tell a drag from a fit.** It fires for both, and
    `resizeColumnsToContents` runs on every fill - so connecting it directly
    would store a width on the first result set and pin the column there for
    ever, silently disabling the fitting this exists alongside. Qt does report
    the difference, through `QHeaderView.sectionHandleDoubleClicked` and through
    the mouse, but the simple and reliable signal is this one: a resize that
    happens while the header is being dragged.
    """
    header = table.horizontalHeader()
    if header is None:
        return
    order = [key for key, _heading in columns]

    def record(index: int, new: int) -> None:
        """The actual work, run from the event loop rather than inside Qt."""
        try:
            if table.property(APPLYING):
                return
            if 0 <= index < len(order) and new > 0:
                # **Stored as dragged.** This used to apply `column_cap` - 40%
                # of the viewport - on the way in, so a column dragged to 321px
                # on a 900px table was stored as 250 and restored as 250. It
                # snapped back on every drag and every launch, which is exactly
                # "the UI does not remember column widths": the preference was
                # saved and then overruled.
                #
                # The reasoning behind it was sound and applied to the wrong
                # thing. Storing what will actually be shown *is* right - a
                # preference the table contradicts is worse than either - but
                # the answer is to stop contradicting a deliberate width rather
                # than to shrink it before saving. `_cap_columns` now leaves
                # chosen columns alone, so the two agree at the width asked for.
                #
                # Bounded only by the table itself, which is about a width being
                # reachable rather than about it being tasteful.
                width = _bound_to_table(int(new), _available_width(table))
                button.remember_width(order[index], width)
                # **Logged because this is the link that cannot be tested
                # here.** Everything either side of it is covered - the store
                # round-trip, the restore, the cap - but whether a real drag on
                # Windows reaches this line at all depends on Qt's mouse state,
                # and no offscreen test can hold a mouse button down. One DEBUG
                # line turns "it does not remember" into a question the run log
                # answers.
                _log.debug("column {} width saved as {}", order[index], width)
        except RuntimeError:
            # The table's C++ side went away between the resize and this
            # callback - a tab closing, or shutdown. Nothing to save, and
            # nothing worth reporting.
            return

    def resized(index: int, _old: int, new: int) -> None:
        # **Ours, or theirs?** `sectionResized` fires for both, and recording a
        # width we set ourselves feeds straight back into setting it again -
        # see the note in `_apply_widths` for the crash that produced.
        if table.property(APPLYING):
            return
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication

        # A second, weaker signal for the resizes Qt does on its own - a
        # window resize with `setStretchLastSection` on, for one. Those hold no
        # mouse button; a drag does.
        if not (QApplication.mouseButtons() & _Qt.MouseButton.LeftButton):
            # Qt resizing on its own account - a window resize, a theme
            # re-polish changing font metrics. Recorded at DEBUG rather than
            # dropped silently, because "the width was never saved" and "the
            # width was saved and then lost" need different fixes and the log
            # is what tells them apart.
            _log.debug("column resize ignored: no button held (section {})", index)
            return

        # **Never save from inside the signal, and this one crashed the app.**
        #
        # `sectionResized` is emitted from the middle of Qt's own layout, and
        # `remember_width` saves preferences, which calls `on_change`, which
        # re-applies the whole view - re-entering the layout that is still
        # running. `_apply_widths` guards its own recursion with the APPLYING
        # flag, but that only covers resizes *it* starts. This one arrives from
        # `MainWindow._apply_theme`: setting a stylesheet on the top-level
        # window re-polishes every child, font metrics change, header sections
        # resize, and this fires with the flag clear.
        #
        # The window then died in C++ during construction with no Python
        # exception - no traceback, no run-log footer, nothing on screen. The
        # crash stack, once faulthandler was enabled, was exactly
        # `__init__ -> _apply_theme -> resized`.
        #
        # A zero-delay timer moves the save to the next turn of the event loop,
        # when Qt has finished laying out and re-entering it is safe. The width
        # is captured now, so a later drag cannot change what gets recorded.
        QTimer.singleShot(0, lambda i=index, n=new: record(i, n))

    # **The connection is made after the window exists, not during construction.**
    #
    # This is the fix, and it took bisecting to find. The window died in C++
    # inside `MainWindow.__init__`, every faulthandler dump naming
    # `_apply_theme -> resized`. Three attempts at what the slot *does* -
    # deferring the save, guarding the body so its first statement only read a
    # Python bool, moving the background workers off construction - changed
    # nothing. A Python bool cannot fault, so the crash was never in the body.
    #
    # An environment switch that skipped `connect()` entirely was the experiment
    # that settled it: with no connection the window opens. So the fault is in
    # *invoking a Python slot from `sectionResized` while `setStyleSheet` is
    # re-polishing the widget tree* - Qt calling into PyQt's glue during a
    # layout pass the header has not finished.
    #
    # Deferring the connection itself removes that window completely: during
    # construction and the first theme pass nothing is attached to the signal,
    # and afterwards this behaves exactly as it always did - a direct
    # connection, so the APPLYING check still sees the flag its own resizes set.
    #
    # Nothing is lost. A column width can only be dragged by somebody looking at
    # the window, and every resize before that point is Qt laying out, which
    # this had to ignore anyway.
    from PyQt6.QtCore import QTimer as _QTimer

    def listen() -> None:
        try:
            header.sectionResized.connect(resized)
        except RuntimeError:
            # The table went away before the event loop turned - a view built
            # and discarded during start-up. Nothing to connect to.
            return

    _QTimer.singleShot(0, listen)


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
    from PyQt6.QtWidgets import QHeaderView

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
    header.setStretchLastSection(not prefs.widths)

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
        if not table.property(FITTED):
            table.resizeColumnsToContents()
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
        table.setProperty(APPLYING, False)


def apply_to_table(
    table: Any, prefs: ViewPreferences, *, columns: Sequence[tuple[str, str]],
    available: Sequence[str],
) -> tuple[str, ...]:
    """Show the chosen columns at the chosen size and spacing. Returns what is visible.

    Columns are hidden rather than removed: the model keeps every column, so
    turning one back on needs no re-query and the row data stays addressable by
    a stable index.
    """
    from PyQt6.QtGui import QFontMetrics

    order = [key for key, _heading in columns]
    shown = visible_columns(prefs, order, available)

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
        from PyQt6.QtWidgets import QHeaderView
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
