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

from PySide6.QtCore import QThreadPool, Signal

from app.ui.presenter import mail_places as places
from app.ui.widgets.side_list import SideList

__all__ = ["MailFolderList", "attach", "STATE_KEY", "FOLDERS_TIP", "folder_tree", "in_folder",
           "mail_entries"]

STATE_KEY = "ui:mail_folders"
FOLDERS_TIP = ("Show or hide your mailboxes and their folders on the left. "
               "Click one to see only its mail; All mail shows everything again.")


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


#: `{account id: name}` for the live mailboxes, set when the list is drawn; the summary line reads it.
_NAMES: dict[str, str] = {}


def in_folder(summary: str, choice: Any) -> str:
    """D2: the summary line says which mailbox or folder the list is narrowed to."""
    if not choice:
        return summary
    archive, path = choice
    where = path.rsplit("/", 1)[-1] if path else places.place_label(archive, _NAMES)
    return f"{summary} - in {where}" if summary else f"In {where}"


class MailFolderList(SideList):
    """The tree, drawn by `SideList` (order 1p). `chosen` carries `(archive, folder)`, or None
    for All mail."""

    chosen = Signal(object)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("mailFolders", accessible="Mailboxes and folders",
                         tip="Click a mailbox or a folder to see only its mail", parent=parent)
        self.picked.connect(lambda entry: self.chosen.emit(entry.value))
        self.show_rows(())

    def show_rows(self, rows: Any, names: Optional[dict] = None) -> None:
        """`rows` as `mail_folder_counts` gives them; `names` the live accounts' names, by id."""
        if isinstance(rows, tuple) and len(rows) == 2 and isinstance(rows[1], dict):
            rows, names = rows                                   # the worker hands both over together
        _NAMES.clear()
        _NAMES.update(names or {})
        self.set_entries(mail_entries(folder_tree(rows), names or {}))

    def _clicked(self, item: Any, column: int) -> None:          # kept for tests that click
        super()._clicked(item, column)


def mail_entries(tree: dict, names: Optional[dict] = None) -> list:
    """All mail, then Mailboxes: each archive or account and its folders, nested, with counts."""
    names = names or {}
    from app.ui.presenter.side_entry import SideEntry

    total = sum(node["count"] for node in tree.values())
    entries = [SideEntry("all", "All mail", icon="mail", count=total or None, kind="all",
                         value=None, tip="Every message, in every mailbox")]

    def add(archive: str, parent: str, folders: dict) -> None:
        for name in sorted(folders, key=str.lower):
            node = folders[name]
            key = f"{archive}|{node['path']}"
            entries.append(SideEntry(key, name, icon="folder", count=node["count"], parent=parent,
                                     value=(archive, node["path"]), tip=node["path"]))
            add(archive, key, node["folders"])

    for archive in sorted(tree, key=lambda a: (not places.is_account(a), places.place_label(a, names).lower())):
        node = tree[archive]
        key = f"{archive}|"
        entries.append(SideEntry(key, places.place_label(archive, names), section="Mailboxes",
                                 icon=places.place_icon(archive), count=node["count"],
                                 value=(archive, ""), tip=places.place_tip(archive, names)))
        add(archive, key, node["folders"])
    return entries


def _read_places(store: Any) -> tuple:
    """On a worker: the folders with their counts, and the live accounts' names."""
    return store.mail_folder_counts(), store.mail_account_names()


def attach(view: Any, top: Any) -> MailFolderList:
    """Put the list left of Mail's results, its switch on the top row, its entry in View
    (`side_panel.attach`, the same for every page), and count the folders on a worker."""
    from app.ui.widgets.side_panel import attach as attach_side
    from app.ui.workers import CallableWorker, run

    folders = MailFolderList()
    ref = weakref.ref(view)
    view.mail_folder = None

    def pick(choice: Any) -> None:
        mail = ref()
        if mail is not None:
            mail.mail_folder = choice
            mail._run()

    folders.chosen.connect(pick)
    before = getattr(view, "toggles", {}).get("inspector") or view.view_button
    show = attach_side(view, folders, target=view.split, row=top, before=before, page="mail",
                       words="Folders", tip=FOLDERS_TIP, state_key=STATE_KEY)
    view.folder_split = view.side_split
    view.toggles["folders"] = view.toggles["side"]
    view.show_folders = show

    def load() -> None:
        mail = ref()
        if mail is None:
            return
        worker = CallableWorker(_read_places, mail._store, component="ui.mail")
        worker.signals.finished.connect(folders.show_rows)
        worker.signals.failed.connect(lambda _e: None)          # the list stays as it was
        run(QThreadPool.globalInstance(), worker)

    view.load_folders = load
    if view._store is not None:
        load()
    return folders
