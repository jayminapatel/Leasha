r"""A grid of photo thumbnails, beside the results list. Work order 0h §3a.

Layer: L5

**Off by default; the list stays the default view.** The work order is
explicit - a thumbnail grid is a per-surface preference, off-able like every
behaviour this application has, the same way the preview pane and "show
scores" are off until somebody asks for them (`view_options.ViewPreferences`).
`enabled_checkbox` below follows `timeline_strip.enabled_checkbox`'s own
shape, with `default_on = False` where that one is `True` - this is a new
surface, not an established one, and the work order names the off default
explicitly rather than leaving it to guesswork.

**Drawn the way the Photos page draws** (review 2026-10-14, MS-6). The grid
had a pipeline of its own - every photo queued at once, a full decode each, a
generic file icon while it waited. It is now the Photos page's `PhotoModel`,
delegate and `ThumbLoader` (`search_photos.py`): thumbnails kept on disk, the
cells on screen asked for first and one screen ahead, a blurred preview and a
fade-in, the same grey tile while a picture is on its way. Every decode is on
a worker; a picture that cannot be decoded keeps its tile - H4: never a crash,
never a blocked scroll. A grid that is not on screen asks for nothing.

**Keys** (review 2026-10-14, MS-2 and MS-9): Down/Up from the Search box move
the grid's selection when the grid is in front (`forward_key`), Enter opens the
lightbox, and Space is quick look - the lightbox, closed again by Space.

**Nothing to show says so** (MS-5): a result set with no pictures in it shows
the one empty state every list uses, not a blank grid.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QStackedWidget, QVBoxLayout, QWidget

from app.ui.widgets import list_keys
from app.ui.widgets.file_menu import FileActions, copy_paths, show_for
from app.ui.widgets.page_states import EmptyState
from app.ui.widgets.search_photos import (
    ROLE_PHOTO, SearchPhotoModel, ask_ahead, photo_view, photos_of, shared_loader,
)

__all__ = ["ThumbnailGrid", "enabled_checkbox", "GRID_ENABLED_KEY", "NO_PHOTOS"]

#: `index_state` key for the grid's own on/off switch. Namespaced `ui:`
#: exactly like every other per-surface preference in this file's family
#: (`ui:timeline_strip_enabled`, `ui:pinned_panel_enabled`).
GRID_ENABLED_KEY = "ui:thumbnail_grid_enabled"

#: The cell a thumbnail sits in - the picture is `THUMB_CELL - 20` on a side,
#: so the caption line and the grid's own padding both fit without cropping it.
THUMB_CELL = 240

#: The empty state's words (new, 2026-10-14).
NO_PHOTOS = "None of these results is a picture. Turn the grid off to see them as a list."


def enabled_checkbox(store: Any, *, on_toggle: Any) -> QCheckBox:
    """The "Thumbnail grid" on/off switch. Off by default - work order §3a.

    Same shape as `timeline_strip.enabled_checkbox` and
    `pinned_panel.enabled_checkbox` - read once from `index_state`, never
    raising against a locked or missing database - but `default_on = False`:
    this is a new, opt-in surface, and the list stays the default the work
    order names explicitly.
    """
    box = QCheckBox("Thumbnail grid")
    box.setToolTip(
        "Show image-heavy results as thumbnails instead of a list. Off by "
        "default; your other results still show as a list either way."
    )
    default_on = False
    try:
        raw = store.get_state(GRID_ENABLED_KEY, None) if store is not None else None
    except Exception:                            # noqa: BLE001 - a preference
        raw = None
    box.setChecked(default_on if raw is None else raw not in ("off", "0", "false"))
    box.toggled.connect(on_toggle)
    return box


class ThumbnailGrid(QWidget):
    """Photos from the current result set, as thumbnails."""

    #: A thumbnail was opened (double-click / Enter). Carries the row and the
    #: full, ordered list of photo rows currently in the grid - the lightbox
    #: (`PreviewWindow`, work order §3b) uses the second argument for
    #: next/previous, so the grid is the one place that already has the
    #: ordering and does not need to reconstruct it.
    opened = Signal(object, list)
    #: The menu's "Open": the file itself, as Open does on every other page
    #: (2026-10-04). Double-click and Enter stay the lightbox (`opened`), and
    #: the menu offers that as "View".
    file_requested = Signal(object)
    reveal_requested = Signal(object)
    pin_requested = Signal(object)
    #: "More like this" from a photo's right-click menu - work order §2d.
    similar_requested = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None, *, store: Any = None) -> None:
        super().__init__(parent)
        self.model = SearchPhotoModel(shared_loader(store), self)
        self._list = photo_view(self.model, THUMB_CELL - 20)
        self._list.setObjectName("searchPhotoGrid")
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Double-click and Enter open the lightbox, from one signal; Space is quick
        # look (the lightbox again, closed by Space - `quick_look.py`), Ctrl+C copies
        # the selected photos' paths (`list_keys`).
        list_keys.install(self._list, default=self._on_activated, preview=list_keys.PAGE_PREVIEW,
                          copy=self._copy_selected)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)

        self.empty = EmptyState(NO_PHOTOS, glyph="image")
        self._pages = QStackedWidget()
        self._pages.addWidget(self._list)
        self._pages.addWidget(self.empty)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._pages)
        self._pages.setCurrentWidget(self.empty)

    # -- populating -----------------------------------------------------------

    def show_rows(self, rows: Sequence[Any]) -> None:
        """The current result set. Only the photos in it are shown.

        Called from `ResultsView.rows_changed`, which already fires on every
        search and every federated append - the grid needs no search-specific
        wiring of its own, it just filters what the list already has. The same
        photos again (a tier swap, a details redraw) swap their rows under the
        view and repaint only their words (`PhotoModel.set_rows`).
        """
        photos = photos_of(rows)
        self.model.set_rows(photos)
        self._pages.setCurrentWidget(self._list if photos else self.empty)
        if photos:
            ask_ahead(self._list, self.model)

    def clear(self) -> None:
        """Forget the rows; whatever was asked for no longer matters."""
        self.show_rows([])

    def image_rows(self) -> list[Any]:
        """The photos currently shown, in grid order - the lightbox's sibling list."""
        return self.model.result_rows()

    def showEvent(self, event: Any) -> None:     # noqa: N802 - Qt's naming
        """On screen: ask for the screen past the one shown, too."""
        super().showEvent(event)
        ask_ahead(self._list, self.model)

    # -- the keyboard (MS-2) ----------------------------------------------------

    def forward_key(self, event: Any) -> None:
        """Down/Up from the search box: Qt's own grid navigation, from the first
        photo when none is current yet."""
        if not self._list.currentIndex().isValid() and self.model.rowCount():
            self._list.setCurrentIndex(self.model.index(0, 0))
            return
        self._list.keyPressEvent(event)

    def current_row(self) -> Any:
        """The search row of the current photo, or None."""
        return self._row_at(self._list.currentIndex())

    def open_current(self, *, reveal: bool = False) -> bool:
        """Enter from the box: the lightbox (Ctrl+Enter: show in folder)."""
        row = self.current_row()
        if row is None:
            return False
        if reveal:
            self.reveal_requested.emit(row)
        else:
            self.opened.emit(row, self.image_rows())
        return True

    # -- interaction --------------------------------------------------------

    def _row_at(self, index: Any) -> Optional[Any]:
        """The result row behind a grid cell, or None."""
        if not isinstance(index, QModelIndex) or not index.isValid():
            return None
        photo = index.data(ROLE_PHOTO)
        return getattr(photo, "row", None)

    def _on_activated(self, index: Any) -> None:
        row = self._row_at(index)
        if row is not None:
            self.opened.emit(row, self.image_rows())

    def _selected_rows(self) -> list[Any]:
        indexes = sorted(self._list.selectionModel().selectedIndexes(), key=lambda i: i.row())
        return [row for row in (self._row_at(i) for i in indexes) if row is not None]

    def _copy_selected(self) -> None:
        copy_paths([(str(getattr(row, "path", "") or ""), row) for row in self._selected_rows()])

    def _on_context_menu(self, point: Any) -> None:
        """Right-click: the shared file menu, with View (the lightbox) added."""
        row = self._row_at(self._list.indexAt(point))   # the viewport's point, as Qt gives it
        if row is None:
            selected = self._selected_rows()
            row = selected[0] if selected else None
        if row is None:
            return

        show_for(self._list, point, str(getattr(row, "path", "") or ""), FileActions(
            open_file=lambda: self.file_requested.emit(row), default="view",
            view=lambda: self.opened.emit(row, self.image_rows()),
            reveal=lambda: self.reveal_requested.emit(row), row=row,
            pin=lambda: self.pin_requested.emit(row),
            similar=lambda: self.similar_requested.emit(row),
        ))
