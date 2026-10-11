"""Settings > Accounts: connect accounts, schedule and watch their syncs (order 1k, section 10).

Layer: L5

One page, built like the rest of Settings: every control saves at once, every
value has a plain sentence under it, and anything destructive states what it
costs and confirms. The pieces are in `app/ui/accounts/`; this class wires them.

**Cheap to start.** `SettingsView` creates this widget empty. Its content (and the
import of everything under `app/ui/accounts/`) is built the first time the page is
shown, so the window's start-up does not pay for it (6a's budget, order 0r).

**Off the UI thread (rule 5, 10t).** Every read and write of the service runs on a
worker (`accounts/backend.py`). The page reads its state when it is shown and when
a worker reports - never on a timer of its own.

**It never starts a sync by itself.** "Sync now", Approve and Allow go to the
`AccountScheduler`, which is the one gate (one run at a time, D11.2).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QVBoxLayout, QWidget

__all__ = ["AccountsPage", "CATEGORY_ACCOUNTS"]

#: The Settings category this page is the content of (6l).
CATEGORY_ACCOUNTS = "Accounts"


def _reorder(factory: Callable[[], Any], ids: list[int]) -> None:
    factory().accounts.reorder(ids)


class AccountsPage(QWidget):
    #: A setting or an account was saved: the tray re-reads its pause state.
    settings_saved = Signal()
    #: The state was read: (PageState). The runtime uses it for the tray and toasts.
    state_loaded = Signal(object)

    def __init__(self, factory: Callable[[], Any], parent: Optional[QWidget] = None, *,
                 scheduler: Any = None) -> None:
        super().__init__(parent)
        self._factory = factory
        self._scheduler: Any = None
        self._built = False
        self._reading = False
        self._again = False
        self._state: Any = None
        self._select_after = 0
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        if scheduler is not None:
            self.attach_scheduler(scheduler)

    # ------------------------------------------------------------------ build
    def ensure_built(self) -> None:
        if self._built:
            return
        self._built = True
        from app.ui.accounts import parts  # noqa: PLC0415
        from app.ui.accounts.account_list import AccountList  # noqa: PLC0415
        from app.ui.accounts.backend import Backend  # noqa: PLC0415
        from app.ui.accounts.globals_box import GlobalSettingsBox  # noqa: PLC0415
        from app.ui.accounts.panel import AccountPanel  # noqa: PLC0415
        from app.ui.accounts.sync_log import SyncLog  # noqa: PLC0415
        from app.ui.widgets.no_scroll import protect_all  # noqa: PLC0415

        self.backend = Backend(self._factory, self)
        self.backend.changed.connect(self.refresh)
        self.backend.error.connect(self._error)

        self._layout.addWidget(parts.hint(
            "Connect Google, Microsoft, Apple and other accounts to bring their contacts (and read-only "
            "mail) into Leasha. Everything is stored and searched on this computer; nothing is sent "
            "anywhere except to the account you connected. Nothing syncs until you press Sync now or "
            "choose a schedule."))
        self.notice = parts.notice()
        self._layout.addWidget(self.notice)
        self.globals = GlobalSettingsBox(self.backend)
        self._layout.addWidget(self.globals)

        box = QGroupBox("Accounts")
        lay = QVBoxLayout(box)
        self.account_list = AccountList()
        lay.addWidget(self.account_list)
        row = QHBoxLayout()
        self.add_button = parts.btn("Add account", "user-plus", "primary", tip="Connect another account")
        self.sync_all_button = parts.btn("Sync now", "refresh-cw", tip="Sync every account now",
                                         name="Sync all accounts now")
        row.addWidget(self.add_button)
        row.addWidget(self.sync_all_button)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(parts.hint("Each row shows when the account last synced and when it will next. "
                                 "Select a row to change its name, schedule and what it syncs."))
        self.panel = AccountPanel(self.backend)
        lay.addWidget(self.panel)
        self._layout.addWidget(box)

        log_box = QGroupBox("Sync log")
        ll = QVBoxLayout(log_box)
        self.log = SyncLog(self.backend)
        ll.addWidget(self.log)
        self._layout.addWidget(log_box)
        self._layout.addStretch(1)

        self.add_button.clicked.connect(self.open_wizard)
        self.sync_all_button.clicked.connect(lambda: self.request_sync(0, ""))
        self.account_list.account_selected.connect(self._selected)
        self.account_list.reordered.connect(self._reordered)
        self.panel.sync_requested.connect(self.request_sync)
        self.panel.removed.connect(self._removed)
        self.log.approve_requested.connect(lambda aid: self.request_sync(aid, "contacts", approve=True))
        self.log.allow_requested.connect(lambda aid: self.request_sync(aid, "contacts", allow_brake=True))
        protect_all(self)

    def showEvent(self, event: Any) -> None:              # noqa: N802 - Qt's naming
        super().showEvent(event)
        self.ensure_built()
        from app.ui.accounts.parts import restyle_hints  # noqa: PLC0415
        restyle_hints(self)
        self.refresh()

    # --------------------------------------------------------------- scheduler
    def attach_scheduler(self, scheduler: Any) -> None:
        self._scheduler = scheduler
        scheduler.run_finished.connect(self._run_done)
        scheduler.run_started.connect(lambda _why: self.refresh())
        scheduler.run_failed.connect(self._run_failed)
        scheduler.folded.connect(lambda: self._say(
            "A sync is already running. This request was folded into it - it is not queued twice."))

    def request_sync(self, account_id: int = 0, kind: str = "", *, approve: bool = False,
                     allow_brake: bool = False) -> bool:
        """'Sync now' for all accounts, one account, or one kind of one account."""
        if self._scheduler is None:
            from app.ui.account_scheduler import AccountScheduler  # noqa: PLC0415
            self.attach_scheduler(AccountScheduler(self._factory, parent=self))
        started = self._scheduler.sync_now(account_id or None, kind or None, allow_brake=allow_brake,
                                           approve=approve)
        if started:
            self._say("Syncing…")
        return bool(started)

    def _run_done(self, outcomes: Any) -> None:
        failed = [o for o in outcomes if getattr(o, "status", "") == "failed" and getattr(o, "error", None)]
        if failed:
            e = failed[0].error
            self._say(f"{failed[0].account_name}: {e.message} {e.suggestion}".strip())
        else:
            self._say("Sync finished." if outcomes else "")
        self.refresh()

    def _run_failed(self, error: Any) -> None:
        self._error(error)
        self.refresh()

    # -------------------------------------------------------------------- state
    def refresh(self) -> None:
        """Read the state on a worker, then show it. Called when shown and when a worker reports."""
        if not self._built:
            return
        if self._reading:
            self._again = True
            return
        from app.ui.accounts.state import read_page_state  # noqa: PLC0415
        self._reading = True
        self.backend.call(read_page_state, self._factory, serial=True, done=self._apply, fail=self._read_failed)

    def _read_failed(self, error: Any) -> None:
        self._reading = False
        self._error(error)

    def _apply(self, state: Any) -> None:
        self._reading = False
        self._state = state
        self.globals.load(state)
        self.account_list.set_rows(state.statuses)
        if self._select_after:
            self.account_list.select(self._select_after)
            self._select_after = 0
        self.log.set_runs(state.runs, [r.account for r in state.statuses])
        self._show_panel()
        self.state_loaded.emit(state)
        self.settings_saved.emit()
        if self._again:
            self._again = False
            self.refresh()

    def _show_panel(self) -> None:
        aid = self.account_list.current_id()
        row = next((r for r in (self._state.statuses if self._state else []) if r.account.id == aid), None)
        if row is None:
            self.panel.clear()
        else:
            self.panel.show_account(row, self._state, bool(self._state.settings.same_for_all))

    def _selected(self, _aid: int) -> None:
        self._show_panel()

    def _reordered(self, ids: list[int]) -> None:
        self.backend.call(_reorder, self._factory, ids, serial=True, refresh=True)

    def _removed(self) -> None:
        self.panel.clear()

    # ------------------------------------------------------------------- wizard
    def open_wizard(self) -> Any:
        from app.ui.accounts.wizard import AddAccountDialog  # noqa: PLC0415
        from app.ui.widgets.no_scroll import protect_all  # noqa: PLC0415

        self.wizard = AddAccountDialog(self.backend, self)
        protect_all(self.wizard)
        self.wizard.accepted.connect(self._wizard_done)
        self.wizard.open()
        return self.wizard

    def _wizard_done(self) -> None:
        account = self.wizard.account
        if account is not None:
            self._select_after = account.id
            self._say(f"Added {account.name}. Press Sync now to start its first sync - it begins as a dry "
                      "run that writes nothing, and waits for your Approve.")
        self.refresh()

    # ------------------------------------------------------------------- notices
    def _say(self, text: str) -> None:
        if self._built:
            self.notice.setText(text)
            self.notice.setVisible(bool(text))

    def _error(self, error: Any) -> None:
        self._say(f"{getattr(error, 'message', error)} {getattr(error, 'suggestion', '')}".strip())

    def flush(self) -> None:
        """Before the window closes: save anything still inside a debounce window."""
        if self._built:
            self.globals.flush()
