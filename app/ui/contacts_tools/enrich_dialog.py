"""Enrichment proposals (order 1k section 7).

Layer: L5

A shell for the person's say-so. Enrichment **proposes**; nothing is kept until it is accepted here,
one contact at a time or all at once. The proposals come from `ctx.enrich`, an optional callable the
host supplies; without it the dialog says plainly that enrichment is off and does nothing. Nothing is
looked up when the dialog opens: **Look for suggestions** is the person's click.

What `ctx.enrich()` returns is read defensively: a list of proposal objects, or an object holding them
in `.proposals`. A proposal is expected to have `contact_id`, `field_label`, `old`, `new` and `source`
(and may have `receipt` and `confidence`). Accepting calls the result's `apply(selected)` if it has one,
else `ctx.enrich_apply(selected)`, else `app.contacts.enrich.base.apply_proposals` for the stock
`Proposal` class, which writes the accepted values as one journal batch (so **Undo these** is one
step).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget

from app.contacts.card import display_name
from app.ui.contacts_tools._common import ToolDialog, btn, ink, named, plural
from app.ui.widgets.icons import icon

__all__ = ["EnrichDialog", "EnrichData", "gather_proposals", "apply_selected"]


@dataclass
class EnrichData:
    proposals: list = field(default_factory=list)
    names: dict = field(default_factory=dict)
    apply: Optional[Callable[[list], Any]] = None
    available: bool = True


def gather_proposals(ctx: Any) -> EnrichData:
    """Ask the host for proposals and the names of the contacts they are about. A worker's job."""
    fn = getattr(ctx, "enrich", None)
    if not callable(fn):
        return EnrichData(available=False)
    try:
        result = fn()
    except TypeError:
        result = fn(list(getattr(ctx, "selected_ids", []) or []))
    proposals = getattr(result, "proposals", None)
    if proposals is None:
        proposals = list(result or [])
    apply = getattr(result, "apply", None)
    if not callable(apply):
        apply = getattr(ctx, "enrich_apply", None)
    names: dict = {}
    for p in proposals:
        cid = getattr(p, "contact_id", None)
        if cid is not None and cid not in names:
            card = ctx.hub.get_card(cid)
            names[cid] = display_name(card) if card is not None else f"Contact {cid}"
    return EnrichData(list(proposals), names, apply if callable(apply) else None)


def apply_selected(ctx: Any, data: EnrichData, indexes: list[int]) -> Any:
    """Write the accepted proposals through the best door there is. Returns what that door returned."""
    chosen = [data.proposals[i] for i in indexes]
    if data.apply is not None:
        return data.apply(chosen)
    try:
        from app.contacts.enrich.base import Proposal, apply_proposals   # noqa: PLC0415
    except ImportError:
        raise RuntimeError("Enrichment cannot be applied here: no way to write the accepted values was provided.")
    if all(isinstance(p, Proposal) for p in data.proposals):
        return apply_proposals(ctx.hub, data.proposals, indexes)
    raise RuntimeError("Enrichment cannot be applied here: the proposals came without a way to write them.")


class EnrichDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Enrich contacts", glyph="sparkles",
                         subtitle="Suggestions for missing details. Nothing is added until you accept it, and every accepted "
                                  "value remembers where it came from.",
                         size=(900, 600), name="contacts-enrich")
        self.hub = ctx.hub
        self.data: Optional[EnrichData] = None
        self.last_batch: Optional[str] = None

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Contact / detail", "Now", "Suggested", "Where from"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        named(self.tree, "Suggestions", "Details enrichment would add. Tick the ones to keep")
        self.tree.itemChanged.connect(lambda _i, _c: self._sync())
        self.body.addWidget(self.tree, 1)

        self.find_button = btn("Look for suggestions", "sparkles", "secondary",
                               "Ask the enrichment sources you switched on for suggestions", "Look for suggestions")
        self.accept_button = btn("Accept selected", "check", "primary", "Keep the ticked suggestions", "Accept selected")
        self.accept_all_button = btn("Accept all", "list-checks", "secondary", "Keep every suggestion", "Accept all")
        self.undo_button = btn("Undo these", "undo-2", "secondary", "Take back what was just accepted", "Undo these")
        self.undo_button.hide()
        self.find_button.clicked.connect(self.find)
        self.accept_button.clicked.connect(self.accept_selected)
        self.accept_all_button.clicked.connect(self.accept_every)
        self.undo_button.clicked.connect(self.undo)
        self.add_footer_buttons(self.find_button, self.accept_button, self.accept_all_button, self.undo_button)
        self.has_source = callable(getattr(ctx, "enrich", None))
        if not self.has_source:
            self.say("Enrichment is not switched on. Turn it on in Settings > Accounts, then come back.")
            self.find_button.setEnabled(False)
        self._sync()

    # -- finding -------------------------------------------------------------
    def find(self) -> None:
        self.busy(True, "Looking for suggestions…")
        self.find_button.setEnabled(False)
        ctx = self.ctx
        self.job(lambda: gather_proposals(ctx), self._found)

    def _found(self, data: EnrichData) -> None:
        self.find_button.setEnabled(self.has_source)
        self.data = data
        self.tree.blockSignals(True)
        self.tree.clear()
        by_contact: dict[Any, QTreeWidgetItem] = {}
        for index, p in enumerate(data.proposals):
            cid = getattr(p, "contact_id", None)
            top = by_contact.get(cid)
            if top is None:
                top = QTreeWidgetItem([data.names.get(cid, f"Contact {cid}")])
                top.setIcon(0, icon("user", ink()))
                top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                top.setCheckState(0, Qt.CheckState.Unchecked)
                self.tree.addTopLevelItem(top)
                by_contact[cid] = top
            old, new = getattr(p, "old", ""), getattr(p, "new", "")
            child = QTreeWidgetItem([str(getattr(p, "field_label", "") or getattr(p, "field_kind", "")),
                                     str(old or ""), "(a photo)" if hasattr(new, "mime") else str(new),
                                     str(getattr(p, "source", ""))])
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setCheckState(0, Qt.CheckState.Unchecked)
            child.setData(0, Qt.ItemDataRole.UserRole, index)
            receipt = getattr(p, "receipt", "")
            if receipt:
                child.setToolTip(3, str(receipt))
            top.addChild(child)
            top.setExpanded(True)
        self.tree.blockSignals(False)
        for col in range(4):
            self.tree.resizeColumnToContents(col)
        n = len(data.proposals)
        self.busy(False, f"{plural(n, 'suggestion')} for {plural(len(by_contact), 'contact')}. Tick what you want to keep."
                  if n else ("Enrichment is not switched on." if not data.available else "No suggestions right now."))
        self._sync()

    # -- accepting -----------------------------------------------------------
    def ticked(self) -> list[int]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    out.append(child.data(0, Qt.ItemDataRole.UserRole))
        return out

    def every(self) -> list[int]:
        return list(range(len(self.data.proposals))) if self.data else []

    def _sync(self) -> None:
        have = bool(self.data and self.data.proposals)
        self.accept_button.setEnabled(have and bool(self.ticked()))
        self.accept_all_button.setEnabled(have)

    def accept_selected(self) -> None:
        self._accept(self.ticked())

    def accept_every(self) -> None:
        self._accept(self.every())

    def _accept(self, indexes: list[int]) -> None:
        if not indexes or self.data is None:
            return
        self.busy(True, "Keeping the suggestions…")
        ctx, data = self.ctx, self.data
        self.job(lambda: (indexes, apply_selected(ctx, data, indexes)), self._accepted)

    def _accepted(self, payload: Any) -> None:
        indexes, result = payload
        self.last_batch = getattr(result, "batch_id", None) or None
        self.undo_button.setVisible(bool(self.last_batch))
        applied = getattr(result, "applied", None)
        n = len(applied) if applied is not None else len(indexes)
        done = set(applied if applied is not None else indexes)
        for i in range(self.tree.topLevelItemCount() - 1, -1, -1):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount() - 1, -1, -1):
                if top.child(j).data(0, Qt.ItemDataRole.UserRole) in done:
                    top.removeChild(top.child(j))
            if top.childCount() == 0:
                self.tree.takeTopLevelItem(i)
        self.busy(False, f"Kept {plural(n, 'suggestion')}." + (" Undo these takes them back in one step." if self.last_batch else ""))
        self.refresh_page()
        self._sync()

    def undo(self) -> None:
        batch = self.last_batch
        if not batch:
            return
        self.busy(True, "Taking them back…")
        hub = self.hub
        self.job(lambda: hub.undo_batch(batch), self._undone)

    def _undone(self, result: Any) -> None:
        self.last_batch = None
        self.undo_button.hide()
        self.busy(False, f"Undone: {plural(result.undone, 'contact')} put back.")
        self.refresh_page()
