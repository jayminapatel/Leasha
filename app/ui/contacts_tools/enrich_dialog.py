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

Order 1s section 7 (the rules are `enrich_rows`): rows grouped by contact, surest first, with how sure
in words; a row starts ticked at **Tick automatically from** and is hidden below **Hide suggestions
below** (both read from the settings on the worker); **Accept selected** is still the one button that
writes what is ticked, and **Accept all** asks once, with the count, when it would take rows that did
not start ticked. **Never suggest these** remembers the rows the person unticked
(`app.contacts.enrich.declines`), so no source offers them for that contact again. The run's notes are
one line under the list, not only a notice.
"""

from __future__ import annotations

import time

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QLabel, QTreeWidget, QTreeWidgetItem, QWidget

from app.contacts.card import display_name
from app.ui.contacts_tools import enrich_rows
from app.ui.contacts_tools._common import ToolDialog, btn, ink, named, plural
from app.ui.contacts_tools.enrich_rows import sure_words
from app.ui.contacts_tools.enrich_run import run_for
from app.ui.widgets.icons import icon
from app.ui.widgets.select_all import SelectBar

__all__ = ["EnrichDialog", "EnrichData", "gather_proposals", "apply_selected", "sure_words"]


@dataclass
class EnrichData:
    proposals: list = field(default_factory=list)
    names: dict = field(default_factory=dict)
    apply: Optional[Callable[[list], Any]] = None
    available: bool = True
    #: What the sources said besides suggestions: one unreachable, or one not asked again within 90 days.
    notes: list = field(default_factory=list)
    #: Order 1s D4, from the settings when the run began: at or above, a row starts ticked; below, it is hidden.
    tick_from: float = 0.85
    hide_below: float = 0.5
    #: Order 1s D1: the age window's start (Unix seconds), or None for any age or no mail source on.
    since: Optional[int] = None


def _review_settings(ctx: Any) -> dict:
    """D4's two thresholds and D1's window start, read on the worker (the settings live in the index)."""
    try:
        settings = ctx.accounts.settings()
    except Exception:                                    # noqa: BLE001 - a host without settings keeps the defaults
        return {}
    from app.contacts.enrich.run import window_of        # noqa: PLC0415

    out = {"tick_from": float(getattr(settings, "enrich_tick_from", 0.85)),
           "hide_below": float(getattr(settings, "enrich_hide_below", 0.5))}
    if getattr(settings, "enrich_signatures", True):     # the window is about mail; say it only when mail is read
        out["since"] = window_of(settings)
    return out


def when_words(when: Any, now: Optional[float] = None) -> str:
    """Order 1s 2d: the date a suggestion's message was sent, and how long ago - "12 Mar 2026, 3 weeks ago"."""
    from app.ui.presenter.formatting import format_when  # noqa: PLC0415 - the Search tab's own words

    try:
        seconds = int(when or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    day = time.strftime("%d %b %Y", time.gmtime(seconds)).lstrip("0")
    return f"{day}, {format_when(seconds * 1_000_000_000, now=now)}"


def _call_host(fn: Callable[..., Any], offered: dict, selected: list) -> Any:
    """Call the host's `enrich` with every keyword of `offered` it takes, and none it does not.

    Read from its signature (CodeRabbit on #67, three rounds): retrying fixed subsets kept dropping one
    callback some host took - Look again's `force`, or `cancelled` so Stop could not interrupt. A host whose
    signature cannot be read is given everything and then nothing; one that takes only the contact ids (the
    oldest kind) is handed them positionally."""
    import inspect  # noqa: PLC0415

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        try:
            return fn(**offered)
        except TypeError:
            return fn()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**offered)
    taken = {k: v for k, v in offered.items() if k in params}
    required = [p for p in params.values() if p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                and p.name not in taken]
    if required:
        return fn(selected, **taken)
    return fn(**taken)


def gather_proposals(ctx: Any, on_progress: Any = None, cancelled: Any = None, force: bool = False) -> EnrichData:
    """Ask the host for proposals and the names of the contacts they are about. A worker's job.

    `on_progress` and `cancelled` are given to a host that takes them, and left out for one that does not.
    `force` (Look again) is passed only when set, so a host that predates it still works."""
    fn = getattr(ctx, "enrich", None)
    if not callable(fn):
        return EnrichData(available=False)
    offered: dict = {"on_progress": on_progress, "cancelled": cancelled}
    if force:
        offered["force"] = True
    result = _call_host(fn, offered, list(getattr(ctx, "selected_ids", []) or []))
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
    notes = [str(n) for n in (getattr(result, "notes", None) or [])]
    return EnrichData(list(proposals), names, apply if callable(apply) else None, notes=notes,
                      **_review_settings(ctx))


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
        #: The look-up belongs to the page: closing this window leaves it running, and opening it again
        #: shows where it got to (or what it found, waiting to be accepted).
        self.run = run_for(parent, ctx)
        self.run.changed.connect(self._from_run)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Contact / detail", "Now", "Suggested", "Where from", "When", "Confidence"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        named(self.tree, "Suggestions", "Details enrichment would add. Tick the ones to keep")
        #: Order 1s 7c: each row's tick as last seen, and the rows the person unticked (Never suggest these).
        self._was: dict[int, bool] = {}
        self._unticked: set[int] = set()
        #: Rows that started ticked (D4): Accept all asks when it would take any other.
        self._started: set[int] = set()
        self.tree.itemChanged.connect(self._ticked_changed)
        # A row that starts unticked (less sure, or one that would replace a value) is turned down by
        # selecting it: it has no tick to take off (review of #75).
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._sync)
        self.select_bar = SelectBar(self.tree, "suggestions")
        self.select_bar.changed.connect(lambda _n: self._sync())
        self.body.addWidget(self.select_bar)
        self.body.addWidget(self.tree, 1)
        self.notes_line = QLabel("")
        self.notes_line.setWordWrap(True)
        self.notes_line.setObjectName("resultsSummary")
        named(self.notes_line, "What the look-up said", "The age window, and what the sources said besides suggestions")
        self.notes_line.hide()
        self.body.addWidget(self.notes_line)

        self.find_button = btn("Look for suggestions", "sparkles", "secondary",
                               "Ask the enrichment sources you switched on for suggestions", "Look for suggestions")
        self.accept_button = btn("Accept selected", "check", "primary", "Keep the ticked suggestions", "Accept selected")
        self.accept_all_button = btn("Accept all", "list-checks", "secondary", "Keep every suggestion", "Accept all")
        self.never_button = btn("Never suggest these", "ban", "secondary",
                                "Untick the suggestions you never want again, or select ones that start "
                                "unticked, then press this: they are not offered for that contact again, from "
                                "any source", "Never suggest these")
        self.undo_button = btn("Undo these", "undo-2", "secondary", "Take back what was just accepted", "Undo these")
        self.undo_button.hide()
        self.stop_button = btn("Stop looking", "square", "secondary",
                               "Stop at the next contact; the suggestions found so far are kept to accept",
                               "Stop looking")
        self.stop_button.hide()
        self.again_button = btn("Look again", "refresh-cw", "secondary",
                                "Ask the internet sources again, even for contacts they were asked about in the "
                                "last 90 days", "Look again")
        self.stop_button.clicked.connect(lambda: self.run.stop())
        self.find_button.clicked.connect(self.find)
        self.again_button.clicked.connect(self.look_again)
        self.accept_button.clicked.connect(self.accept_selected)
        self.accept_all_button.clicked.connect(self.accept_every)
        self.undo_button.clicked.connect(self.undo)
        self.never_button.clicked.connect(self.never_suggest)
        self.add_footer_buttons(self.find_button, self.again_button, self.stop_button, self.accept_button,
                                self.accept_all_button, self.never_button, self.undo_button)
        self.has_source = callable(getattr(ctx, "enrich", None))
        if not self.has_source:
            self.say("Enrichment is not switched on. Turn it on in Settings > Accounts, then come back.")
            self.find_button.setEnabled(False)
            self.again_button.setEnabled(False)
        self._sync()
        if self.run.state != "idle":
            self._from_run()

    # -- finding -------------------------------------------------------------
    def find(self) -> None:
        self.run.start(self.ctx)

    def look_again(self) -> None:
        self.run.start(self.ctx, force=True)

    def _from_run(self) -> None:
        """Show the run's state: going (the window may be closed meanwhile), found, nothing, or stopped."""
        run = self.run
        if run.state == "running":
            self.find_button.setEnabled(False)
            self.again_button.setEnabled(False)
            self.stop_button.setVisible(True)
            p = run.progress
            where = (f"Contact {p['done']:,} of {p['total']:,} - {plural(p['found'], 'suggestion')} so far. "
                     if p else "")
            self.busy(True, f"Looking for suggestions... {where}You can close this window: the look-up carries on, "
                            "and the page says when it is done.")
        elif run.state in ("ready", "empty") and run.data is not None:
            self.stop_button.setVisible(False)
            self._found(run.data)
        elif run.state == "failed":
            self.find_button.setEnabled(self.has_source)
            self.again_button.setEnabled(self.has_source)
            self.busy(False, "Enrichment stopped: " + run.error)

    def _found(self, data: EnrichData) -> None:
        self.find_button.setEnabled(self.has_source)
        self.again_button.setEnabled(self.has_source)
        self.data = data
        self.tree.blockSignals(True)
        self.tree.clear()
        self._was, self._unticked, self._started = {}, set(), set()
        groups, hidden = enrich_rows.layout(data.proposals, self.run.done | self.run.declined, data.hide_below)
        for cid, indexes in groups:
            top = QTreeWidgetItem([data.names.get(cid, f"Contact {cid}")])
            top.setIcon(0, icon("user", ink()))
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            top.setCheckState(0, Qt.CheckState.Unchecked)   # before its rows, so theirs decide its state
            self.tree.addTopLevelItem(top)
            for index in indexes:
                top.addChild(self._row(data, index))
            top.setExpanded(True)
        self.tree.blockSignals(False)
        for col in range(6):
            self.tree.resizeColumnToContents(col)
        shown = sum(len(ix) for _c, ix in groups)
        said = (f"{plural(shown, 'suggestion')} for {plural(len(groups), 'contact')}. Tick what you want to keep."
                if shown else ("Enrichment is not switched on." if not data.available else "No suggestions right now."))
        self.busy(False, said)
        line = enrich_rows.notes_line(data.since, data.notes, hidden)
        self.notes_line.setText(line)
        self.notes_line.setToolTip(line or "The age window, and what the sources said besides suggestions")
        self.notes_line.setVisible(bool(line))
        self._sync()

    def _row(self, data: EnrichData, index: int) -> QTreeWidgetItem:
        p = data.proposals[index]
        old, new = getattr(p, "old", ""), getattr(p, "new", "")
        child = QTreeWidgetItem([str(getattr(p, "field_label", "") or getattr(p, "field_kind", "")),
                                 str(old or ""), "(a photo)" if hasattr(new, "mime") else str(new),
                                 str(getattr(p, "source", "")), when_words(getattr(p, "when", None)),
                                 sure_words(p)])
        child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        ticked = enrich_rows.starts_ticked(p, data.tick_from)
        child.setCheckState(0, Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
        child.setData(0, Qt.ItemDataRole.UserRole, index)
        self._was[index] = ticked
        if ticked:
            self._started.add(index)
        receipt = getattr(p, "receipt", "")
        if receipt:
            child.setToolTip(3, str(receipt))
        if getattr(p, "update", False):
            child.setToolTip(1, "This would replace what the card has now, so it is never ticked for you")
        return child

    def _ticked_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(index, int):
            on = item.checkState(0) == Qt.CheckState.Checked
            if self._was.get(index) and not on:
                self._unticked.add(index)               # the person took a tick off: a "not this"
            elif on:
                self._unticked.discard(index)
            self._was[index] = on
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
        """Every row shown: a hidden one (below Hide suggestions below) is not in "all"."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            out += [top.child(j).data(0, Qt.ItemDataRole.UserRole) for j in range(top.childCount())]
        return out

    def _sync(self) -> None:
        have = bool(self.data and self.data.proposals)
        self.accept_button.setEnabled(have and bool(self.ticked()))
        self.accept_all_button.setEnabled(have and bool(self.every()))
        self.never_button.setEnabled(have and bool(self.turned_down()))

    def accept_selected(self) -> None:
        self._accept(self.ticked())

    def accept_every(self) -> None:
        """D4: Accept all asks once, with the count, when it takes a row that was not ticked for the person."""
        rows = self.every()
        extra = [i for i in rows if i not in self._started]
        if extra:
            from app.ui.accounts import parts       # noqa: PLC0415 - the shared Cancel-default question

            n = len(extra)
            if not parts.confirm(self, "Accept all suggestions?",
                                 f"Keep all {plural(len(rows), 'suggestion')}? {n} of them {'was' if n == 1 else 'were'} "
                                 "not ticked for you: Leasha is less sure of them, or they replace a value the "
                                 "contact already has.", "Accept all"):
                return
        self._accept(rows)

    def turned_down(self) -> set[int]:
        """The rows to never offer again: those the person unticked, and selected rows that are not ticked."""
        picked = {item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()
                  if item.checkState(0) != Qt.CheckState.Checked}
        return set(self._unticked) | {i for i in picked if isinstance(i, int)}

    def never_suggest(self) -> None:
        """D5: remember the turned-down rows, so no source offers those values for that contact again."""
        chosen = sorted(self.turned_down())
        if not chosen or self.data is None:
            return
        from app.contacts.enrich import declines     # noqa: PLC0415

        picked = [self.data.proposals[i] for i in chosen]
        store, region = self.hub.store, getattr(self.hub, "region", "GB")
        self.busy(True, "Remembering what you turned down…")
        self.job(lambda: (chosen, declines.decline(store, picked, region=region)), self._declined)

    def _declined(self, payload: Any) -> None:
        chosen, _added = payload
        gone = set(chosen)
        self.run.declined |= gone                      # kept for when the window is opened again
        self._drop_rows(gone)
        self.busy(False, f"{plural(len(gone), 'suggestion')} will not be offered again. Each can be taken back "
                         "on the contact's page, under Suggestions you turned down.")
        self._sync()

    def _drop_rows(self, gone: set) -> None:
        self._unticked -= gone
        for i in range(self.tree.topLevelItemCount() - 1, -1, -1):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount() - 1, -1, -1):
                if top.child(j).data(0, Qt.ItemDataRole.UserRole) in gone:
                    top.removeChild(top.child(j))
            if top.childCount() == 0:
                self.tree.takeTopLevelItem(i)

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
        self.run.done |= done                               # kept for when the window is opened again
        self._drop_rows(done)
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
