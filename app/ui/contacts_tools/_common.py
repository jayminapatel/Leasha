"""What every Contacts tool dialog shares: the base dialog, workers, icons and card lists.

Layer: L5

Order 1k 6g-6r. Nothing here knows about a particular tool. It is the part that keeps the
nine dialogs one family:

* `ToolDialog` - the header (icon, title, one line of help), a body, a status line, a
  progress bar and a Close button. Every control is given a tooltip and an accessible name
  through `named`, and a test walks each dialog to prove it.
* `Job` - **rule 5.** Every read of the book and every write to it runs on the global
  `QThreadPool` through `CallableWorker`; the answer comes back to the dialog on the UI
  thread. A `Job` is a `QObject` that lives on the UI thread, so Qt queues the signal to it
  and the callback never runs on the worker's thread.
* `btn` - a button of the one button system (`widgets/buttons.py`). The words of the new
  buttons are not in `BUTTONS` yet, so the icon and kind are passed explicitly.
* `CardList` - a card drawn as a list of icon + text rows, the same glyphs as the editor
  (6m), used for previews and for what an account will be sent.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QObject, QSize, Qt, QThreadPool, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QProgressBar, QPushButton, QTableWidget, QVBoxLayout, QWidget,
)

from app.contacts.card import SCALARS, Card, display_name
from app.ui.theme import SPACE_S, theme_colours
from app.ui.widgets.buttons import action_button
from app.ui.widgets.icons import icon, icon_size
from app.ui.workers import CallableWorker, run
from app.ui.widgets.dialog_frame import frame

__all__ = [
    "named", "btn", "Job", "idle", "ToolDialog", "CardList", "make_table", "kind_icon",
    "scalar_icon", "SCALAR_LABELS", "KIND_LABELS", "plural", "summary_line", "ink", "pixmap",
    "card_rows", "contact_names",
]

#: Words for the single-valued parts, as the editor shows them.
SCALAR_LABELS = {
    "prefix": "Title (Mr, Dr)", "given": "First name", "additional": "Middle name", "family": "Last name",
    "suffix": "Suffix", "nickname": "Nickname", "company": "Company", "department": "Department",
    "title": "Job title", "notes": "Notes",
}
KIND_LABELS = {
    "email": "Email", "phone": "Phone", "address": "Address", "url": "Website", "date": "Date",
    "related": "Related person", "social": "Social", "im": "Chat", "custom": "Other",
}
_KIND_ICON = {"email": "mail", "phone": "phone", "address": "map-pin", "url": "globe", "date": "calendar",
              "related": "users", "social": "at-sign", "im": "at-sign", "custom": "tag"}
_SCALAR_ICON = {"company": "building-2", "department": "building-2", "title": "briefcase",
                "notes": "sticky-note", "nickname": "user"}


def kind_icon(kind: str, label: str = "") -> str:
    if kind == "date" and "birth" in label.lower():
        return "cake"
    from app.ui.contacts.logic import label_icon           # noqa: PLC0415 - the one table of label pictures

    return label_icon(kind, label, _KIND_ICON.get(kind, "tag"))


def scalar_icon(name: str) -> str:
    return _SCALAR_ICON.get(name, "user")


def ink(token: str = "text_dim") -> str:
    return theme_colours().get(token, "#888888")


def pixmap(name: str, px: int = 16, token: str = "text_dim"):
    return icon(name, ink(token)).pixmap(QSize(px, px))


def plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def named(widget: Any, name: str, tip: str = "") -> Any:
    """Give a control its screen-reader name and its tooltip. Returns the control."""
    widget.setAccessibleName(name)
    widget.setToolTip(tip or name)
    return widget


def btn(text: str, glyph: str, role: str = "secondary", tip: str = "", name: str = "") -> QPushButton:
    """A system button with its icon, kind, tooltip and accessible name."""
    button = action_button(text, glyph, role)
    named(button, name or text.rstrip("…").strip() or text, tip or text)
    return button


# --------------------------------------------------------------------------
# Workers (rule 5)
# --------------------------------------------------------------------------

_ACTIVE: set = set()


def idle() -> bool:
    """True when no tool dialog has work running or waiting to be delivered. For tests."""
    return not _ACTIVE


class Job(QObject):
    """Run `fn(**)` on a pool thread; deliver `on_done(result)` on the UI thread.

    The owner is the parent, so closing a dialog drops its jobs' callbacks with it.
    A failure arrives as an `AppError` at `on_fail` (default: the owner's `show_error`).
    """

    def __init__(self, owner: QObject, fn: Callable[..., Any], on_done: Optional[Callable[[Any], Any]] = None,
                 on_fail: Optional[Callable[[Any], Any]] = None,
                 on_progress: Optional[Callable[[Any], Any]] = None, *, component: str = "ui.contacts_tools",
                 **kwargs: Any) -> None:
        super().__init__(owner)
        self._owner = owner
        self._done_cb = on_done
        self._fail_cb = on_fail
        self._progress_cb = on_progress
        self._worker = CallableWorker(fn, component=component, report_progress=on_progress is not None, **kwargs)
        self._worker.signals.finished.connect(self._finished)
        self._worker.signals.failed.connect(self._failed)
        self._worker.signals.progress.connect(self._progress)
        self._worker.signals.done.connect(self._release)
        _ACTIVE.add(self)
        owner.destroyed.connect(lambda *_a, job=self: _ACTIVE.discard(job))
        run(QThreadPool.globalInstance(), self._worker)

    @Slot(object)
    def _finished(self, result: Any) -> None:
        if self._done_cb is not None:
            self._done_cb(result)

    @Slot(object)
    def _failed(self, error: Any) -> None:
        if self._fail_cb is not None:
            self._fail_cb(error)
        elif hasattr(self._owner, "show_error"):
            self._owner.show_error(error)

    @Slot(object)
    def _progress(self, stage: Any) -> None:
        if self._progress_cb is not None:
            self._progress_cb(stage)

    @Slot()
    def _release(self) -> None:
        _ACTIVE.discard(self)


# --------------------------------------------------------------------------
# The dialog
# --------------------------------------------------------------------------

class ToolDialog(QDialog):
    """Header, body, status line, progress bar and a Close button."""

    def __init__(self, ctx: Any, parent: Optional[QWidget], *, title: str, glyph: str, subtitle: str,
                 size: tuple[int, int] = (880, 620), name: str = "") -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle(title)
        self.setObjectName(name or title)
        self.setAccessibleName(title)
        self.setMinimumSize(560, 420)
        self.resize(*size)
        root = frame(self)                        # the one dialog frame (UI phase B)
        head = QHBoxLayout()
        self.header_icon = QLabel()
        self.header_icon.setPixmap(pixmap(glyph, 28, "accent"))
        self.header_icon.setAccessibleName(f"{title} icon")
        self.header_icon.setToolTip(title)
        titles = QVBoxLayout()
        self.title_label = QLabel(f"<b>{title}</b>")
        self.title_label.setAccessibleName(title)
        self.title_label.setToolTip(title)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setAccessibleName(f"About {title}")
        self.subtitle_label.setToolTip(subtitle)
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle_label)
        head.addWidget(self.header_icon, 0, Qt.AlignmentFlag.AlignTop)
        head.addLayout(titles, 1)
        root.addLayout(head)
        self.body = QVBoxLayout()
        root.addLayout(self.body, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        named(self.status, "Status", "What this window is doing, or what went wrong")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        named(self.progress, "Progress", "Working in the background")
        self.progress.hide()
        root.addWidget(self.status)
        root.addWidget(self.progress)
        self.footer = QHBoxLayout()
        self.footer.setSpacing(SPACE_S)
        self.footer.addStretch(1)
        self.close_button = btn("Close", "x", "secondary", "Close this window", "Close")
        self.close_button.clicked.connect(self.close)
        self.footer.addWidget(self.close_button)
        root.addLayout(self.footer)

    # -- state ---------------------------------------------------------------
    def say(self, text: str) -> None:
        self.status.setStyleSheet("")
        self.status.setText(text)

    def show_error(self, error: Any) -> None:
        """An `AppError` (or any exception) in plain words: what happened and what to do."""
        self.busy(False)
        message = getattr(error, "message", None) or str(error)
        fix = getattr(error, "suggestion", "") or ""
        self.status.setStyleSheet(f"color: {ink('danger')};")
        self.status.setText(f"{message} {fix}".strip())

    def busy(self, on: bool, text: str = "") -> None:
        self.progress.setVisible(on)
        if text or not on:
            self.say(text)

    def add_footer_buttons(self, *buttons: QWidget) -> None:
        """Put action buttons at the left of the footer row, before the stretch."""
        for index, b in enumerate(buttons):
            self.footer.insertWidget(index, b)

    def job(self, fn: Callable[..., Any], on_done: Optional[Callable[[Any], Any]] = None,
            on_fail: Optional[Callable[[Any], Any]] = None, on_progress: Optional[Callable[[Any], Any]] = None,
            **kwargs: Any) -> Job:
        return Job(self, fn, on_done, on_fail, on_progress, **kwargs)

    def refresh_page(self) -> None:
        """Tell the Contacts page the book changed, so it re-reads."""
        callback = getattr(self.ctx, "refresh", None)
        if callable(callback):
            try:
                callback()
            except Exception:                           # noqa: BLE001 - a page that cannot refresh is its own problem
                pass


# --------------------------------------------------------------------------
# Tables and card lists
# --------------------------------------------------------------------------

def make_table(owner: QWidget, headers: list[str], name: str, tip: str, *, stretch: int = 0,
               select: str = "rows") -> QTableWidget:
    table = QTableWidget(0, len(headers), owner)
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if select == "multi"
                           else QAbstractItemView.SelectionMode.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    if stretch:
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    named(table, name, tip)
    return table


def contact_names(hub: Any, ids: Iterable[int]) -> dict[int, str]:
    """{id: display name}. A worker's job: it reads the book."""
    out: dict[int, str] = {}
    for cid in ids:
        card = hub.get_card(cid)
        out[cid] = display_name(card) if card is not None else f"Contact {cid}"
    return out


def summary_line(card: Card) -> str:
    """Name, company, main email and phone on one line."""
    bits = [display_name(card)]
    company = card.get("company")
    if company and company != bits[0]:
        bits.append(company)
    for kind in ("email", "phone"):
        values = [f for f in card.values(kind) if f.value.strip()]
        if values:
            main = next((f for f in values if f.primary), values[0])
            bits.append(main.value)
    return "  ·  ".join(b for b in bits if b)


def card_rows(card: Card) -> list[tuple[str, str]]:
    """A card as (glyph, text) rows: name first, then each scalar and each value."""
    rows: list[tuple[str, str]] = []
    name = display_name(card)
    if name:
        rows.append(("user", name))
    for key in SCALARS:
        if key in ("prefix", "given", "additional", "family", "suffix"):
            continue
        text = card.get(key).strip()
        if text:
            rows.append((scalar_icon(key), text.replace("\n", " / ") if key == "notes" else text))
    for f in card.fields:
        text = f.value.strip() or ", ".join(v for v in f.data.values() if v)
        if not text:
            continue
        label = f"{f.label}: " if f.label else ""
        rows.append((kind_icon(f.kind, f.label), f"{label}{text.replace(chr(10), ', ')}"
                     + ("  (main)" if f.primary else "")))
    if card.groups:
        rows.append(("tag", ", ".join(card.groups)))
    if card.favourite:
        rows.append(("star", "Favourite"))
    if card.photo is not None:
        rows.append(("image", "Photo"))
    return rows


class CardList(QListWidget):
    """A card as icon + text rows. Read only."""

    def __init__(self, name: str, tip: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setIconSize(icon_size("list"))
        self.setWordWrap(True)
        named(self, name, tip)

    def set_card(self, card: Optional[Card]) -> None:
        self.clear()
        if card is None:
            return
        for glyph, text in card_rows(card):
            item = QListWidgetItem(icon(glyph, ink()), text)
            self.addItem(item)

    def texts(self) -> list[str]:
        return [self.item(i).text() for i in range(self.count())]
