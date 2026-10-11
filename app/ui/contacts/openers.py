"""The doors from the Contacts page to its tool dialogs.

Layer: L5

Order 1k 6h-6j, 6n-6r. The page has buttons and menu entries for import, export,
duplicates, merge, clean-up, printing, the QR code, enrichment and Sync now; the
dialogs behind them live in `app/ui/contacts_tools/` and are built by someone else.
This module is the only thing both sides know:

    # in a dialog module, at import time
    from app.ui.contacts import openers
    openers.register("import", lambda parent, ctx: ImportDialog(ctx.hub, parent).exec())

    # in the page
    openers.open("import", self, ctx)

`open` shows a friendly "not available" message for a name nobody registered, so the page
never breaks because a tool is missing. `ctx` is a `Ctx`: the hub, the accounts store, the
selected and visible contact ids, a callback that makes the page re-read after the tool
changed something, and (when the host has one) the service that runs account syncs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = ["NAMES", "OPENERS", "Ctx", "register", "open", "is_registered", "ensure_tools_loaded"]

#: Every door the page has. A dialog registers under one of these.
NAMES = ("import", "export", "duplicates", "merge", "cleanup", "print", "qr", "enrich", "sync")

#: name -> callable(parent, ctx)
OPENERS: dict[str, Callable[[Any, "Ctx"], Any]] = {}


@dataclass
class Ctx:
    """What a tool dialog is given: the page's data and its way back."""

    hub: Any
    accounts: Any
    selected_ids: list[int] = field(default_factory=list)
    visible_ids: list[int] = field(default_factory=list)
    refresh: Callable[[], Any] = lambda: None
    service: Optional[Any] = None
    #: Enrichment (7): `enrich()` -> proposals for the selection (the visible list when none is
    #: selected); `caps_for(account)` -> what that kind of account can hold.
    enrich: Optional[Callable[[], Any]] = None
    caps_for: Optional[Callable[[Any], Any]] = None
    #: Say something on the page's status line (a long job telling the person it finished).
    notify: Optional[Callable[[str], Any]] = None


def register(name: str, fn: Callable[[Any, Ctx], Any]) -> None:
    """Put a dialog behind a door. A second registration replaces the first."""
    OPENERS[name] = fn


def is_registered(name: str) -> bool:
    ensure_tools_loaded()
    return name in OPENERS


_loaded = False


def ensure_tools_loaded() -> None:
    """Import the tool dialogs' package once, so they register themselves.

    Missing is normal (the package may not be built yet, or a dialog may fail to import
    without its optional library); that only means its doors answer "not available".
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        import app.ui.contacts_tools  # noqa: F401, PLC0415 - registers on import
    except Exception:                                    # noqa: BLE001 - a missing tool is not a broken page
        from app.core.logging import logger

        logger.bind(component="ui.contacts").debug("contacts_tools not loaded", exc_info=True)


def open(name: str, parent: Any, ctx: Ctx) -> Any:           # noqa: A001 - the name the order specifies
    """Run the dialog registered as `name`, or say that it is not available."""
    ensure_tools_loaded()
    fn = OPENERS.get(name)
    if fn is None:
        _not_available(name, parent)
        return None
    return fn(parent, ctx)


def _not_available(name: str, parent: Any) -> None:
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Not available yet")
    box.setText(f"The {name} tool is not available in this build.")
    box.setInformativeText("Nothing was changed. It will appear here when it is installed.")
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
