"""What the Accounts page reads, in one pass, off the UI thread (10t).

Layer: L5 (the bodies run on a worker)

`read_page_state` is handed to a `CallableWorker`; nothing here runs on the UI
thread. It is a read: it starts no sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

__all__ = ["PageState", "read_page_state"]


@dataclass
class PageState:
    settings: Any
    statuses: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    kept: dict = field(default_factory=dict)          # account id -> contacts kept there
    last_runs: dict = field(default_factory=dict)     # account id -> last real contacts SyncRun
    mail_space: dict = field(default_factory=dict)    # account id -> bytes, when the mail syncer says
    mail_progress: dict = field(default_factory=dict)  # account id -> {"fetched","total",...}
    google_secret_set: bool = False
    now: datetime = field(default_factory=datetime.now)


def read_page_state(factory: Callable[[], Any], now: Optional[datetime] = None) -> PageState:
    svc = factory()
    now = now or datetime.now()
    st = PageState(svc.settings(), svc.status(now), svc.accounts.runs(limit=300), now=now)
    for row in st.statuses:
        aid = row.account.id
        try:
            st.kept[aid] = int(svc.store.conn.execute(
                "SELECT COUNT(*) FROM contact_links WHERE account_id = ? AND state IN ('synced','pending_create')",
                (aid,)).fetchone()[0])
        except Exception:                                  # noqa: BLE001 - a missing table is zero
            st.kept[aid] = 0
        st.last_runs[aid] = svc.accounts.last_run(aid, "contacts")
        space = getattr(svc, "mail_space_used", None)
        if callable(space):
            st.mail_space[aid] = space(aid)
        extra = svc.accounts.state(aid, "mail").extra or {}
        if extra:
            st.mail_progress[aid] = extra
    try:
        from app.accounts.secrets import client_secret_key  # noqa: PLC0415
        st.google_secret_set = bool(svc.secrets.get(client_secret_key("google")))
    except Exception:                                      # noqa: BLE001 - no keyring here
        st.google_secret_set = False
    return st
