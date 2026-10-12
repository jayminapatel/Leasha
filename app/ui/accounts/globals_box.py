"""The top of Settings > Accounts: the settings that cover every account (10g-10n, 1a).

Layer: L5

Built like the rest of Settings (rule 11): every control saves the moment it
changes - through `Backend.save_settings`, which runs on a worker - and every
value has a plain sentence under it. Turning the first-sync dry run off states
the cost and asks first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import QTime
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QSpinBox, QTimeEdit, QVBoxLayout, QWidget,
)

from app.ui.accounts import enrich_settings, logic, parts
from app.ui.accounts.backend import Backend
from app.ui.accounts.parts import ScheduleRow, btn, hint, set_quiet
from app.ui.widgets.region_box import RegionBox

__all__ = ["GlobalSettingsBox"]


def _put_google_secret(factory: Any, value: str) -> None:
    from app.accounts.secrets import client_secret_key  # noqa: PLC0415
    factory().secrets.put(client_secret_key("google"), value)


class GlobalSettingsBox(QWidget):
    def __init__(self, backend: Backend, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.backend = backend
        self._loading = False
        self._google_secret_set = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # ---- 10g schedule
        box, form = self._group("Sync schedule", outer)
        self.same_for_all = QComboBox()
        self.same_for_all.setAccessibleName("Schedule for accounts")
        self.same_for_all.addItem("The same schedule for every account", True)
        self.same_for_all.addItem("Each account has its own", False)
        form.addRow("Schedule for", self.same_for_all)
        form.addRow(hint("With 'Each account has its own', every account's panel shows its own pair of "
                         "schedules, starting from the ones below."))
        self.mail_schedule = ScheduleRow("Mail")
        self.contacts_schedule = ScheduleRow("Contacts")
        form.addRow("Mail", self.mail_schedule)
        form.addRow(hint("How often mail is fetched from your accounts. Manual (the default) means only "
                         "when you press Sync now. Mail read from files and Outlook follows the indexing "
                         "schedule instead."))
        form.addRow("Contacts", self.contacts_schedule)
        form.addRow(hint("How often your contacts are read from and written to your accounts. Nothing "
                         "syncs when Leasha starts, wakes or reconnects - only this schedule or Sync now."))
        self.same_for_all.currentIndexChanged.connect(
            lambda _i: self._save(same_for_all=bool(self.same_for_all.currentData())))
        self.mail_schedule.changed.connect(lambda sc: self._save(mail=sc))
        self.contacts_schedule.changed.connect(lambda sc: self._save(contacts=sc))

        # ---- 10h pause, 10i quiet hours
        box, form = self._group("Pause and quiet hours", outer)
        self.pause = QCheckBox("Pause all sync")
        self.pause_for = QComboBox()
        self.pause_for.setAccessibleName("Pause for")
        for key, text in logic.PAUSE_CHOICES:
            self.pause_for.addItem(text, key)
        row = QHBoxLayout()
        row.addWidget(self.pause)
        row.addWidget(self.pause_for)
        row.addStretch(1)
        form.addRow(row)
        self.pause_note = hint("")
        form.addRow(self.pause_note)
        self.pause.toggled.connect(lambda _c: self._pause_changed())
        self.pause_for.currentIndexChanged.connect(lambda _i: self.pause.isChecked() and self._pause_changed())

        self.quiet = QCheckBox("Quiet hours")
        self.quiet_start = QTimeEdit()
        self.quiet_end = QTimeEdit()
        for w, n in ((self.quiet_start, "Quiet hours start"), (self.quiet_end, "Quiet hours end")):
            w.setDisplayFormat("HH:mm")
            w.setAccessibleName(n)
        qrow = QHBoxLayout()
        for w in (self.quiet, QLabel("from"), self.quiet_start, QLabel("to"), self.quiet_end):
            qrow.addWidget(w)
        qrow.addStretch(1)
        form.addRow(qrow)
        form.addRow(hint("No scheduled sync starts between these times (off by default). Sync now still "
                         "works, and an account's row says when quiet hours are holding a sync back."))
        self.quiet.toggled.connect(lambda c: self._save(quiet_on=bool(c)))
        self.quiet_start.timeChanged.connect(lambda t: self._save(quiet_start=t.toString("HH:mm")))
        self.quiet_end.timeChanged.connect(lambda t: self._save(quiet_end=t.toString("HH:mm")))

        # ---- 10j first-fetch speed cap
        box, form = self._group("First fetch of mail", outer)
        self.cap = QSpinBox()
        self.cap.setRange(0, 100_000)
        self.cap.setSingleStep(100)
        self.cap.setSuffix(" KB/s")
        self.cap.setSpecialValueText("No cap")
        self.cap.setKeyboardTracking(False)
        self.cap.setAccessibleName("Speed cap for the first fetch")
        form.addRow("Speed cap", self.cap)
        form.addRow(hint("Limits how fast a mailbox is downloaded the first time, so the internet stays "
                         "usable. No cap is the default."))
        self.cap.valueChanged.connect(lambda v: self._save(first_fetch_kb_s=int(v)))

        # ---- 10k conflict rule
        box, form = self._group("When the same contact changed in two places", outer)
        self.conflict = QComboBox()
        self.conflict.setAccessibleName("When a contact changed in two places")
        for key, text in logic.CONFLICT_CHOICES:
            self.conflict.addItem(text, key)
        form.addRow("Rule", self.conflict)
        self.conflict_note = hint("")
        form.addRow(self.conflict_note)
        self.conflict.currentIndexChanged.connect(self._conflict_changed)

        # ---- 10l safety
        box, form = self._group("Safety", outer)
        self.brake_count = QSpinBox()
        self.brake_count.setRange(1, 100_000)
        self.brake_count.setKeyboardTracking(False)
        self.brake_count.setAccessibleName("Delete brake: contacts")
        self.brake_percent = QDoubleSpinBox()
        self.brake_percent.setRange(0.1, 100.0)
        self.brake_percent.setDecimals(1)
        self.brake_percent.setSuffix(" %")
        self.brake_percent.setKeyboardTracking(False)
        self.brake_percent.setAccessibleName("Delete brake: percent of the book")
        self.brake_percent.setToolTip("A sync that would delete more than this share of your contact book "
                                      "stops and asks first. 5 % is the default.")
        form.addRow("Stop a sync that deletes more than", self.brake_count)
        form.addRow("or more than", self.brake_percent)
        form.addRow(hint("A sync that would delete more contacts than this (20 contacts or 5% of your book "
                         "is the default) stops and asks. Nothing is deleted until you press Allow."))
        self.dry_run = QCheckBox("Dry run before an account's first sync")
        form.addRow(self.dry_run)
        form.addRow(hint("Off by default. When on, the first sync of an account lists what it would add, "
                         "merge and change, and writes nothing until you press Approve."))
        self.brake_count.valueChanged.connect(lambda v: self._save(brake_count=int(v)))
        self.brake_percent.valueChanged.connect(lambda v: self._save(brake_percent=float(v)))
        self.dry_run.toggled.connect(self._dry_run_toggled)

        # ---- 2026-10-11: keeping every contact in step across the accounts
        box, form = self._group("Keeping contacts in step", outer)
        self.master = QComboBox()
        self.master.setAccessibleName("Master account")
        self.master.setToolTip("The account whose list is the master: a contact deleted there is deleted "
                               "here and from your other accounts")
        form.addRow("Master account", self.master)
        form.addRow(hint("Leasha's own contact book is the master copy; this chooses the account whose "
                         "deletes count. A contact deleted in it goes to the Bin here and is removed from "
                         "the other accounts. A delete in any other account only flags the contact. A big "
                         "batch of deletes is held by the delete brake until you press Allow."))
        self.mirror = QCheckBox("Keep every contact in every account")
        self.mirror.setToolTip("Adds and changes made in any account reach all of them, through Leasha")
        form.addRow(self.mirror)
        form.addRow(hint("Off by default. When on, every contact is written to each account that takes "
                         "contacts back, so a person added in one appears in all."))
        self.backup = QCheckBox("Copy the contact book before a first sync, and daily")
        self.backup_keep = QSpinBox()
        self.backup_keep.setRange(1, 1000)
        self.backup_keep.setKeyboardTracking(False)
        self.backup_keep.setAccessibleName("Contact copies to keep")
        self.backup.setToolTip("Write a copy of the contact book before an account's first sync and once a day")
        self.backup_keep.setToolTip("How many copies to keep; older ones are removed as new ones are written")
        form.addRow(self.backup)
        form.addRow("Copies to keep", self.backup_keep)
        form.addRow(hint("A vCard file and a small database of the contact tables, in the backups folder "
                         "of your data. Both can put the book back as it was."))
        self.master.currentIndexChanged.connect(
            lambda _i: self._save(master_account_id=self.master.currentData()))
        self.mirror.toggled.connect(lambda on: self._save(mirror_contacts=bool(on)))
        self.backup.toggled.connect(lambda on: self._save(backup_contacts=bool(on)))
        self.backup_keep.valueChanged.connect(lambda v: self._save(contacts_backup_keep=int(v)))

        # ---- 10m notifications
        box, form = self._group("Notifications", outer)
        self.notify_brake = QCheckBox("A sync is held by the delete brake")
        self.notify_conflict = QCheckBox("A contact changed in two places")
        self.notify_signin = QCheckBox("A sign-in has expired or expires within 2 days")
        self.notify_failed = QCheckBox("An account has failed for 24 hours")
        for c, key in ((self.notify_brake, "notify_brake"), (self.notify_conflict, "notify_conflict"),
                       (self.notify_signin, "notify_signin"), (self.notify_failed, "notify_failed_24h")):
            form.addRow(c)
            c.toggled.connect(lambda on, k=key: self._save(**{k: bool(on)}))
        form.addRow(hint("Windows notifications come from the Leasha icon by the clock (switch it on in "
                         "Settings > Window). Nothing is sent for an ordinary successful sync."))

        # ---- 10n default account, phone region, maps
        box, form = self._group("Contacts defaults", outer)
        self.default_account = QComboBox()
        self.default_account.setAccessibleName("Default account for new contacts")
        form.addRow("New contacts go to", self.default_account)
        form.addRow(hint("A contact you make in Leasha is kept in this account. By default it is the first "
                         "account added with contact sync on."))
        self.phone_region = RegionBox()
        self.phone_region.setAccessibleName("Phone number region")
        self.phone_region.setToolTip("The country a phone number is from, with its dialling code")
        form.addRow("Phone numbers are from", self.phone_region)
        form.addRow(hint("Pick a country, or type its name, two-letter code or dialling code. A number typed "
                         "without a country code is read as belonging to it."))
        self.maps_site = QComboBox()
        self.maps_site.setAccessibleName("Maps site")
        for key, text in logic.MAPS_CHOICES:
            self.maps_site.addItem(text, key)
        form.addRow("Show addresses on", self.maps_site)
        form.addRow(hint("The site opened when you click an address."))
        self.default_account.currentIndexChanged.connect(self._default_changed)
        self.phone_region.editingFinished.connect(self._region_done)
        self.maps_site.currentIndexChanged.connect(lambda _i: self._save(maps_site=str(self.maps_site.currentData())))

        # ---- 7 enrichment: what may be looked up, and where
        box, form = self._group("Enrichment", outer)
        # Order 1s 8a: every source and every rule, built in `enrich_settings`.
        self.enrich_controls = enrich_settings.build(form, self._save)
        self.enrich: dict[str, QCheckBox] = self.enrich_controls.checks

        # ---- 1a client IDs
        box, form = self._group("Sign-in set-up (Google and Microsoft)", outer)
        form.addRow(hint("Leasha ships with no sign-in key of its own: you make a free one with your own "
                         "Google and Microsoft logins, and paste it here. Never a file."))
        self.google_id = QLineEdit()
        self.google_id.setAccessibleName("Google client ID")
        self.google_id.setPlaceholderText("Google client ID")
        self.google_secret = QLineEdit()
        self.google_secret.setAccessibleName("Google client secret")
        self.google_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_secret.setPlaceholderText("Google client secret")
        self.microsoft_id = QLineEdit()
        self.microsoft_id.setAccessibleName("Microsoft client ID")
        self.microsoft_id.setPlaceholderText("Microsoft application (client) ID")
        self.google_help = btn("Show me how: Google", "external-link", tip="Opens the step-by-step guide")
        self.microsoft_help = btn("Show me how: Microsoft", "external-link", tip="Opens the step-by-step guide")
        self.apple_help = btn("Show me how: iCloud", "external-link",
                              tip="How to make an app-specific password at Apple")
        self.imap_help = btn("Show me how: other servers", "external-link",
                             tip="How to connect any IMAP or CardDAV server")
        form.addRow("Google client ID", self.google_id)
        form.addRow("Google client secret", self.google_secret)
        self.google_note = hint("Google gives a desktop app a client ID and a secret. The secret is kept in "
                                "Windows Credential Manager, never in a file or the database.")
        form.addRow(self.google_note)
        form.addRow(self.google_help)
        form.addRow("Microsoft client ID", self.microsoft_id)
        form.addRow(hint("From your Microsoft Entra app registration (a public client with a loopback "
                         "redirect). Personal, work and school accounts can all use it."))
        form.addRow(self.microsoft_help)
        form.addRow(parts.hint("Apple and other servers use an app password made at the provider."))
        form.addRow(self.apple_help)
        form.addRow(self.imap_help)
        self.google_id.editingFinished.connect(lambda: self._save(google_client_id=self.google_id.text().strip()))
        self.microsoft_id.editingFinished.connect(
            lambda: self._save(microsoft_client_id=self.microsoft_id.text().strip()))
        self.google_secret.editingFinished.connect(self._secret_done)
        self._tips()
        for b, key in ((self.google_help, "google"), (self.microsoft_help, "microsoft"),
                       (self.apple_help, "apple"), (self.imap_help, "imap")):
            b.clicked.connect(lambda _c=False, k=key: parts.open_guide(logic.GUIDE_ANCHORS[k]))

    def _tips(self) -> None:
        """A sentence of what each control does (the hover text)."""
        self.same_for_all.setToolTip('Choose one schedule for every account, or a pair of schedules for each account.')
        self.pause.setToolTip('Hold every scheduled sync. Sync now still works.')
        self.pause_for.setToolTip('How long to hold scheduled syncs.')
        self.quiet.setToolTip('Do not start scheduled syncs between the two times.')
        self.quiet_start.setToolTip('When quiet hours begin.')
        self.quiet_end.setToolTip('When quiet hours end.')
        self.cap.setToolTip('The most the first download of a mailbox may use, in kilobytes per second. No cap leaves it unlimited.')
        self.conflict.setToolTip('What happens when one contact was changed in Leasha and in an account, or in two accounts.')
        self.brake_count.setToolTip('A sync that would delete more contacts than this stops and asks.')
        self.dry_run.setToolTip('List what the first sync of an account would do and write nothing until you approve.')
        self.notify_brake.setToolTip('Show a Windows notification when a sync is held by the delete brake.')
        self.notify_conflict.setToolTip('Show a Windows notification when a contact changed in two places.')
        self.notify_signin.setToolTip('Show a Windows notification when a sign-in has expired or expires within 2 days.')
        self.notify_failed.setToolTip('Show a Windows notification when an account has failed for 24 hours.')
        self.default_account.setToolTip('The account a contact made in Leasha is kept in.')
        self.maps_site.setToolTip('The site opened when you click an address.')

    # ------------------------------------------------------------------ helpers
    def _group(self, title: str, outer: QVBoxLayout) -> tuple[QGroupBox, QFormLayout]:
        box = QGroupBox(title)
        form = QFormLayout(box)
        outer.addWidget(box)
        return box, form

    def _save(self, **values: Any) -> None:
        if not self._loading:
            self.backend.save_settings(**values)

    def _pause_changed(self) -> None:
        if self._loading:
            return
        value = (logic.pause_value(str(self.pause_for.currentData()), datetime.now())
                 if self.pause.isChecked() else None)
        self.backend.save_settings(paused_until=value)

    def _conflict_changed(self, _i: int) -> None:
        key = str(self.conflict.currentData())
        self.conflict_note.setText({
            "newest": "Field by field, the edit made last is kept and the other is kept in the journal.",
            "priority": "The account nearer the top of the account list wins. Drag accounts to change "
                        "their order.",
            "ask": "Nothing is decided for you: the contact waits, marked 'changed in two places'."}[key])
        self._save(conflict_policy=key)

    def _dry_run_toggled(self, on: bool) -> None:
        if self._loading:
            return
        if not on:
            ok = parts.confirm(
                self, "Turn off the first-sync dry run?",
                "Without the dry run, the first sync of a new account merges and writes at once: contacts "
                "may be added to Leasha, and changes written back to the account, before you have seen "
                "the list. Every sync can still be undone from the sync log.\n\nTurn it off?",
                "Turn it off", danger=True)
            if not ok:
                set_quiet(self.dry_run, True, "setChecked")
                return
        self._save(dry_run_first=bool(on))

    def _default_changed(self, _i: int) -> None:
        self._save(default_account_id=self.default_account.currentData())

    def _region_done(self) -> None:
        text = self.phone_region.text().strip().upper()
        if len(text) == 2 and text.isalpha():
            self.phone_region.setText(text)
            self._save(phone_region=text)
        else:
            self.phone_region.setText(self._region)

    _region = "GB"

    def _secret_done(self) -> None:
        text = self.google_secret.text().strip()
        if not text or self._loading:
            return
        self.google_secret.clear()
        self.google_secret.setPlaceholderText("Saved in Windows Credential Manager - type to replace")
        self.backend.call(_put_google_secret, self.backend.factory, text, serial=True, refresh=True)

    # --------------------------------------------------------------------- load
    def load(self, state: Any) -> None:
        """Show the saved settings without emitting on the way in."""
        s = state.settings
        self._loading = True
        try:
            set_quiet(self.same_for_all, self.same_for_all.findData(bool(s.same_for_all)), "setCurrentIndex")
            self.mail_schedule.set_value(s.mail)
            self.contacts_schedule.set_value(s.contacts)
            now = state.now
            held = logic.hold_text(s, now)
            paused = s.paused_until is not None and (s.paused_until >= 9e15 or s.paused_until > now.timestamp())
            set_quiet(self.pause, paused, "setChecked")
            self.pause_note.setText(
                (held or "Paused.") if paused else
                "Sync runs on its schedule. Pausing holds every scheduled sync; Sync now still works. The "
                "Leasha icon by the clock has the same switch.")
            set_quiet(self.quiet, bool(s.quiet_on), "setChecked")
            for w, t in ((self.quiet_start, s.quiet_start), (self.quiet_end, s.quiet_end)):
                h, _, m = str(t).partition(":")
                set_quiet(w, QTime(int(h or 0), int(m or 0)), "setTime")
            set_quiet(self.cap, int(s.first_fetch_kb_s), "setValue")
            set_quiet(self.conflict, self.conflict.findData(s.conflict_policy), "setCurrentIndex")
            self._conflict_changed_text()
            set_quiet(self.brake_count, int(s.brake_count), "setValue")
            set_quiet(self.brake_percent, float(s.brake_percent), "setValue")
            set_quiet(self.dry_run, bool(s.dry_run_first), "setChecked")
            set_quiet(self.mirror, bool(s.mirror_contacts), "setChecked")
            set_quiet(self.backup, bool(s.backup_contacts), "setChecked")
            set_quiet(self.backup_keep, int(s.contacts_backup_keep), "setValue")
            blocked_m = self.master.blockSignals(True)
            self.master.clear()
            self.master.addItem("None - a delete in an account is only flagged", None)
            for row in state.statuses:
                self.master.addItem(row.account.name, row.account.id)
            self.master.setCurrentIndex(max(0, self.master.findData(s.master_account_id)))
            self.master.blockSignals(blocked_m)
            for c, v in ((self.notify_brake, s.notify_brake), (self.notify_conflict, s.notify_conflict),
                         (self.notify_signin, s.notify_signin), (self.notify_failed, s.notify_failed_24h)):
                set_quiet(c, bool(v), "setChecked")
            enrich_settings.load(self.enrich_controls, s)
            blocked = self.default_account.blockSignals(True)
            self.default_account.clear()
            self.default_account.addItem("The first account with contact sync on", None)
            for row in state.statuses:
                self.default_account.addItem(row.account.name, row.account.id)
            self.default_account.setCurrentIndex(max(0, self.default_account.findData(s.default_account_id)))
            self.default_account.blockSignals(blocked)
            self._region = s.phone_region or "GB"
            set_quiet(self.phone_region, self._region, "setText")
            set_quiet(self.maps_site, self.maps_site.findData(s.maps_site), "setCurrentIndex")
            for w, v in ((self.google_id, s.google_client_id), (self.microsoft_id, s.microsoft_client_id)):
                if not w.hasFocus():
                    set_quiet(w, v, "setText")
            self._google_secret_set = bool(state.google_secret_set)
            if self._google_secret_set:
                self.google_secret.setPlaceholderText("Saved in Windows Credential Manager - type to replace")
        finally:
            self._loading = False

    def _conflict_changed_text(self) -> None:
        loading, self._loading = self._loading, True
        try:
            self._conflict_changed(self.conflict.currentIndex())
        finally:
            self._loading = loading

    def flush(self) -> None:
        self.mail_schedule.flush()
        self.contacts_schedule.flush()
