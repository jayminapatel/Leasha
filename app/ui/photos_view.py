r"""The Photos tab: see, find and look after your pictures - and name the people in them.

Layer: L5

2026-10-05, the owner, in order: "would it be helpful to have a chip just for
pictures designed to view find and deal with pictures including namings?";
"all that functionality should be also available on photos page"; "list,
small thumbnail or normal thumbnail etc design a system"; "make it like a
professional photo management/viewer"; "like other tabs where i can narrow by
year name location etc etc .. also have / commands".

Left, the narrowing lists (`PhotoSidebar`); centre, the photos in Details or
Small/Medium/Large thumbnails (`PhotoBrowser`); right, everything known about
the selected photo (`PhotoInfo`). The box above reads like every tab's, chips
and all. "People to name" swaps the centre for the naming page, Accept all
included. Double-click opens the viewer; "Write names into photos…" is
option b, on demand. Logic is in `presenter.photos`; reads are on workers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter,
                             QStackedWidget, QVBoxLayout, QWidget)

from app.ui.presenter.photos import PHOTOS_COMMANDS, facets, narrow, sort_rows, summary
from app.ui.presenter.photos import toggle_in_box
from app.ui.widgets.chips import list_chips, show_page
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.photo_browser import PhotoBrowser
from app.ui.widgets.photo_info import PhotoInfo
from app.ui.widgets.photo_page_parts import STATE_KEYS, ViewButton, photo_menu, same_day_box
from app.ui.widgets.photo_sidebar import PhotoSidebar
from app.ui.widgets.photo_thumbs import ThumbLoader

__all__ = ["PhotosView"]

#: How long typing settles before the box is read again.
DEBOUNCE_MS = 220


def _library(store: Any) -> tuple[list, int]:
    """Every picture, and how many faces wait for a Yes or No. **Worker.**"""
    from app.extract.ocr import OcrExtractor

    rows = store.photo_library(OcrExtractor.extensions)
    waiting = sum(count for _p, _n, count in store.suggestion_counts())
    return rows, waiting


def _read(store: Any, text: str, reading: dict) -> tuple:
    """The box, read as every tab reads it. **Worker.**"""
    from app.search.run import read_typed, words_of

    parsed, applied = read_typed(store, text, surface="files", **reading)
    return parsed, applied, words_of(parsed)


class PhotosView(QWidget):
    open_requested = Signal(str)
    reveal_requested = Signal(str)
    error = Signal(object)

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._pool = QThreadPool.globalInstance()
        self._all: list = []
        self._parsed: Any = None
        self._words = ""
        self._generation = 0
        self._naming: Any = None
        self.sort_key = self._state("sort") or "newest"
        data_dir = Path(getattr(store, "db_path", "")).parent if getattr(
            store, "db_path", None) else None
        self._data_dir = data_dir
        self.thumbs = ThumbLoader(data_dir / "thumbs" if data_dir else None, self)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Find photos - type words, or / for who, date, place, "
                                      "only unnamed …")
        self.input.setAccessibleName("Find photos")
        self.input.setClearButtonEnabled(True)
        self._popup = attach_to(self.input, only=PHOTOS_COMMANDS, store=store)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._run)
        self.input.textChanged.connect(self._typed)
        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")

        self.write_button = QPushButton("Write names into photos…")
        self.write_button.setToolTip("Write the people and descriptions into the photos' own "
                                     "metadata, so other photo programs see them")
        self.write_button.clicked.connect(self.write_names)
        self.view_button = ViewButton(self)

        self.sidebar = PhotoSidebar()
        self.sidebar.toggled.connect(self._toggle)
        self.sidebar.action.connect(self._sidebar_action)
        self.browser = PhotoBrowser(self.thumbs)
        self.browser.current_changed.connect(self._current)
        self.browser.selection_changed.connect(self._selection)
        self.browser.opened.connect(self.view_photo)
        self.browser.menu_requested.connect(self._menu)
        self.info = PhotoInfo(store)
        self.info.open_requested.connect(self._open)
        self.info.reveal_requested.connect(self._reveal)
        self.info.view_requested.connect(self.view_photo)
        self.info.name_requested.connect(self.show_naming)

        self.centre = QStackedWidget()
        self.centre.addWidget(self.browser)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        for widget, stretch in ((self.sidebar, 0), (self.centre, 1), (self.info, 0)):
            self.split.addWidget(widget)
            self.split.setStretchFactor(self.split.indexOf(widget), stretch)
        self.split.setSizes([220, 900, 320])

        top = QHBoxLayout()
        top.addWidget(self.input, 1)
        top.addWidget(self.write_button)
        top.addWidget(self.view_button)
        layout = QVBoxLayout(self)
        self.chips = list_chips(self, layout, top, self._run)
        layout.addWidget(self.summary)
        layout.addWidget(self.split, 1)

        self.set_mode(self._state("mode") or "medium", remember=False)
        self.show_info((self._state("info") or "1") == "1", remember=False)
        self.refresh()

    # -- reading -------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the library again - on opening the tab, and after a run."""
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(_library, self._store, component="ui.photos")
        when_done(self, worker, finished=self._library_ready, failed=self.error.emit)
        run(self._pool, worker)

    def _library_ready(self, result: Any) -> None:
        rows, waiting = result
        self._all = list(rows)
        self.sidebar.fill(facets(self._all), waiting)
        self._run()

    def _typed(self, text: str) -> None:
        self.sidebar.set_box(text)
        self._timer.start()

    def _run(self, *_args: Any) -> None:
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        self._generation += 1
        generation = self._generation
        worker = CallableWorker(_read, self._store, self.input.text(), self.chips.reading(),
                                component="ui.photos")
        when_done(self, worker, finished=lambda read, g=generation: self._read(read, g),
                  failed=self.error.emit)
        run(self._pool, worker)

    def _read(self, read: Any, generation: int) -> None:
        if generation != self._generation:
            return
        self._parsed, applied, self._words = read
        self._show(applied)

    def _show(self, applied: Any = ()) -> None:
        shown = narrow(self._all, self._parsed, self._words) if self._parsed else self._all
        order = getattr(self._parsed, "sort", "") or self.sort_key
        self.browser.set_rows(sort_rows(shown, order), dated=order in ("newest", "oldest"))
        show_page(self, applied, summary(len(shown), len(self._all),
                                         len(self.browser.selected_rows())))

    # -- the choices -----------------------------------------------------------------

    def set_mode(self, mode: str, *, remember: bool = True) -> None:
        self.browser.set_mode(mode)
        if remember:
            self._remember("mode", self.browser.mode)

    def set_sort(self, key: str) -> None:
        self.sort_key = key
        self._remember("sort", key)
        self._show()

    def show_info(self, on: bool, *, remember: bool = True) -> None:
        self.info.setVisible(bool(on))
        if remember:
            self._remember("info", "1" if on else "0")

    def _state(self, key: str) -> str:
        """A remembered choice, or "" - never an error that stops the tab opening."""
        try:
            return str(self._store.get_state(STATE_KEYS[key]) or "")
        except Exception:                          # noqa: BLE001 - a default is fine
            return ""

    def _remember(self, key: str, value: str) -> None:
        from app.ui.state_writes import save_state

        save_state(self._store, STATE_KEYS[key], value)

    def _toggle(self, switch: str, value: Any) -> None:
        self.input.setText(toggle_in_box(self.input.text(), switch, value))

    def _sidebar_action(self, action: str) -> None:
        if action == "clear":
            self.input.clear()
        elif action == "name_people":
            self.show_naming()

    # -- the selected photo -------------------------------------------------------------

    def _current(self, row: Any) -> None:
        if self.info.isVisible():
            self.info.show_row(row)

    def _selection(self) -> None:
        shown = self.browser.model.rowCount()
        self.summary.setText(summary(shown, len(self._all), len(self.browser.selected_rows())))

    def _open(self, row: Any) -> None:
        self.open_requested.emit(str(row.path))

    def _reveal(self, row: Any) -> None:
        self.reveal_requested.emit(str(row.path))

    def view_photo(self, row: Any) -> None:
        from app.ui.widgets.photo_viewer import PhotoViewer

        self.browser.select_path(str(row.path))
        viewer = PhotoViewer(row, self.browser.step, self._position, self)
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        viewer.show()

    def _position(self) -> tuple[int, int]:
        index = self.browser.grid.selectionModel().currentIndex()
        return (index.row() + 1 if index.isValid() else 0), self.browser.model.rowCount()

    def _menu(self, row: Any, point: Any) -> None:
        day = same_day_box(row)
        menu = photo_menu(
            self, row, view=lambda: self.view_photo(row), open_file=lambda: self._open(row),
            reveal=lambda: self._reveal(row),
            name=(lambda: self.show_naming()) if row.faces else None,
            write=lambda: self.write_names(selected_only=True),
            same_day=(lambda: self.input.setText(day)) if day else None)
        menu.exec(point)

    # -- naming, and writing names into photos ---------------------------------------

    def show_naming(self, *_args: Any) -> None:
        """Swap the photos for the naming page - Accept all, the "Is this ...?"
        questions, Manage faces - with a way back."""
        if self._naming is None:
            from app.ui.widgets.photo_tagger_page import PhotoTaggerPage

            holder = QWidget()
            back = QPushButton("Back to photos")
            back.setToolTip("Return to your photos, with what you named included")
            back.clicked.connect(self.show_photos)
            page = PhotoTaggerPage(self._store)
            page.opened.connect(self.open_requested.emit)
            column = QVBoxLayout(holder)
            column.setContentsMargins(0, 0, 0, 0)
            column.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
            column.addWidget(page, 1)
            self._naming = page
            self.centre.addWidget(holder)
            from app.ui.widgets.buttons import style_all

            style_all(holder)
        else:
            self._naming.reload()
        self.centre.setCurrentIndex(1)
        self._info_was = self.info.isVisible()
        self.info.hide()                        # nothing selected to show there

    def show_photos(self) -> None:
        self.centre.setCurrentWidget(self.browser)
        self.info.setVisible(getattr(self, "_info_was", self.info.isVisible()))
        self.refresh()                          # names given there show here

    def write_names(self, *_args: Any, selected_only: bool = False) -> None:
        from app.ui.widgets.photo_write_dialog import WriteNamesDialog

        selected = [row.file_id for row in self.browser.selected_rows()]
        shown = [row.file_id for row in self.browser.model.rows()]
        dialog = WriteNamesDialog(self._store, self._data_dir, selected=selected,
                                  shown=shown, parent=self)
        dialog.exec()
        if dialog.result_text:
            self.refresh()

    def shutdown(self) -> None:
        """Stop the debounce timer - see `workers.stop_timers`."""
        from app.ui.workers import stop_timers

        stop_timers(self)
        self.thumbs.save_tiny()                 # the blurred previews made this session
