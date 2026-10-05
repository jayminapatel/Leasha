r"""What each line of the Offline list carries by itself.

Layer: L5

2026-10-02, the owner: each line should carry its own button, "as if i have
lot of archives it will index all of them otherwise", and the list "should
also show the hw id". Two columns after 2a's five, and a menu on the line:

* **Hardware ID** - the words are the presenter's (`offline.hardware_id_words`);
  this only puts them on the line, with "Copy hardware ID" on right-click.
* **Rescan** - an icon on every line. Rescan was one button above the list
  for whichever line was selected; with several drives that is
  select-then-press for each. On the line it is one press, and each button
  says by itself whether its drive is in reach.

Kept out of `offline_media_view.py` because a view stays short
(`test_presenter.py`); nothing here owns a store or starts a worker either.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QPushButton, QWidget

from app.ui.widgets.buttons import icon_button, put_on_row

__all__ = [
    "EXTRA_COLUMNS", "LineActions", "copy_hardware_id", "rescan_button",
    "row_menu", "sync_rescan_button",
]

#: After 2a's five columns, never among them.
EXTRA_COLUMNS = ("Hardware ID", "Rescan")

#: A line's Rescan tooltip, by what the line can do now.
RESCAN_READY = (
    "Rescan - this source only.\n\n"
    "Walks it again for what has changed since its last Scan. Nothing else "
    "is read.")
RESCAN_AWAY = (
    "Rescan is only available while this source is plugged in. Its catalogue "
    "and everything already indexed from it stay exactly as they are.")


def rescan_button(row: Any, on_press: Callable[[int], None]) -> QPushButton:
    """The Rescan icon for one line; `on_press(volume_id)` when it is pressed."""
    button = icon_button("Rescan", tooltip=RESCAN_READY, name=f"Rescan {row.name}")
    button.clicked.connect(
        lambda _checked=False, volume_id=row.volume_id: on_press(volume_id))
    return button


def sync_rescan_button(button: Optional[QPushButton], row: Any, busy: bool) -> None:
    """On only for a source in reach while nothing is running, and saying why
    when it is off."""
    if button is None:
        return
    online = row.status_code == "ONLINE"
    button.setEnabled(online and not busy)
    button.setToolTip(RESCAN_READY if online else
                      f"{RESCAN_AWAY}\n\nCurrent status: {row.status}.")


def copy_hardware_id(row: Any) -> str:
    """Put a source's disk serial on the clipboard. Returns the sentence to
    show, or "" when the source has no serial to copy."""
    serial = str(getattr(row, "hardware_serial", "") or "")
    if not serial:
        return ""
    QApplication.clipboard().setText(serial)
    return f"Copied the hardware ID of {row.name!r}."


def row_menu(parent: QWidget, row: Any, *, busy: bool,
             on_rescan: Callable[[int], None],
             on_copy: Callable[[int], None]) -> QMenu:
    """The line's right-click menu: Rescan, and Copy hardware ID."""
    menu = QMenu(parent)
    rescan = QAction("Rescan", menu)
    rescan.setEnabled(row.status_code == "ONLINE" and not busy)
    rescan.triggered.connect(lambda _checked=False: on_rescan(row.volume_id))
    menu.addAction(rescan)
    copy = QAction("Copy hardware ID", menu)
    copy.setEnabled(bool(getattr(row, "hardware_serial", "")))
    copy.triggered.connect(lambda _checked=False: on_copy(row.volume_id))
    menu.addAction(copy)
    return menu


class LineActions:
    """Mixed into `OfflineMediaView`: what a line does by itself.

    The view supplies `tree`, `_rows` (the `VolumeRow`s on screen), `_busy`,
    `_row_buttons` (`{volume id: its Rescan button}`), `status_line` and the
    `rescan_requested` signal; this owns nothing of its own.
    """

    def _add_line_button(self, item: Any, row: Any, column: int) -> None:
        button = rescan_button(row, self._rescan_row)
        self._row_buttons[row.volume_id] = button
        put_on_row(self.tree, item, column, button)

    def _sync_row_buttons(self) -> None:
        for row in self._rows:
            sync_rescan_button(self._row_buttons.get(row.volume_id), row, self._busy)

    def _row_for(self, volume_id: Optional[int]) -> Any:
        for row in self._rows:
            if row.volume_id == volume_id:
                return row
        return None

    def _row_menu(self, point: Any) -> None:
        item = self.tree.itemAt(point)
        value = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        row = self._row_for(int(value) if value is not None else None)
        if row is None:
            return
        menu = row_menu(self, row, busy=self._busy, on_rescan=self._rescan_row,
                        on_copy=self.copy_hardware_id)
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def copy_hardware_id(self, volume_id: int) -> bool:
        """Put a source's disk serial on the clipboard. False if it has none."""
        row = self._row_for(volume_id)
        said = copy_hardware_id(row) if row is not None else ""
        if said:
            self.status_line.setText(said)
        return bool(said)

    def _rescan_row(self, volume_id: int) -> None:
        """A line's own Rescan: that source, whichever line is selected."""
        row = self._row_for(volume_id)
        if row is not None and row.status_code == "ONLINE" and not self._busy:
            self.rescan_requested.emit(row.volume_id)
