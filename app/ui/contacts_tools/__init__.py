"""The Contacts page's tool dialogs (order 1k 6g-6r, 7).

Layer: L5

Importing this package puts each dialog behind its door in `app.ui.contacts.openers`, which is
all the Contacts page knows about them:

    import     6i  Import contacts: file, column mapping, preview with duplicates, one-step undo
    export     6j  Export: vCard, CSV (Google / Outlook / plain), Excel, PDF list or business cards
    duplicates 6n  Possible duplicates: scan, merge all safe matches, not a duplicate, rejected
    merge      6g  Merge dialog and the preview of what each account will be sent (6o)
    cleanup    6p  Clean-up: phone format, name capitals, company from email domain, finders
    print      6q  Labels, envelopes and a printed address book, with a preview
    qr         6r  The card's QR code and quick actions
    enrich     7   Enrichment proposals, accepted per contact or in bulk

Each opener imports its dialog only when it is used, so the page opens without loading the
PDF, QR or spreadsheet libraries, and a dialog that cannot import says so when asked for rather
than taking the others down. `ctx` is `openers.Ctx`: hub, accounts, selected_ids, visible_ids,
refresh, service (and, when the host has them, `enrich` and `caps_for`).
"""

from __future__ import annotations

from typing import Any

from app.ui.contacts import openers

__all__ = ["register_all", "DOORS"]

#: door name -> (module, class)
DOORS = {
    "import": ("import_dialog", "ImportDialog"),
    "export": ("export_dialog", "ExportDialog"),
    "duplicates": ("duplicates_dialog", "DuplicatesDialog"),
    "merge": ("merge_dialog", "MergeDialog"),
    "cleanup": ("cleanup_dialog", "CleanupDialog"),
    "print": ("print_dialog", "PrintDialog"),
    "qr": ("qr_dialog", "QrDialog"),
    "enrich": ("enrich_dialog", "EnrichDialog"),
}


def _dialog_class(name: str) -> Any:
    import importlib                                          # noqa: PLC0415

    module, cls = DOORS[name]
    return getattr(importlib.import_module(f"app.ui.contacts_tools.{module}"), cls)


def _opener(name: str):
    def open_tool(parent: Any, ctx: Any) -> Any:
        try:
            cls = _dialog_class(name)
        except Exception as exc:                              # noqa: BLE001 - a missing library is not a broken page
            from app.core.logging import logger               # noqa: PLC0415

            logger.bind(component="ui.contacts_tools").warning("{} tool did not load: {}", name, exc)
            _tell(parent, "This tool is not available", f"The {name} tool could not start ({exc}). Nothing was changed.")
            return None
        if name == "merge":
            ids = list(getattr(ctx, "selected_ids", []) or [])
            if len(ids) < 2:
                _tell(parent, "Select two or more contacts first", "Pick the contacts to merge in the list, "
                      "then choose Merge. Nothing was changed.")
                return None
            dialog = cls(ctx, ids, parent)
        else:
            dialog = cls(ctx, parent)
        result = dialog.exec()
        callback = getattr(ctx, "refresh", None)
        if callable(callback):
            callback()
        return result

    open_tool.__name__ = f"open_{name}"
    return open_tool


def _tell(parent: Any, title: str, text: str) -> None:
    from PySide6.QtWidgets import QMessageBox                  # noqa: PLC0415

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def _open_sync(parent: Any, ctx: Any) -> Any:
    """Sync (10c): the Sync window, which starts a sync if none is going. The scheduler is the one
    starter besides the schedule; the window only asks it."""
    service = getattr(ctx, "service", None)
    if callable(getattr(service, "scheduler", None)) and service.scheduler() is not None:
        from app.ui.contacts_tools.sync_dialog import open_sync_window  # noqa: PLC0415

        return open_sync_window(parent, ctx)
    ask = getattr(service, "request_sync", None)
    if not callable(ask):
        _tell(parent, "Sync is not available here", "Open Settings > Accounts and press Sync now.")
        return None
    if not ask():
        _tell(parent, "A sync is already running", "Sync now was folded into the one in progress.")
    return None


def register_all() -> None:
    for name in DOORS:
        openers.register(name, _opener(name))
    openers.register("sync", _open_sync)


register_all()
