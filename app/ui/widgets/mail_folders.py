r"""Mail's list on the left: every mailbox and its folders, and a click narrows the list.

Layer: L5 widget

Order 1n (2026-10-11), the owner, after the Photos list: "if we wanted that kind
of menu in files, mail, code and even say browse what would they be ? same thing
but has to be user friendly". Mail first, because every message already carries
its archive (`messages.store_path`) and its folder in it (`folder_path`).

* **D1** "All mail", then each archive, then its folders as Outlook nests them,
  each with how many messages it holds.
* **D2** A click narrows the Mail list to that archive or folder and the folders
  inside it, together with whatever is typed; "All mail" takes it away.
* **D3** Switched like the Photos list: the panel icon beside Preview and View,
  and View > Folders, remembered as `ui:mail_folders`, on by default.

Built here and attached in one call (`attach`), because `mail_view.py` is at its
length limit. The counts are read on a worker; nothing here touches the store on
the window's thread.
"""

from __future__ import annotations

import weakref
from pathlib import PureWindowsPath
from typing import Any, Optional

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import QSplitter, QTreeWidget, QTreeWidgetItem

__all__ = ["MailFolderList", "attach", "STATE_KEY", "FOLDERS_TIP", "folder_tree", "in_folder"]

STATE_KEY = "ui:mail_folders"
FOLDERS_TIP = ("Show or hide your mailboxes and their folders on the left. "
               "Click one to see only its mail; All mail shows everything again.")
_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def folder_tree(rows: Any) -> dict:
    """`{archive: {"count": n, "folders": {name: {"count": n, "path": p, "folders": {...}}}}}`
    from `(archive, folder, messages)` rows. A folder's count includes the folders in it."""
    tree: dict = {}
    for archive, folder, count in rows:
        node = tree.setdefault(archive, {"count": 0, "folders": {}})
        node["count"] += int(count)
        level, path = node["folders"], ""
        for part in [p for p in str(folder or "").split("/") if p]:
            path = f"{path}/{part}" if path else part
            entry = level.setdefault(part, {"count": 0, "path": path, "folders": {}})
            entry["count"] += int(count)
            level = entry["folders"]
    return tree


def _words(count: int) -> str:
    return f"{count:,}"


def in_folder(summary: str, choice: Any) -> str:
    """D2: the summary line says which mailbox or folder the list is narrowed to."""
    if not choice:
        return summary
    archive, path = choice
    where = path.rsplit("/", 1)[-1] if path else (PureWindowsPath(archive).name or archive)
    return f"{summary} - in {where}" if summary else f"In {where}"


class MailFolderList(QTreeWidget):
    """The tree. `chosen` carries `(archive, folder)`, or None for All mail."""

    chosen = Signal(object)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.setObjectName("mailFolders")
        self.setAccessibleName("Mailboxes and folders")
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setMinimumWidth(170)
        self.itemClicked.connect(self._clicked)
        self._fill({})

    def _fill(self, tree: dict) -> None:
        self.clear()
        total = sum(node["count"] for node in tree.values())
        everything = QTreeWidgetItem(["All mail", _words(total) if total else ""])
        everything.setData(0, _ROLE, None)
        self.addTopLevelItem(everything)
        for archive in sorted(tree, key=lambda a: PureWindowsPath(a).name.lower()):
            node = tree[archive]
            name = PureWindowsPath(archive).name or archive or "(no archive)"
            top = QTreeWidgetItem([name, _words(node["count"])])
            top.setToolTip(0, archive)
            top.setData(0, _ROLE, (archive, ""))
            self.addTopLevelItem(top)
            self._add(top, archive, node["folders"])
        for column in (1,):
            self.header().setStretchLastSection(False)
            self.resizeColumnToContents(column)
        self.setCurrentItem(everything)

    def _add(self, parent: QTreeWidgetItem, archive: str, folders: dict) -> None:
        for name in sorted(folders, key=str.lower):
            entry = folders[name]
            item = QTreeWidgetItem([name, _words(entry["count"])])
            item.setData(0, _ROLE, (archive, entry["path"]))
            item.setToolTip(0, entry["path"])
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            parent.addChild(item)
            self._add(item, archive, entry["folders"])

    def show_rows(self, rows: Any) -> None:
        self._fill(folder_tree(rows))

    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self.chosen.emit(item.data(0, _ROLE))


def attach(view: Any, top: Any) -> MailFolderList:
    """Put the list left of Mail's results, its icon on the top row, its entry in View."""
    from app.ui.state_writes import save_state
    from app.ui.widgets.side_toggle import side_toggle
    from app.ui.workers import CallableWorker, run

    folders = MailFolderList()
    ref = weakref.ref(view)
    view.mail_folder = None

    outer = QSplitter(Qt.Orientation.Horizontal)
    view.layout().replaceWidget(view.split, outer)
    outer.addWidget(folders)
    outer.addWidget(view.split)
    outer.setStretchFactor(1, 1)
    outer.setSizes([220, 900])
    view.folder_split = outer

    def pick(choice: Any) -> None:
        mail = ref()
        if mail is not None:
            mail.mail_folder = choice
            mail._run()

    folders.chosen.connect(pick)

    def show(on: bool, *, remember: bool = True) -> None:
        mail = ref()
        if mail is None:
            return
        folders.setVisible(bool(on))
        toggle.set_quietly(bool(on))
        if remember:
            save_state(mail._store, STATE_KEY, "1" if on else "0", component="ui.mail")

    toggle = side_toggle("Folders", tip=FOLDERS_TIP, accessible="Mailboxes and folders list",
                         name="toggle_mail_folders", on_click=lambda on: show(on))
    top.insertWidget(max(0, top.indexOf(view.view_button) - 1), toggle)
    view.toggles = {**getattr(view, "toggles", {}), "folders": toggle}
    view.show_folders = show

    def load() -> None:
        mail = ref()
        if mail is None:
            return
        worker = CallableWorker(mail._store.mail_folder_counts, component="ui.mail")
        worker.signals.finished.connect(folders.show_rows)
        worker.signals.failed.connect(lambda _e: None)          # the list stays as it was
        run(QThreadPool.globalInstance(), worker)

    view.load_folders = load

    # View > Folders, in the tab's own View menu and the window's.
    making = view.view_button.menu_for

    def menu_for(parent: Any) -> Any:
        menu = making(parent)
        if menu is not None:
            entry = menu.addAction("Folders")
            entry.setCheckable(True)
            entry.setChecked(folders.isVisible() or not folders.isHidden())
            entry.setToolTip(FOLDERS_TIP)
            entry.triggered.connect(lambda on=False: show(bool(on)))
        return menu

    view.view_button.menu_for = menu_for
    try:
        shown = str(view._store.get_state(STATE_KEY, "") or "1") == "1"
    except Exception:                                            # noqa: BLE001 - shown by default
        shown = True
    show(shown, remember=False)
    if view._store is not None:
        load()
    return folders
