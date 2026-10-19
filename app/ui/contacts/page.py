"""The Contacts page proper: the toolbar, the sidebar, the two views, the read view, the editor and the history.

Layer: L5

Order 1k section 6. `app/ui/contacts_view.py` builds this lazily the first time the page is
shown. Every read and write of the hub is a worker (rule 5, `loader.run_task`); what comes back
is a signal on the UI thread. The data flows one way: `reload` reads a `Snapshot` (rows for the
current filter, the sidebar's numbers) and the model, sidebar and editor are filled from it;
selecting one contact reads its `Detail` for the read view and the editor; every edit is an action
here that runs on a worker and ends in a reload. The tool dialogs are not here - the page opens them
through `openers`.

**Reading is not editing** (UI phase B, 2026-10-13). A pick shows the read view (`reader.py`);
Edit, Enter, a double-click or F2 switches to the editor, and Save or Cancel come back. A contact
with a decision waiting (deleted elsewhere, changed in two places) opens in the editor, where the
buttons for it are. Both lists answer the one list behaviour (`widgets/list_keys.py`): Enter and a
double-click edit, F2 edits, Delete moves the pick to the bin, Ctrl+C copies the email addresses.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QHeaderView, QInputDialog, QLabel, QListView,
                             QMessageBox, QSplitter, QStackedWidget, QTableView, QVBoxLayout, QWidget)

from app.contacts.card import Card, Photo
from app.contacts.hub import ContactFilter
from app.ui.contacts import loader, openers
from app.ui.contacts.bulkbar import BulkBar
from app.ui.contacts.delegates import CARD_SIZE, CardDelegate, ChipsDelegate, NameDelegate
from app.ui.contacts.editor import ContactEditor, SaveRequest
from app.ui.contacts.enrich_panel import EnrichPanel
from app.ui.contacts.history import HistoryPanel
from app.ui.contacts.index_panel import IndexPanel
from app.ui.contacts.logic import (COLUMNS, STATE_SORT, STATE_VIEW, TAG_COLOURS, Entry, build_entries,
                                    filter_for, parse_sort, describe_entry)
from app.ui.contacts.model import ContactModel
from app.ui.contacts.photos import PhotoCache
from app.ui.contacts.parts import retint_tools
from app.ui.contacts.reader import ContactReader
from app.ui.contacts.sidebar import ContactSidebar
from app.ui.contacts.smart import SmartDialog
from app.ui.contacts.toolbar import ContactToolbar
from app.ui.presenter.link_marks import link_marks
from app.ui.theme import theme_colours
from app.ui.widgets import list_keys
from app.ui.widgets.menus import global_point
from app.ui.widgets.row_ring import install_row_ring

__all__ = ["ContactsPage"]

#: How long typing settles before the list is read again.
DEBOUNCE_MS = 220
COLUMN_WIDTHS = (220, 150, 140, 200, 130, 190, 150, 150, 120, 150)


class ContactsPage(QWidget):
    error = Signal(object)
    #: A line for the window's toast ("Moved 3 contacts to the bin").
    notice = Signal(str)
    loaded = Signal()                                   # a read landed (tests wait on this)

    def __init__(self, store: Any, hub: Any, accounts: Any, *, service: Any = None, host: Any = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self.hub = hub
        self.accounts = accounts
        self.service = service
        self.host = host
        self._gen = 0
        self._detail_gen = 0
        self._entry = Entry("all", "all", "All", "contact", section="Contacts")
        self._snap: Optional[loader.Snapshot] = None
        self._sort = "name"
        self._creating = False
        self._busy = 0
        self._mode = "list"
        self._pending_pick: Optional[int] = None    # Back/Forward's contact, until a read lands
        self.setObjectName("contactsPage")

        self.photos = PhotoCache(hub, parent=self)
        self.model = ContactModel(self)
        self.toolbar = ContactToolbar()
        self.sidebar = ContactSidebar()
        self.bulk = BulkBar()
        self.bulk.hide()
        self.table = self._make_table()
        self.cards = self._make_cards()
        self.empty = QLabel("")
        self.empty.setObjectName("resultsSummary")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setAccessibleName("Nothing to show")
        self.views = QStackedWidget()
        for w in (self.table, self.cards, self.empty):
            self.views.addWidget(w)
        self.cards.setSelectionModel(self.table.selectionModel())

        # 2026-10-15: **the editor is built when it is first needed** (`editor`, below): a contact picked,
        # Ctrl+N, Edit. It was the largest part of this page's first visit - 70-90 ms of widgets on the
        # window's thread, measured in `gui_mainwindow`, for a pane nobody sees until a contact is picked
        # (`test_navigation_is_instant.py`). Until then `_editor` is None and the page answers for it as
        # an empty, disabled editor would. The two panels it drives are made here, because the read view
        # shows them from the start, and lent to it when it is built.
        self._editor: Optional[ContactEditor] = None
        self._editor_setup: Optional[tuple] = None        # the last `_landed`'s names and `configure` args
        self._colours: Optional[dict[str, str]] = None    # the last `retint`'s, for an editor built later
        self.enrich_panel = EnrichPanel()
        self.enrich_panel.error.connect(self.error.emit)
        self.index_panel = IndexPanel()
        self.index_panel.error.connect(self.error.emit)
        self.reader = ContactReader()
        # The mail, files and photos about this person, and enrichment, are read with the contact:
        # under the read view. The editor still fills and clears them.
        self.reader.add_panels(self.enrich_panel, self.index_panel)
        self.index_panel.configure(hub)
        # IA-6: "See all photos" / "See mail from them" - the window opens the page.
        self.index_panel.photos_requested.connect(
            lambda box: (self._hook("show_photos_for") or (lambda _b: None))(box))
        self.index_panel.mail_requested.connect(
            lambda box: (self._hook("show_mail_for") or (lambda _b: None))(box))
        self.enrich_panel.configure(hub)
        self.enrich_panel.enrich_requested.connect(self.enrich_one)
        self.history = HistoryPanel()
        self.placeholder = QLabel("Pick a contact to see it here.\nPress Ctrl+N for a new one.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setObjectName("resultsSummary")
        self.placeholder.setAccessibleName("No contact picked")
        self.right = QStackedWidget()
        for w in (self.placeholder, self.reader, self.history):    # the editor goes in between when built
            self.right.addWidget(w)

        centre = QWidget()
        col = QVBoxLayout(centre)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self.bulk)
        col.addWidget(self.views, 1)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        for w, stretch in ((self.sidebar, 0), (centre, 1), (self.right, 0)):
            self.split.addWidget(w)
            self.split.setStretchFactor(self.split.indexOf(w), stretch)
        self.split.setSizes([220, 520, 420])
        self.split.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.split, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(lambda: self.reload(full=False))
        self._wire()
        self._shortcuts()
        self.retint(theme_colours())
        self.set_view(self._state(STATE_VIEW) or "list", remember=False)
        col_, desc = parse_sort(self._state(STATE_SORT))
        self.table.horizontalHeader().setSortIndicator(col_, Qt.SortOrder.DescendingOrder if desc
                                                      else Qt.SortOrder.AscendingOrder)

    # ------------------------------------------------------------------ build
    def _make_table(self) -> QTableView:
        t = QTableView()
        install_row_ring(t)
        t.setObjectName("contactsTable")
        t.setModel(self.model)
        t.setAccessibleName("Contacts")
        t.setToolTip("Your contacts. Enter opens one, Delete moves it to the bin")
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setShowGrid(False)
        t.setAlternatingRowColors(False)
        t.setWordWrap(False)
        t.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        t.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        t.verticalHeader().setDefaultSectionSize(34)
        head = t.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        head.setStretchLastSection(True)
        head.setSectionsMovable(False)
        head.setSortIndicatorShown(True)
        for i, w in enumerate(COLUMN_WIDTHS):
            t.setColumnWidth(i, w)
        self.name_delegate = NameDelegate(self.photos, t)
        self.chips_delegate = ChipsDelegate(self.photos, t)
        t.setItemDelegateForColumn(0, self.name_delegate)
        for col in (5, 6, 7):
            t.setItemDelegateForColumn(col, self.chips_delegate)
        t.setSortingEnabled(True)
        corner = t.findChild(QAbstractButton, "qt_tableview_cornerbutton")
        if corner is not None:
            corner.setAccessibleName("Pick every contact")
            corner.setToolTip("Pick every contact")
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos: self._context_menu(t, pos))
        return t

    def _make_cards(self) -> QListView:
        v = QListView()
        v.setObjectName("contactsCards")
        v.setModel(self.model)
        v.setModelColumn(0)
        v.setViewMode(QListView.ViewMode.IconMode)
        v.setResizeMode(QListView.ResizeMode.Adjust)
        v.setMovement(QListView.Movement.Static)
        v.setFlow(QListView.Flow.LeftToRight)
        v.setWrapping(True)
        v.setUniformItemSizes(True)
        v.setSpacing(0)
        v.setGridSize(CARD_SIZE)
        v.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        v.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        v.customContextMenuRequested.connect(lambda pos: self._context_menu(v, pos))
        v.setAccessibleName("Contact cards")
        v.setToolTip("Your contacts as business cards. Enter opens one, Delete moves it to the bin")
        self.card_delegate = CardDelegate(self.photos, v)
        v.setItemDelegate(self.card_delegate)
        return v

    def _wire(self) -> None:
        sm = self.table.selectionModel()
        sm.selectionChanged.connect(self._selection_changed)
        # `activated` alone, through the one list behaviour: Qt sends it for a double-click and
        # `list_keys` sends it for Enter, so wiring `doubleClicked` as well opened the contact twice
        # for one double-click (2026-10-12).
        for view in (self.table, self.cards):
            list_keys.install(view, default=self._activated, delete=self.delete_selected,
                              rename=self.edit_current, copy=self.copy_picked)
        self.reader.edit_requested.connect(self.edit_current)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._sort_changed)
        self.photos.loaded.connect(lambda _c: (self.table.viewport().update(), self.cards.viewport().update()))
        tb = self.toolbar
        tb.new_requested.connect(self.new_contact)
        tb.view_changed.connect(self.set_view)
        tb.tool_requested.connect(self.open_tool)
        tb.history_toggled.connect(self._history_toggled)
        tb.bin_requested.connect(lambda: self.sidebar.select_key("bin"))
        tb.favourite_requested.connect(lambda: self._bulk("favourite", None))
        tb.text_changed.connect(lambda _t: self._timer.start())
        sb = self.sidebar
        sb.picked.connect(self._picked)
        sb.opener_requested.connect(self.open_tool)
        sb.smart_new.connect(lambda: self._smart_dialog(None))
        sb.smart_edit.connect(self._smart_dialog)
        sb.smart_delete.connect(self._smart_delete)
        sb.tag_colour.connect(self._tag_colour)
        sb.tag_delete.connect(self._tag_delete)
        sb.group_rename.connect(self._group_rename)
        self.bulk.action.connect(self._bulk)
        self.history.undo_requested.connect(self._undo_entry)

    # ------------------------------------------------------------- the editor, built when needed
    @property
    def editor(self) -> ContactEditor:
        """The editor, built and wired the first time anything asks for it (2026-10-15, see `__init__`)."""
        if self._editor is None:
            ed = ContactEditor(own_panels=False, enrich_panel=self.enrich_panel, index_panel=self.index_panel)
            self._editor = ed
            self._wire_editor(ed)
            if self._colours is not None and self._colours != theme_colours():
                ed.retint(self._colours)
            if self._editor_setup is not None:
                names, configure = self._editor_setup
                ed.set_account_names(names)
                ed.configure(*configure)
            self.right.insertWidget(self.right.indexOf(self.history), ed)
        return self._editor

    def editor_built(self) -> bool:
        return self._editor is not None

    def _editor_dirty(self) -> bool:
        """`editor.is_dirty()` without building it: an editor not yet built has nothing typed in it."""
        return self._editor is not None and self._editor.is_dirty()

    def _save_if_dirty(self) -> None:
        if self._editor is not None:
            self._editor.save_if_dirty()

    def _wire_editor(self, ed: ContactEditor) -> None:
        ed.save_requested.connect(self._save)
        ed.cancel_requested.connect(self._cancel)
        ed.acknowledge_requested.connect(self._acknowledge)
        ed.kept_in_requested.connect(self._kept_in)
        ed.delete_here_requested.connect(self._delete_here_too)
        ed.put_back_requested.connect(self._put_back)
        ed.resolve_requested.connect(self._resolve_conflict)
        ed.person_photo_requested.connect(self._person_photo)
        ed.error.connect(self.error.emit)

    def _shortcuts(self) -> None:
        def key(seq: str, slot: Callable, widget: Optional[QWidget] = None,
                ctx: Qt.ShortcutContext = Qt.ShortcutContext.WidgetWithChildrenShortcut) -> QShortcut:
            sc = QShortcut(QKeySequence(seq), widget or self)
            sc.setContext(ctx)
            sc.activated.connect(slot)
            return sc

        self.shortcuts = [
            key("/", self.toolbar.focus_filter),
            key("Ctrl+N", self.new_contact),
            key("Ctrl+Z", self.undo_last),
            key("Ctrl+S", self._save_if_dirty),
            key("Ctrl+A", self.select_all, self.table, Qt.ShortcutContext.WidgetShortcut),
        ]

    # ------------------------------------------------------------------ state
    def _state(self, key: str) -> str:
        try:
            return str(self._store.get_state(key) or "")
        except Exception:                                 # noqa: BLE001 - a default is fine
            return ""

    def _remember(self, key: str, value: str) -> None:
        from app.ui.state_writes import save_state

        save_state(self._store, key, value)

    def view_mode(self) -> str:
        return "cards" if self.views.currentWidget() is self.cards else "list"

    def set_view(self, mode: str, *, remember: bool = True) -> None:
        mode = "cards" if mode == "cards" else "list"
        self.toolbar.set_view(mode)
        if self._snap is not None and not self.model.rowCount():
            self.views.setCurrentWidget(self.empty)
        else:
            self.views.setCurrentWidget(self.cards if mode == "cards" else self.table)
        self._mode = mode
        if remember:
            self._remember(STATE_VIEW, mode)

    def _show_list(self) -> None:
        self.views.setCurrentWidget(self.cards if self._mode == "cards" else self.table)

    def _sort_changed(self, column: int, order: Qt.SortOrder) -> None:
        self._remember(STATE_SORT, f"{COLUMNS[column][0]}:{'desc' if order == Qt.SortOrder.DescendingOrder else 'asc'}")

    def retint(self, colours: dict[str, str]) -> None:
        self.model.retint(colours)
        for d in (self.name_delegate, self.chips_delegate, self.card_delegate):
            d.retint(colours)
        self.sidebar.retint(colours)
        self._colours = dict(colours)
        if self._editor is not None:
            self._editor.retint(colours)
        self.reader.retint(colours)
        retint_tools(self, colours)
        self.toolbar.retint(colours)
        # The theme's sheet names QTableWidget and not QTableView, so the table gets its
        # surface, its edge and its selected row from the same tokens here.
        self.table.setStyleSheet(
            f"#contactsTable {{ background: {colours.get('surface', '#ffffff')}; "
            f"border: 1px solid {colours.get('border', '#cccccc')}; border-radius: 6px; }} "
            f"#contactsTable::item:selected {{ background: {colours.get('accent_soft', '#dddddd')}; "
            f"color: {colours.get('text', '#000000')}; }} "
            f"#contactsTable::item:hover {{ background: {colours.get('surface_hover', '#eeeeee')}; }}")
        self.table.viewport().update()
        self.cards.viewport().update()

    # ------------------------------------------------------------------- reads
    def current_filter(self) -> ContactFilter:
        return filter_for(self._entry, self.toolbar.filter.text(), self._sort)

    def refresh(self) -> None:
        """The page came forward, or a tool changed the book."""
        self.reload(full=True)

    def reload(self, *, full: bool = True, select: Optional[list[int]] = None,
               then: Optional[Callable[[], Any]] = None) -> None:
        self._gen += 1
        gen = self._gen
        flt = self.current_filter()
        keep = select if select is not None else self.selected_ids()
        hub, accounts = self.hub, self.accounts
        self._busy += 1

        def done(snap: loader.Snapshot) -> None:
            self._busy -= 1
            if gen != self._gen:
                return
            self._landed(snap, keep, full)
            if then is not None:
                then()
            self.loaded.emit()

        def failed(err: Any) -> None:
            self._busy -= 1
            self.error.emit(err)

        loader.run_task(self, lambda: loader.load_snapshot(hub, accounts, flt, full=full), done, failed)

    def _landed(self, snap: loader.Snapshot, keep: list[int], full: bool) -> None:
        if full:
            self._snap = snap
            if snap.region:
                self.hub.region = snap.region
            self.model.set_accounts(snap.accounts)
            self.model.set_tag_colours(snap.tags)
            self.sidebar.set_entries(build_entries(snap.counts, snap.groups, snap.tags, snap.accounts,
                                                    snap.smarts, snap.account_counts))
            writable = [a for a in snap.accounts if a.syncs_contacts]
            self._editor_setup = ({a.id: (a.name or a.address) for a in snap.accounts},
                                  (writable, [g[1] for g in snap.groups], snap.tags, self.hub.region,
                                   self._hook("pick_photo_from_person") is not None))
            if self._editor is not None:
                self._editor.set_account_names(self._editor_setup[0])
                self._editor.configure(*self._editor_setup[1])
            self.bulk.set_choices([g[1] for g in snap.groups], [t[1] for t in snap.tags], writable)
        elif self._snap is not None:
            self._snap.rows = snap.rows
        self.toolbar.favourite_button.setEnabled(self._entry.kind != "bin")
        self.bulk.set_mode(bin_view=self._entry.kind == "bin")
        sm = self.table.selectionModel()
        sm.blockSignals(True)
        self.model.set_rows(snap.rows)
        pick, self._pending_pick = self._pending_pick, None
        if pick is not None and self._may_pick(pick):
            keep = [pick]
        sm.clearSelection()
        for cid in keep:
            row = self.model.position_of(cid)
            if row >= 0:
                sm.select(self.model.index(row, 0),
                          QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        sm.blockSignals(False)
        n = len(snap.rows)
        total = (self._snap.counts.get("all", n) if self._snap else n)
        filtered = bool(self.toolbar.filter.text().strip()) or self._entry.kind != "all"
        self.toolbar.count.setText(f"{n} of {total}" if filtered and self._entry.kind != "bin" else f"{n}")
        self._empty_state(n, total)
        self._selection_changed()

    def _empty_state(self, shown: int, total: int) -> None:
        if shown:
            self._show_list()
            return
        if self.toolbar.filter.text().strip():
            self.empty.setText("No contact matches that. Clear the box to see everyone.")
        elif self._entry.kind == "bin":
            self.empty.setText("The bin is empty. Deleted contacts wait here for 30 days.")
        elif total == 0:
            self.empty.setText("No contacts yet.\nPress Ctrl+N to add one, or use Import to bring in a file.")
        else:
            self.empty.setText("Nothing here.")
        self.views.setCurrentWidget(self.empty)

    # --------------------------------------------------------------- selection
    def selected_ids(self) -> list[int]:
        rows: set[int] = set()
        for span in self.table.selectionModel().selection():
            rows.update(range(span.top(), span.bottom() + 1))
        return [r.id for r in (self.model.row_at(i) for i in sorted(rows)) if r is not None]

    def select_ids(self, ids: list[int]) -> None:
        sm = self.table.selectionModel()
        sm.clearSelection()
        for cid in ids:
            row = self.model.position_of(cid)
            if row >= 0:
                sm.select(self.model.index(row, 0),
                          QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        if ids and self.model.position_of(ids[0]) >= 0:
            sm.setCurrentIndex(self.model.index(self.model.position_of(ids[0]), 0),
                               QItemSelectionModel.SelectionFlag.NoUpdate)

    def current_contact_id(self) -> Optional[int]:
        """The one contact picked, for the window's Back/Forward; None for none or several."""
        ids = self.selected_ids()
        return ids[0] if len(ids) == 1 else None

    def _may_pick(self, cid: int) -> bool:
        """Back/Forward may pick `cid`: listed now, not in the bin, no unsaved edit in the way."""
        return (self._entry.kind != "bin" and self.model.position_of(cid) >= 0
                and not self._editor_dirty())

    def show_contact_id(self, cid: Optional[int]) -> None:
        """Back/Forward: pick `cid` again from the rows already read - never a read of its own.

        A read in flight (the page came forward and refreshes) would put back the old pick as it
        lands, so the contact waits for it instead. One deleted or binned meanwhile is not in the
        rows: the page stays as it is, with no dialog."""
        if cid is None:
            return
        if self._busy or self._snap is None:
            self._pending_pick = int(cid)
            return
        if self._may_pick(int(cid)) and self.current_contact_id() != int(cid):
            self.select_ids([int(cid)])
            self.table.scrollTo(self.model.index(self.model.position_of(int(cid)), 0))

    def show_contact(self, cid: int) -> None:
        """Search's "Open contact": the full list, this contact picked and its editor in front."""
        if self._editor_dirty() and not self._confirm_discard():
            return
        self._creating = False
        self.toolbar.history_button.setChecked(False)
        self.toolbar.filter.blockSignals(True)
        self.toolbar.filter.clear()
        self.toolbar.filter.blockSignals(False)
        self._entry = Entry("all", "all", "All", "contact", section="Contacts")
        self.sidebar.select_key("all", emit=False)

        def reveal() -> None:
            at = self.model.position_of(cid)
            if at >= 0:
                self.table.scrollTo(self.model.index(at, 0))
                self.cards.scrollTo(self.model.index(at, 0))
            self._open(cid)

        self.reload(select=[cid], then=reveal)

    def _selection_changed(self, *_a: object) -> None:
        ids = self.selected_ids()
        self.toolbar.set_picked(len(ids))
        rows = [r for r in (self.model.row_for_id(i) for i in ids) if r is not None]
        self.bulk.set_rows(rows)
        in_bin = self._entry.kind == "bin"
        self.bulk.setVisible(len(ids) > 1 or in_bin)
        if self._creating and not ids:
            return
        if len(ids) == 1:
            ed = self._editor
            if ed is None or ed.cid != ids[0] or not ed.isEnabled():
                if self._editor_dirty() and not self._confirm_discard():
                    return
                self._creating = False
                self._open(ids[0])
        elif len(ids) > 1:
            self._creating = False
            self.placeholder.setText(f"{len(ids)} contacts picked.\nUse the bar above the list.")
            self.right.setCurrentWidget(self.placeholder)
        else:
            if not self._creating and not self._editor_dirty():
                if self._editor is not None:
                    self._editor.clear()
                self.reader.clear()
                self.history.clear()
                self.placeholder.setText("Pick a contact to see it here.\nPress Ctrl+N for a new one.")
                self.right.setCurrentWidget(self.history if self.toolbar.history_button.isChecked()
                                            else self.placeholder)

    def _confirm_discard(self) -> bool:
        """Unsaved edits: throw them away (True) or keep editing (False, and the pick goes back)."""
        if self._ask_discard():
            return True
        self.select_ids([self.editor.cid] if self.editor.cid is not None else [])
        return False

    def _ask_discard(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Unsaved changes")
        box.setText("This contact has changes you have not saved.")
        discard = box.addButton("Discard them", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Keep editing", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is discard

    def _activated(self, _index: QModelIndex) -> None:
        """Enter or a double-click on a contact: edit it."""
        self.edit_current()

    def edit_current(self) -> None:
        """Edit, F2, Enter: the contact on show goes from the read view to the editor."""
        if self._editor is not None and self._editor.isEnabled() and self._entry.kind != "bin":
            self.right.setCurrentWidget(self.editor)
            self.toolbar.history_button.setChecked(False)
            self.editor.focus_first()

    def is_editing(self) -> bool:
        return self._editor is not None and self.right.currentWidget() is self._editor

    def _show_contact_pane(self) -> None:
        """The read view, unless a new contact or unsaved edits are in the editor, or it needs a decision."""
        if self._creating or self._editor_dirty() or (self._editor is not None and self._editor.needs_attention()):
            self.right.setCurrentWidget(self.editor)
        elif self.reader.cid is not None:
            self.right.setCurrentWidget(self.reader)
        else:
            self.right.setCurrentWidget(self.placeholder)

    def copy_picked(self) -> None:
        """Ctrl+C on the list: the picked contacts' email addresses."""
        from app.ui.contacts import quick

        quick.copy_picked(self)

    def _picked(self, entry: Entry) -> None:
        if self._editor_dirty() and not self._confirm_discard():
            self.sidebar.select_key(self._entry.key, emit=False)
            return
        self._entry = entry
        self._creating = False
        self.reload(full=False, select=[])

    # ------------------------------------------------------------------ editor
    def _open(self, cid: int) -> None:
        self._detail_gen += 1
        gen = self._detail_gen
        hub = self.hub

        def done(d: Any) -> None:
            if gen != self._detail_gen:
                return
            if d is None:
                if self._editor is not None:
                    self._editor.clear()
                self.reader.clear()
                return
            self.editor.show_detail(d)
            in_bin = self._entry.kind == "bin"
            if in_bin:
                self.editor.setEnabled(False)
            names = self._names()
            marks = link_marks(getattr(d, "link_states", {}) or {}, names, getattr(d, "refused", {}) or {})
            self.reader.show_contact(d.id, d.card, kept_in=[names[a] for a in d.kept_in if names.get(a)],
                                     tags=d.tags, editable=not in_bin, kept_marks=marks)
            if self.toolbar.history_button.isChecked():
                self._load_history(cid, d.row.display_name if d.row else "")
            elif in_bin:
                self.right.setCurrentWidget(self.reader)
            else:
                self._show_contact_pane()

        accounts = self.accounts
        loader.run_task(self, lambda: loader.load_detail(hub, cid, accounts), done)

    def new_contact(self) -> None:
        if self._editor_dirty() and not self._confirm_discard():
            return
        default = self._snap.default_account_id if self._snap is not None else None
        self._creating = True
        self.table.selectionModel().clearSelection()
        self.toolbar.history_button.setChecked(False)
        self.editor.start_new(default)
        self.right.setCurrentWidget(self.editor)

    def _save(self, req: SaveRequest) -> None:
        card = req.card
        if not card.scalars and not card.fields and not card.photo:
            self.notice.emit("Type a name, an email or a phone number first.")
            return
        hub = self.hub

        def work() -> int:
            if req.cid is None:
                cid = hub.create(card, kept_in=req.kept_in)
            else:
                cid = req.cid
                hub.update(cid, card)
            known = {t[1] for t in hub.tags()}
            for n, name in enumerate(sorted(set(req.tags) - set(req.original_tags))):
                if name not in known:
                    hub.create_tag(name, TAG_COLOURS[(len(known) + n) % len(TAG_COLOURS)])
                hub.tag([cid], name, on=True)
            for name in sorted(set(req.original_tags) - set(req.tags)):
                hub.tag([cid], name, on=False)
            return cid

        def done(cid: int) -> None:
            self.editor.mark_saved()
            self._creating = False
            self.photos.forget(cid)
            self.notice.emit("Saved.")
            if req.cid is None:
                self.toolbar.filter.blockSignals(True)
                self.toolbar.filter.clear()
                self.toolbar.filter.blockSignals(False)
                self._entry = Entry("all", "all", "All", "contact", section="Contacts")
                self.sidebar.select_key("all", emit=False)
            self.reload(select=[cid], then=lambda: self._open(cid))

        loader.run_task(self, work, done)

    def _cancel(self) -> None:
        if self._creating or self.editor.cid is None:
            self._creating = False
            self.editor.clear()
            self._selection_changed()
            self.right.setCurrentWidget(self.placeholder)
        else:
            self._open(self.editor.cid)

    def _acknowledge(self, cid: int) -> None:
        hub = self.hub
        loader.run_task(self, lambda: hub.acknowledge_conflicts(cid),
                        lambda _r: self.reload(then=lambda: self._open(cid)))

    # ------------------------------------------- deleted elsewhere (D7.1) and clashes (10k)
    def _delete_here_too(self, cid: int) -> None:
        if not self.confirm("Delete this contact here too?",
                            "It goes to the Bin, where it waits 30 days, and it leaves its other accounts "
                            "at the next sync. Ctrl+Z undoes it.", "Delete here too"):
            return
        hub = self.hub

        def work() -> bool:
            return bool(hub.confirm_deleted_elsewhere(cid))

        loader.run_task(self, work, lambda _ok: (self.notice.emit("Moved to the bin. It leaves its other "
                                                                  "accounts at the next sync."),
                                                 self.reload(select=[])))

    def _put_back(self, cid: int, account_id: int) -> None:
        hub = self.hub
        name = self._names().get(account_id, "that account")
        loader.run_task(self, lambda: hub.restore_to_account(cid, account_id),
                        lambda _r: (self.notice.emit(f"It will be written to {name} again at the next sync."),
                                    self.reload(select=[cid], then=lambda: self._reopen([cid]))))

    def _resolve_conflict(self, cid: int, account_id: int, choose: str) -> None:
        service = self.service
        ask = getattr(service, "resolve_conflict", None)
        if not callable(ask):
            self.notice.emit("This needs the account service, which is not connected here. Nothing was changed.")
            return

        def done(out: Any) -> None:
            if out is None:
                self.notice.emit("That account is syncing right now. Try again when it has finished.")
            elif getattr(out, "status", "ok") not in ("ok", "running"):
                self.notice.emit("The choice could not be finished. See the sync log in Settings > Accounts.")
            else:
                self.notice.emit("Done: " + ("kept Leasha's values." if choose == "local" else "took the account's values."))
            self.photos.forget(cid)
            self.reload(select=[cid], then=lambda: self._reopen([cid]))

        loader.run_task(self, lambda: ask(cid, account_id, choose), done)

    def _kept_in(self, cid: int, account_ids: list, confirmed: bool) -> None:
        hub = self.hub

        def work() -> dict:
            out = hub.set_kept_in(cid, account_ids, confirmed_removals=confirmed)
            out["now"] = hub.kept_in(cid)
            return out

        def done(out: dict) -> None:
            self.editor.set_kept(out["now"])
            names = self._names()
            self.reader.set_kept([names[a] for a in out["now"] if names.get(a)])
            self.reload(then=None)

        loader.run_task(self, work, done)

    def _hook(self, name: str) -> Optional[Callable]:
        for source in (self.host, self.service):
            fn = getattr(source, name, None)
            if callable(fn):
                return fn
        return None

    def _person_photo(self, cid: int) -> None:
        fn = self._hook("pick_photo_from_person")
        if fn is None:
            return
        got = fn(cid if cid >= 0 else None)
        photo = None
        if isinstance(got, Photo):
            photo = got
        elif isinstance(got, tuple) and len(got) == 2:
            photo = Photo(str(got[0]), bytes(got[1]))
        elif isinstance(got, (bytes, bytearray)):
            photo = Photo("image/jpeg", bytes(got))
        elif isinstance(got, str) and got:
            loader.run_task(self.editor, lambda: loader.read_photo(got),
                            lambda r: self.editor.set_photo(Photo(r[0], r[1])))
            return
        if photo is not None:
            self.editor.set_photo(photo)

    # ----------------------------------------------------------------- history
    def _history_toggled(self, on: bool) -> None:
        ids = self.selected_ids()
        if on:
            if len(ids) == 1:
                row = self.model.row_for_id(ids[0])
                self._load_history(ids[0], row.display_name if row else "")
            else:
                self.history.clear("Pick one contact to see its history." if not ids else
                                   "History shows one contact at a time.")
                self.right.setCurrentWidget(self.history)
        elif self._editor is not None and self._editor.isEnabled():
            self._show_contact_pane()
        else:
            self.right.setCurrentWidget(self.placeholder)

    def _load_history(self, cid: int, name: str) -> None:
        hub = self.hub
        accounts = self.accounts

        def work() -> tuple:
            return loader.load_history(hub, cid), {a.id: (a.name or a.address) for a in accounts.list()}

        def done(res: tuple) -> None:
            self.history.show_history(cid, name, res[0], res[1])
            if self.toolbar.history_button.isChecked():
                self.right.setCurrentWidget(self.history)

        loader.run_task(self, work, done)

    def _undo_entry(self, jid: int) -> None:
        hub = self.hub
        self._after_undo(lambda: hub.undo_entry(jid), "Undone.")

    def undo_last(self) -> None:
        """Ctrl+Z: the person's newest change (the whole bulk action if it was one)."""
        hub = self.hub

        def work() -> tuple:
            entry, res = loader.undo_last(hub)
            return res, entry

        self._after_undo(work, None)

    def _after_undo(self, work: Callable, message: Optional[str]) -> None:
        service = self.service
        names = self._names()

        def run() -> Any:
            out = work()
            res = out[0] if isinstance(out, tuple) else out
            if res is not None and res.accounts and callable(getattr(service, "push_accounts", None)):
                service.push_accounts(sorted(res.accounts))
            return out

        def done(out: Any) -> None:
            res = out[0] if isinstance(out, tuple) else out
            if res is None:
                self.notice.emit("Nothing to undo.")
                return
            text = message or "Undid your last change."
            if isinstance(out, tuple) and out[1] is not None:
                text = "Undid: " + describe_entry(out[1], names)[1] + "."
            if res.skipped:
                text += f" {res.skipped} could not be undone because the contact changed since."
            self.notice.emit(text)
            self.photos.forget()
            ids = self.selected_ids()
            self.reload(then=lambda: self._reopen(ids))

        loader.run_task(self, run, done)

    def _reopen(self, ids: list[int]) -> None:
        if len(ids) == 1 and self.model.position_of(ids[0]) >= 0:
            self._open(ids[0])
            if self.toolbar.history_button.isChecked():
                row = self.model.row_for_id(ids[0])
                self._load_history(ids[0], row.display_name if row else "")

    # ------------------------------------------------------------------- bulk
    def delete_selected(self) -> None:
        if self._entry.kind == "bin":
            return
        self._bulk("delete", None)

    def _names(self) -> dict[int, str]:
        return self.model.account_names()

    def _ask_text(self, title: str, label: str) -> str:
        text, ok = QInputDialog.getText(self, title, label)
        return text.strip() if ok else ""

    def _bulk(self, name: str, arg: Any) -> None:
        ids = self.selected_ids()
        hub = self.hub
        if name == "export":
            return self.open_tool("export")
        if name == "empty_bin":
            return self._empty_bin()
        if not ids:
            return
        batch = uuid.uuid4().hex
        rows = [r for r in (self.model.row_for_id(i) for i in ids) if r is not None]
        verb = f"{len(ids)} contact{'s' if len(ids) != 1 else ''}"
        work: Callable[[], Any]
        message = ""
        if name == "delete":
            def work() -> int:
                return sum(1 for i in ids if hub.delete(i, batch_id=batch))
            message = f"Moved {verb} to the bin. Ctrl+Z undoes it."
        elif name == "restore":
            def work() -> int:
                return sum(1 for i in ids if hub.restore(i))
            message = f"Restored {verb}."
        elif name == "favourite":
            value = not all(r.favourite for r in rows)

            def work() -> int:
                return hub.set_favourite(ids, value, batch_id=batch)
            message = f"{'Marked' if value else 'Unmarked'} {verb} as favourite."
        elif name == "company":
            company = self._ask_text("Set company", f"Company for {verb}:")
            if not company:
                return

            def work() -> int:
                return hub.set_company(ids, company, batch_id=batch)
            message = f"Set the company of {verb}."
        elif name in ("group_add", "group_remove"):
            group = arg or self._ask_text("New group", "Name of the new group:")
            if not group:
                return
            fn = hub.add_to_group if name == "group_add" else hub.remove_from_group

            def work() -> int:
                return fn(ids, group, batch_id=batch)
            message = f"{'Added' if name == 'group_add' else 'Took'} {verb} {'to' if name == 'group_add' else 'out of'} {group}."
        elif name in ("tag_add", "tag_remove"):
            tag = arg or self._ask_text("New tag", "Name of the new tag:")
            if not tag:
                return
            on = name == "tag_add"

            def work() -> int:
                known = {t[1] for t in hub.tags()}
                if on and tag not in known:
                    hub.create_tag(tag, TAG_COLOURS[len(known) % len(TAG_COLOURS)])
                return hub.tag(ids, tag, on=on, batch_id=batch)
            message = f"{'Tagged' if on else 'Untagged'} {verb} {tag}."
        elif name == "account_add":
            account = int(arg)

            def work() -> int:
                n = 0
                for i in ids:
                    got = hub.set_kept_in(i, sorted(set(hub.kept_in(i)) | {account}), batch_id=batch)
                    n += 1 if got["added"] else 0
                return n
            message = f"Keeping {verb} in {self._names().get(account, 'that account')} as well."
        elif name == "account_remove":
            account = int(arg)
            label = self._names().get(account, "that account")
            if not self.confirm(f"Remove {verb} from {label}?",
                                f"They will be deleted from {label} the next time it syncs. "
                                "They stay in Leasha and in the other accounts they are kept in.",
                                "Remove them"):
                return

            def work() -> int:
                n = 0
                for i in ids:
                    got = hub.set_kept_in(i, sorted(set(hub.kept_in(i)) - {account}),
                                          confirmed_removals=True, batch_id=batch)
                    n += 1 if got["removed"] else 0
                return n
            message = f"Took {verb} out of {label}."
        else:
            return
        loader.run_task(self, work, lambda _n: self._bulk_done(message, ids))

    def _bulk_done(self, message: str, ids: list[int]) -> None:
        self.notice.emit(message)
        self.reload(select=ids if self._entry.kind != "bin" else [])

    def confirm(self, title: str, detail: str, yes: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        # The question once, as the box's main words; the window's title bar names the page. It used
        # to carry the question too, so the box said it twice (2026-10-12).
        box.setWindowTitle("Contacts")
        box.setText(title)
        box.setInformativeText(detail)
        go = box.addButton(yes, QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        # Enter on a box that deletes must not delete: Cancel is the default, as `accounts.parts.confirm`.
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is go

    def _empty_bin(self) -> None:
        if not self.confirm("Empty the bin?", "Every contact in the bin is deleted for good. This cannot be undone.",
                            "Empty the bin"):
            return
        hub = self.hub

        def work() -> tuple[int, int]:
            return hub.empty_bin(), hub.bin_waiting()

        def done(result: tuple[int, int]) -> None:
            gone, waiting = result
            text = f"Emptied the bin ({gone})."
            if waiting:
                text += (f" {waiting} more {'is' if waiting == 1 else 'are'} still waiting to be deleted from "
                         "your accounts, and will go at the next sync - press Sync, then empty the bin again.")
            self.notice.emit(text)
            self.reload(select=[])

        loader.run_task(self, work, done)

    # ------------------------------------------------------- sidebar's actions
    def _smart_dialog(self, smart_id: Optional[int]) -> None:
        snap = self._snap
        if snap is None:
            return
        current = next((s for s in snap.smarts if s.id == smart_id), None)
        dlg = SmartDialog(self, name=current.name if current else "", rules=current.rules if current else None,
                          groups=[g[1] for g in snap.groups], tags=[t[1] for t in snap.tags],
                          accounts=[a for a in snap.accounts])
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.name():
            return
        name, rules, hub = dlg.name(), dlg.rules(), self.hub
        loader.run_task(self, lambda: hub.save_smart_group(name, rules, smart_id=smart_id),
                        lambda sid: self.reload(then=lambda: self.sidebar.select_key(f"smart:{sid}")))

    def _smart_delete(self, smart_id: int) -> None:
        if not self.confirm("Delete this smart group?", "The contacts in it are not deleted.", "Delete it"):
            return
        hub = self.hub
        if self._entry.kind == "smart" and self._entry.arg == smart_id:
            self._entry = Entry("all", "all", "All", "contact", section="Contacts")
            self.sidebar.select_key("all", emit=False)
        loader.run_task(self, lambda: hub.delete_smart_group(smart_id), lambda _r: self.reload(select=[]))

    def _tag_colour(self, tag_id: int, colour: str) -> None:
        name = next((t[1] for t in (self._snap.tags if self._snap else []) if t[0] == tag_id), "")
        hub = self.hub
        if name:
            loader.run_task(self, lambda: hub.create_tag(name, colour), lambda _r: self.reload())

    def _tag_delete(self, tag_id: int) -> None:
        if not self.confirm("Delete this tag?", "It is taken off every contact. The contacts stay.", "Delete it"):
            return
        hub = self.hub
        if self._entry.kind == "tag" and self._entry.arg == tag_id:
            self._entry = Entry("all", "all", "All", "contact", section="Contacts")
            self.sidebar.select_key("all", emit=False)
        loader.run_task(self, lambda: hub.delete_tag(tag_id), lambda _r: self.reload(select=[]))

    def _group_rename(self, group_id: int) -> None:
        name = self._ask_text("Rename group", "New name for the group:")
        hub = self.hub
        if name:
            loader.run_task(self, lambda: hub.rename_group(group_id, name), lambda _r: self.reload())

    def _context_menu(self, view: QWidget, pos: Any) -> None:
        """6r: right-click. The contact under the pointer joins the pick if it is not in it."""
        index = view.indexAt(pos)
        row = self.model.row_at(index.row()) if index.isValid() else None
        if row is not None and row.id not in self.selected_ids():
            self.select_ids([row.id])
        from app.ui.contacts import quick

        quick.show_menu(self, global_point(view, pos))

    # ----------------------------------------------------------------- tools
    def context(self, only: Optional[list[int]] = None) -> openers.Ctx:
        """What a tool is given. `only` narrows it to those contacts (7e, Enrich this contact)."""
        selected, visible = (list(only), list(only)) if only is not None else (self.selected_ids(), self.model.ids())
        hub, accounts = self.hub, self.accounts

        def enrich(on_progress: Any = None, cancelled: Any = None, force: bool = False) -> Any:
            # Runs on a worker; each internet source is skipped unless its switch is on. `force` is Look again:
            # the 90-day memory is ignored. The notes (a source unreachable, or not asked again) go back with
            # the proposals so the window can say them - before 2026-10-12 they were never collected.
            from types import SimpleNamespace  # noqa: PLC0415

            from app.contacts.enrich.run import proposals_for  # noqa: PLC0415
            notes: list[str] = []
            found = proposals_for(hub, accounts, list(selected) or list(visible), on_progress=on_progress,
                                  cancelled=cancelled, force=force, notes=notes)
            return SimpleNamespace(proposals=found, notes=notes)

        return openers.Ctx(hub=hub, accounts=accounts, selected_ids=selected, visible_ids=visible,
                           refresh=self.refresh, service=self.service, enrich=enrich,
                           notify=self.notice.emit)

    def enrich_one(self, cid: int, force: bool = False) -> None:
        """Order 1s 7e: Enrich this contact (Look again with `force`) - this contact only, on the page's one
        `EnrichRun`, so it carries on if the window is closed; the window opens to show it and to accept."""
        from app.ui.contacts_tools.enrich_run import run_for  # noqa: PLC0415

        ctx = self.context(only=[cid])
        if not run_for(self, ctx).start(ctx, force=force):
            self.notice.emit("A look for suggestions is already going. Enrich this contact once it has finished.")
        openers.open("enrich", self, ctx)

    def open_tool(self, name: str) -> None:
        if name == "select_all":
            return self.select_all()
        openers.open(name, self, self.context())

    # ----------------------------------------------------------------- upkeep
    def focus_filter(self) -> None:
        self.toolbar.focus_filter()

    def select_all(self) -> None:
        """Pick every contact shown; the cards view shares the table's selection."""
        self.table.selectAll()

    def shutdown(self) -> None:
        self._timer.stop()
        self.photos._wait_for_workers(2000)
