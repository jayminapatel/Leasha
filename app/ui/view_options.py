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

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "load_prefs", "save_prefs", "build_menu", "apply_to_table", "button",
    "Density", "ViewPreferences", "DEFAULT_FONT_PT", "FONT_RANGE",
    "available_columns", "visible_columns", "row_height_for", "parse_prefs",
    "prefs_to_state", "DENSITIES",
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
        return ViewPreferences(
            columns=tuple(key for key in order if key in current),
            density=self.density,
            font_pt=self.font_pt,
        )


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

    return ViewPreferences(columns=columns, density=density, font_pt=font_pt)


def prefs_to_state(prefs: ViewPreferences, prefix: str) -> dict[str, str]:
    """The inverse of `parse_prefs`, for one batched write."""
    return {
        f"{prefix}:columns": ",".join(prefs.columns),
        f"{prefix}:density": prefs.density,
        f"{prefix}:font_pt": str(int(prefs.font_pt)),
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
) -> Any:
    """The "View" menu: which columns, how tight, how big.

    `columns` is `(key, heading)` in canonical order. `on_change` takes the new
    `ViewPreferences` - the caller persists and redraws, because this module
    knows nothing about stores or tables.
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
        action.triggered.connect(
            lambda _checked, n=name: on_change(
                ViewPreferences(prefs.columns, n, prefs.font_pt))
        )
        menu.addAction(action)

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
        lambda value: on_change(ViewPreferences(
            prefs.columns, prefs.density,
            0 if value < FONT_RANGE[0] else value,
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

    def show(at: Any = None) -> None:
        menu = build_menu(
            widget, widget.prefs, columns=columns,
            available=widget.available, on_change=changed,
        )
        menu.exec(at or widget.mapToGlobal(widget.rect().bottomLeft()))

    widget.show_menu = show
    widget.clicked.connect(lambda: show())
    return widget


def apply_to_table(
    table: Any, prefs: ViewPreferences, *, columns: Sequence[tuple[str, str]],
    available: Sequence[str],
) -> tuple[str, ...]:
    """Show the chosen columns at the chosen size and spacing. Returns what is visible.

    Columns are hidden rather than removed: the model keeps every column, so
    turning one back on needs no re-query and the row data stays addressable by
    a stable index.
    """
    from PyQt6.QtGui import QFont, QFontMetrics

    order = [key for key, _heading in columns]
    shown = visible_columns(prefs, order, available)

    for index, key in enumerate(order):
        table.setColumnHidden(index, key not in shown)

    font = QFont(table.font())
    if prefs.font_pt:
        font.setPointSize(int(prefs.font_pt))
    table.setFont(font)

    header = table.verticalHeader()
    if header is not None:
        height = row_height_for(prefs.density, QFontMetrics(font).height())
        header.setDefaultSectionSize(height)
        # Fixed, not ResizeToContents: the latter measures every row on every
        # repaint, which on five hundred rows is a visible stutter while
        # scrolling and defeats the point of a compact mode.
        from PyQt6.QtWidgets import QHeaderView
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

    return shown
