"""The account panel (10p general, 10q mail, 10r contacts).

Layer: L5

Opens under the list for the selected account. Every control saves at once
(`AccountStore.update`; the per-account choices go in `Account.options`), with a
plain sentence under it. Long work - Reconnect, Test connection - runs on a
worker; Remove account is the flow of 1f and states what it costs first.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QRadioButton, QSpinBox, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget, QColorDialog,
)

from app.ui.accounts import logic, parts
from app.ui.accounts.backend import Backend
from app.ui.accounts.parts import ScheduleRow, btn, hint, notice, set_quiet

__all__ = ["AccountPanel", "ask_remove", "choose_from"]


# ------------------------------------------------------------ worker bodies
def _reconnect(factory: Any, account_id: int, on_progress: Any = None) -> Any:
    svc = factory()
    acct = svc.reconnect(account_id, on_url=on_progress)
    svc.accounts.update(account_id, options={"signed_in_at": int(time.time())})
    return acct


def _test(factory: Any, account_id: int) -> Any:
    svc = factory()
    acct = svc.accounts.get(account_id)
    return svc.test_connection(acct)


def _set_password(factory: Any, account_id: int, password: str) -> None:
    factory().set_app_password(account_id, password)


def _remove(factory: Any, account_id: int, keep_contacts: bool, remove_mail: bool) -> None:
    factory().remove_account(account_id, keep_contacts=keep_contacts, remove_mail=remove_mail)


def _list_names(factory: Any, account_id: int, method: str) -> Optional[list]:
    svc = factory()
    fn = getattr(svc, method, None)
    if not callable(fn):
        return None
    return list(fn(svc.accounts.get(account_id)))


def _drop_mail(factory: Any, account_id: int) -> bool:
    svc = factory()
    hook = getattr(svc, "remove_mail_hook", None)
    if hook is None:
        return False
    hook(account_id)
    return True


# ----------------------------------------------------------------- dialogs
class _RemoveDialog(QDialog):
    def __init__(self, account: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Remove {account.name}")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            f"Removing {account.name} signs Leasha out of it and deletes the password or sign-in "
            "kept for it in Windows Credential Manager. Nothing is changed on the account itself.\n\n"
            "You can sign in again later."))
        self.keep_contacts = QCheckBox("Keep this account's contacts in Leasha")
        self.keep_contacts.setChecked(True)
        self.remove_mail = QCheckBox("Remove this account's mail from the index")
        self.remove_mail.setChecked(True)
        self.keep_contacts.setToolTip("Leave this account's contacts in your Leasha book.")
        self.remove_mail.setToolTip("Take the mail fetched from this account out of search.")
        lay.addWidget(self.keep_contacts)
        lay.addWidget(hint("Kept contacts stay in your book with this account taken out of where they are "
                           "kept. Unticked, the contacts that only lived here go to the bin (30 days)."))
        lay.addWidget(self.remove_mail)
        lay.addWidget(hint("Unticked, the mail already fetched stays searchable but is no longer updated."))
        ok = btn("Remove account", "trash-2", "danger")
        cancel = btn("Cancel", "x")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        lay.addLayout(self._row(cancel, ok))

    @staticmethod
    def _row(*buttons: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        for b in buttons:
            row.addWidget(b)
        return row


def ask_remove(parent: Optional[QWidget], account: Any) -> Optional[tuple[bool, bool]]:
    """The 1f flow. Returns (keep_contacts, remove_mail), or None when cancelled. Tests replace it."""
    dlg = _RemoveDialog(account, parent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.keep_contacts.isChecked(), dlg.remove_mail.isChecked()


def choose_from(parent: Optional[QWidget], title: str, names: list[str], chosen: Optional[list[str]]
                ) -> Optional[list[str]]:
    """A tree of checkable names; returns the ticked ones, or None when cancelled. Tests replace it."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    tree = QTreeWidget()
    tree.setHeaderHidden(True)
    tree.setAccessibleName(title)
    from PySide6.QtCore import Qt  # noqa: PLC0415
    for n in names:
        item = QTreeWidgetItem([n])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Checked if chosen is None or n in chosen else Qt.CheckState.Unchecked)
        tree.addTopLevelItem(item)
    lay.addWidget(tree)
    ok, cancel = btn("Save", "check", "primary"), btn("Cancel", "x")
    ok.clicked.connect(dlg.accept)
    cancel.clicked.connect(dlg.reject)
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(cancel)
    row.addWidget(ok)
    lay.addLayout(row)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())
            if tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]


def ask_password(parent: Optional[QWidget], who: str) -> Optional[str]:
    text, ok = QInputDialog.getText(parent, "App password", f"New app password for {who}:",
                                    QLineEdit.EchoMode.Password)
    return text if ok and text else None


def pick_colour(parent: Optional[QWidget], current: str) -> Optional[str]:
    from PySide6.QtGui import QColor  # noqa: PLC0415
    c = QColorDialog.getColor(QColor(current), parent, "Account colour")
    return c.name() if c.isValid() else None


# -------------------------------------------------------------------- panel
class AccountPanel(QGroupBox):
    #: (account id, kind) - kind "" means both. The page hands it to the scheduler.
    sync_requested = Signal(int, str)
    #: The account was removed; the page reads its state again.
    removed = Signal()

    def __init__(self, backend: Backend, parent: Optional[QWidget] = None) -> None:
        super().__init__("Account", parent)
        self.backend = backend
        self._id = 0
        self._account: Any = None
        self._loading = False
        self._same_for_all = True
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Account settings")
        lay.addWidget(self.tabs)
        self.notice = notice()
        lay.addWidget(self.notice)
        self._build_general()
        self._build_mail()
        self._build_contacts()
        self._tips()
        self.setVisible(False)

    # ============================================================= general
    def _build_general(self) -> None:
        page = QWidget()
        form = QFormLayout(page)
        self.name = QLineEdit()
        self.name.setAccessibleName("Account name")
        form.addRow("Name", self.name)
        form.addRow(hint("Your own name for it, such as Work or Home."))
        self.colour_button = btn("Colour…", "palette", tip="Choose the colour shown beside this account")
        self.colour_swatch = QLabel("")
        self.colour_swatch.setFixedSize(18, 18)
        row = QHBoxLayout()
        row.addWidget(self.colour_swatch)
        row.addWidget(self.colour_button)
        row.addStretch(1)
        form.addRow("Colour", row)
        form.addRow(hint("Shown on its row, and as a stripe on its contacts."))
        self.signin = QLabel("")
        self.signin.setWordWrap(True)
        self.reconnect = btn("Reconnect", "key-round", tip="Sign in to this account again")
        self.change_password = btn("Change app password…", "lock", tip="Type a new app password")
        form.addRow("Sign-in", self.signin)
        srow = QHBoxLayout()
        srow.addWidget(self.reconnect)
        srow.addWidget(self.change_password)
        srow.addStretch(1)
        form.addRow(srow)
        form.addRow(hint("Reconnect opens your browser to sign in again. Nothing already in Leasha is lost."))
        self.test_button = btn("Test connection", "link", tip="Check the sign-in and what Leasha may read")
        self.sync_button = btn("Sync now", "refresh-cw", tip="Sync this account now")
        trow = QHBoxLayout()
        trow.addWidget(self.test_button)
        trow.addWidget(self.sync_button)
        trow.addStretch(1)
        form.addRow(trow)
        form.addRow(hint("Test connection reaches the server and reports each permission granted. "
                         "Sync now runs this account's sync at once, whatever the schedule says."))
        self.schedules = QWidget()
        sform = QFormLayout(self.schedules)
        sform.setContentsMargins(0, 0, 0, 0)
        self.own_mail = ScheduleRow("This account's mail")
        self.own_contacts = ScheduleRow("This account's contacts")
        sform.addRow("Mail schedule", self.own_mail)
        sform.addRow("Contacts schedule", self.own_contacts)
        sform.addRow(hint("Shown because the schedule above is 'Each account has its own'. They start "
                          "from the global schedule and change only this account."))
        form.addRow(self.schedules)
        self.idle = QCheckBox("New inbox mail as it arrives")
        form.addRow(self.idle)
        self.idle_hint = hint("Keeps a connection open while Leasha runs, so new inbox mail is fetched "
                              "at once. Off by default; the schedule and Sync now are the usual starters.")
        form.addRow(self.idle_hint)
        self.remove_button = btn("Remove account", "trash-2", "danger",
                                 tip="Sign out and remove this account from Leasha")
        form.addRow(self.remove_button)
        form.addRow(hint("Asks first whether to keep this account's contacts and its mail in Leasha."))
        self.tabs.addTab(page, "General")

        self.name.editingFinished.connect(self._name_done)
        self.colour_button.clicked.connect(self._colour)
        self.reconnect.clicked.connect(self._reconnect)
        self.change_password.clicked.connect(self._password)
        self.test_button.clicked.connect(self._test)
        self.sync_button.clicked.connect(lambda: self.sync_requested.emit(self._id, ""))
        self.own_mail.changed.connect(lambda sc: self._opt(mail_schedule=logic.schedule_to_option(sc)))
        self.own_contacts.changed.connect(lambda sc: self._opt(contacts_schedule=logic.schedule_to_option(sc)))
        self.idle.toggled.connect(lambda on: self._opt(idle=bool(on)))
        self.remove_button.clicked.connect(self._remove)

    # ================================================================= mail
    def _build_mail(self) -> None:
        page = QWidget()
        form = QFormLayout(page)
        head = QLabel("Read only - Leasha never changes this mailbox")
        head.setObjectName("accountsMailHead")
        form.addRow(head)
        form.addRow(hint("Mail is only read: never moved, flagged, marked read or deleted on the server."))
        self.mail_on = QCheckBox("Fetch this account's mail")
        form.addRow(self.mail_on)
        form.addRow(hint("Off, nothing of this account's mail is fetched. What was fetched stays until you "
                         "remove it below."))
        self.folders_all = QRadioButton("All folders and labels")
        self.folders_some = QRadioButton("Only the ones I choose")
        self.choose_folders = btn("Choose folders…", "folder-open", tip="Pick which folders or labels to fetch")
        frow = QHBoxLayout()
        for w in (self.folders_all, self.folders_some, self.choose_folders):
            frow.addWidget(w)
        frow.addStretch(1)
        form.addRow("Folders", frow)
        form.addRow(hint("All is the default. Spam and Trash are left out by default, and Sent is included."))
        self.back = QComboBox()
        self.back.setAccessibleName("How far back to fetch")
        for key, text in logic.BACK_CHOICES:
            self.back.addItem(text, key)
        self.since = QDateEdit()
        self.since.setCalendarPopup(True)
        self.since.setDisplayFormat("yyyy-MM-dd")
        self.since.setAccessibleName("Fetch mail since")
        self.since.setDate(QDate.currentDate().addYears(-1))
        brow = QHBoxLayout()
        brow.addWidget(self.back)
        brow.addWidget(self.since)
        brow.addStretch(1)
        form.addRow("How far back", brow)
        form.addRow(hint("Everything is the default. Choose before the first fetch to download less."))
        self.attachments = QCheckBox("Index attachments")
        self.attachment_mb = QSpinBox()
        self.attachment_mb.setRange(0, 4096)
        self.attachment_mb.setSuffix(" MB")
        self.attachment_mb.setSpecialValueText("The index's own limit")
        self.attachment_mb.setKeyboardTracking(False)
        self.attachment_mb.setAccessibleName("Largest attachment to index")
        arow = QHBoxLayout()
        arow.addWidget(self.attachments)
        arow.addWidget(self.attachment_mb)
        arow.addStretch(1)
        form.addRow(arow)
        form.addRow(hint("Attachments are searched by their text, as in the rest of your index. A larger "
                         "one than the limit is listed but not read."))
        self.space = QLabel("")
        form.addRow("Space used", self.space)
        self.drop_mail = btn("Remove this account's mail from the index…", "trash-2", "danger")
        form.addRow(self.drop_mail)
        form.addRow(hint("Takes the fetched mail out of search. The mailbox itself is not touched."))
        self.progress = QLabel("")
        self.sync_mail_button = btn("Sync mail now", "refresh-cw", tip="Fetch this account's mail now")
        form.addRow(self.sync_mail_button)
        self.pause_fetch = btn("Pause fetch", "pause", tip="Pause or resume the first fetch of mail")
        form.addRow("First fetch", self.progress)
        form.addRow(self.pause_fetch)
        form.addRow(hint("Messages fetched of the total. Pausing keeps what is fetched; resuming carries on."))
        self.tabs.addTab(page, "Mail")

        self.sync_mail_button.clicked.connect(lambda: self.sync_requested.emit(self._id, "mail"))
        self.mail_on.toggled.connect(lambda on: self._acct(mail_on=bool(on)))
        self.folders_all.toggled.connect(self._folders_mode)
        self.choose_folders.clicked.connect(lambda: self._pick("list_folders", "mail_folders", "Folders to fetch"))
        self.back.currentIndexChanged.connect(self._back_changed)
        self.since.dateChanged.connect(lambda d: self._back_changed())
        self.attachments.toggled.connect(lambda on: self._opt(mail_attachments=bool(on)))
        self.attachment_mb.valueChanged.connect(lambda v: self._opt(mail_attachment_mb=int(v)))
        self.drop_mail.clicked.connect(self._drop_mail)
        self.pause_fetch.clicked.connect(self._toggle_fetch)

    # ============================================================== contacts
    def _build_contacts(self) -> None:
        page = QWidget()
        form = QFormLayout(page)
        self.direction = QComboBox()
        self.direction.setAccessibleName("Contacts direction")
        for key, text in logic.DIRECTION_CHOICES:
            self.direction.addItem(text, key)
        form.addRow("Direction", self.direction)
        self.direction_note = hint("")
        form.addRow(self.direction_note)
        self.groups_all = QRadioButton("All folders and groups")
        self.groups_some = QRadioButton("Only the ones I choose")
        self.choose_groups = btn("Choose groups…", "folder-open", tip="Pick which groups to sync")
        grow = QHBoxLayout()
        for w in (self.groups_all, self.groups_some, self.choose_groups):
            grow.addWidget(w)
        grow.addStretch(1)
        form.addRow("Which contacts", grow)
        form.addRow(hint("All is the default. Choosing syncs only those groups."))
        self.sync_contacts_button = btn("Sync contacts now", "refresh-cw", tip="Sync this account's contacts now")
        form.addRow(self.sync_contacts_button)
        self.kept = QLabel("")
        self.last_changes = QLabel("")
        self.last_changes.setWordWrap(True)
        form.addRow("Kept here", self.kept)
        form.addRow("Last changes", self.last_changes)
        form.addRow(hint("How many of your contacts are kept in this account, and what the last sync did "
                         "in each direction."))
        self.tabs.addTab(page, "Contacts")

        self.sync_contacts_button.clicked.connect(lambda: self.sync_requested.emit(self._id, "contacts"))
        self.direction.currentIndexChanged.connect(self._direction_changed)
        self.groups_all.toggled.connect(self._groups_mode)
        self.choose_groups.clicked.connect(
            lambda: self._pick("list_contact_groups", "contact_groups", "Groups to sync"))

    def _tips(self) -> None:
        """A sentence of what each control does (the hover text)."""
        self.idle.setToolTip('Keep a connection open while Leasha runs, so new inbox mail is fetched at once.')
        self.mail_on.setToolTip("Fetch this account's mail, read only.")
        self.folders_all.setToolTip('Fetch every folder or label (Spam and Trash are left out).')
        self.folders_some.setToolTip('Fetch only the folders or labels you choose.')
        self.back.setToolTip('How far back the first fetch goes.')
        self.attachments.setToolTip('Search inside attachments as well as the message.')
        self.attachment_mb.setToolTip('Attachments larger than this are listed but not read.')
        self.direction.setToolTip('Two-way reads and writes; Into Leasha only never writes to the account; Off stops syncing contacts.')
        self.groups_all.setToolTip('Sync every contact folder or group.')
        self.groups_some.setToolTip('Sync only the groups you choose.')

    # ================================================================== show
    def show_account(self, status: Any, state: Any, same_for_all: bool) -> None:
        a = status.account
        self._account, self._id, self._same_for_all = a, a.id, same_for_all
        self._loading = True
        try:
            self.setTitle(f"Account: {a.name}")
            set_quiet(self.name, a.name, "setText")
            self.colour_swatch.setStyleSheet(f"background: {a.colour}; border: 1px solid palette(mid);")
            self.signin.setText(logic.signin_text(a))
            oauth = a.provider in ("google", "microsoft")
            self.reconnect.setVisible(oauth)
            self.change_password.setVisible(not oauth)
            self.schedules.setVisible(not same_for_all)
            s = state.settings
            set_quiet_sc = self.own_mail.set_value
            set_quiet_sc(logic.schedule_from_option(a.options.get("mail_schedule"), s.mail))
            self.own_contacts.set_value(logic.schedule_from_option(a.options.get("contacts_schedule"), s.contacts))
            imap = a.provider in ("imap", "apple")
            self.idle.setVisible(imap)
            self.idle_hint.setVisible(imap)
            set_quiet(self.idle, bool(a.options.get("idle", False)), "setChecked")
            # mail
            set_quiet(self.mail_on, bool(a.mail_on), "setChecked")
            folders = a.options.get("mail_folders")
            set_quiet(self.folders_all, folders is None, "setChecked")
            set_quiet(self.folders_some, folders is not None, "setChecked")
            self.choose_folders.setEnabled(folders is not None)
            back = str(a.options.get("mail_back", "all"))
            set_quiet(self.back, max(0, self.back.findData(back)), "setCurrentIndex")
            since = a.options.get("mail_since")
            if since:
                set_quiet(self.since, QDate.fromString(str(since), "yyyy-MM-dd"), "setDate")
            self.since.setVisible(back == "since")
            set_quiet(self.attachments, bool(a.options.get("mail_attachments", True)), "setChecked")
            set_quiet(self.attachment_mb, int(a.options.get("mail_attachment_mb", 0)), "setValue")
            used = state.mail_space.get(a.id)
            self.space.setText("Nothing fetched yet." if not used else f"{used / 1_048_576:,.1f} MB")
            prog = state.mail_progress.get(a.id) or {}
            if prog.get("total"):
                self.progress.setText(f"{prog.get('fetched', 0):,} of {prog['total']:,} messages"
                                      + (f" ({prog['rate']}/s)" if prog.get("rate") else ""))
            else:
                self.progress.setText("Not started." if not a.mail_on else "Waiting for the first fetch.")
            paused = bool(a.options.get("mail_paused", False))
            self.pause_fetch.setText("Resume fetch" if paused else "Pause fetch")
            parts_ = [self.pause_fetch, self.progress]
            for w in parts_:
                w.setVisible(a.mail_on)
            # contacts
            set_quiet(self.direction, max(0, self.direction.findData(
                a.contacts_direction if a.contacts_on else "off")), "setCurrentIndex")
            self._direction_note()
            groups = a.options.get("contact_groups")
            set_quiet(self.groups_all, groups is None, "setChecked")
            set_quiet(self.groups_some, groups is not None, "setChecked")
            self.choose_groups.setEnabled(groups is not None)
            n = state.kept.get(a.id, 0)
            self.kept.setText(f"{n:,} contact{'s' if n != 1 else ''}")
            self.last_changes.setText(self._last_text(state.last_runs.get(a.id)))
        finally:
            self._loading = False
        self.setVisible(True)

    @staticmethod
    def _last_text(run: Any) -> str:
        if run is None:
            return "No sync has run yet."
        p = run.plan or {}
        when = logic.when_text(run.finished_at or run.started_at)
        return (f"{when}. Into Leasha: {p.get('add_in', 0)} added, {p.get('merge_in', 0)} changed. "
                f"To this account: {p.get('create_out', 0)} created, {p.get('update_out', 0)} changed, "
                f"{p.get('delete_out', 0)} deleted.")

    def clear(self) -> None:
        self._id = 0
        self._account = None
        self.setVisible(False)

    # ============================================================== handlers
    def _acct(self, **values: Any) -> None:
        if self._id and not self._loading:
            self.backend.save_account(self._id, **values)

    def _opt(self, **options: Any) -> None:
        self._acct(options=options)

    def _say(self, text: str, bad: bool = False) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _error(self, err: Any) -> None:
        msg = getattr(err, "message", str(err))
        fix = getattr(err, "suggestion", "")
        self._say(f"{msg} {fix}".strip(), True)

    def _name_done(self) -> None:
        text = self.name.text().strip()
        if text and self._account is not None and text != self._account.name:
            self._acct(name=text)

    def _colour(self) -> None:
        chosen = pick_colour(self, self._account.colour if self._account else "#4C8BF5")
        if chosen:
            self.colour_swatch.setStyleSheet(f"background: {chosen}; border: 1px solid palette(mid);")
            self._acct(colour=chosen)

    def _reconnect(self) -> None:
        self._say("Your browser is opening so you can sign in. Leasha waits for you here.")
        self.backend.call(_reconnect, self.backend.factory, self._id, progress=lambda url: self._say(
            f"If your browser did not open, go to: {url}"), serial=False,
            done=lambda _a: self._say("Signed in again."), fail=self._error, refresh=True)

    def _password(self) -> None:
        pw = ask_password(self, self._account.name)
        if pw:
            self.backend.call(_set_password, self.backend.factory, self._id, pw, serial=True,
                              done=lambda _r: self._say("Saved in Windows Credential Manager."),
                              fail=self._error, refresh=True)

    def _test(self) -> None:
        self._say("Testing…")
        self.backend.call(_test, self.backend.factory, self._id, done=self._tested, fail=self._error)

    def _tested(self, report: Any) -> None:
        if report.ok:
            granted = ", ".join(report.scopes) if report.scopes else "the connection works"
            self._say(f"Connected. Granted: {granted}.")
        else:
            self._say(f"{report.message} {report.fix}".strip(), True)

    def _remove(self) -> None:
        if self._account is None:
            return
        answer = ask_remove(self, self._account)
        if answer is None:
            return
        keep, drop_mail = answer
        self.backend.call(_remove, self.backend.factory, self._id, keep, drop_mail, serial=True,
                          done=lambda _r: self.removed.emit(), fail=self._error, refresh=True)

    # -- mail
    def _folders_mode(self, all_: bool) -> None:
        if self._loading:
            return
        self.choose_folders.setEnabled(not all_)
        self._opt(mail_folders=None if all_ else (self._account.options.get("mail_folders") or []))

    def _back_changed(self) -> None:
        if self._loading:
            return
        key = str(self.back.currentData())
        self.since.setVisible(key == "since")
        self._opt(mail_back=key, mail_since=self.since.date().toString("yyyy-MM-dd") if key == "since" else None)

    def _pick(self, method: str, option: str, title: str) -> None:
        def got(names: Any) -> None:
            if names is None:
                self._say("The list of folders is not available until this account has connected. "
                          "Press Test connection first.")
                return
            current = self._account.options.get(option)
            chosen = choose_from(self, title, list(names), current)
            if chosen is not None:
                self._opt(**{option: chosen})
        self.backend.call(_list_names, self.backend.factory, self._id, method, done=got, fail=self._error)

    def _drop_mail(self) -> None:
        if not parts.confirm(
                self, "Remove this account's mail?",
                f"This takes every message fetched from {self._account.name} out of Leasha's search. "
                "The mailbox itself is not touched, and fetching it again later brings it back "
                "(taking the time the first fetch took).", "Remove the mail", danger=True):
            return
        self.backend.call(_drop_mail, self.backend.factory, self._id, serial=True,
                          done=lambda ok: self._say("Removed." if ok else "There was no mail to remove."),
                          fail=self._error, refresh=True)

    def _toggle_fetch(self) -> None:
        self._opt(mail_paused=not bool(self._account.options.get("mail_paused", False)))

    # -- contacts
    def _direction_note(self) -> None:
        self.direction_note.setText({
            "two-way": "Changes go both ways: what you change in Leasha is written to this account.",
            "in-only": "Leasha reads this account and never writes to it.",
            "off": "Contacts are not synced with this account."}[str(self.direction.currentData())])

    def _direction_changed(self, _i: int) -> None:
        self._direction_note()
        if self._loading:
            return
        key = str(self.direction.currentData())
        values: dict[str, Any] = {"contacts_direction": key}
        if key != "off":
            values["contacts_on"] = True
        self._acct(**values)

    def _groups_mode(self, all_: bool) -> None:
        if self._loading:
            return
        self.choose_groups.setEnabled(not all_)
        self._opt(contact_groups=None if all_ else (self._account.options.get("contact_groups") or []))
