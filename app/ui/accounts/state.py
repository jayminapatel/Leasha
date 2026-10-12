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
    #: account id -> `ContactHub.account_counts()[id]` (order 1t 3a): kept, linked, waiting, deleted there.
    contact_counts: dict = field(default_factory=dict)
    #: account id -> the last check against the account (`AccountStore.check`), or absent if never checked.
    checks: dict = field(default_factory=dict)
    last_runs: dict = field(default_factory=dict)     # account id -> last real contacts SyncRun
    mail_space: dict = field(default_factory=dict)    # account id -> bytes, when the mail syncer says
    mail_progress: dict = field(default_factory=dict)  # account id -> {"fetched","total",...}
    #: `AccountService.activity_snapshot`: what each account's contacts and mail are doing.
    activities: list = field(default_factory=list)
    google_secret_set: bool = False
    now: datetime = field(default_factory=datetime.now)


def read_page_state(factory: Callable[[], Any], now: Optional[datetime] = None) -> PageState:
    svc = factory()
    now = now or datetime.now()
    st = PageState(svc.settings(), svc.status(now), svc.accounts.runs(limit=300), now=now)
    try:
        st.activities = svc.activity_snapshot(now)
    except Exception:                                      # noqa: BLE001 - a label; the page opens without it
        st.activities = []
    try:
        counts = svc.hub.account_counts()                  # order 1t 3a: the one definition, as Contacts shows it
    except Exception:                                      # noqa: BLE001 - a missing table is zero
        counts = {}
    for row in st.statuses:
        aid = row.account.id
        st.contact_counts[aid] = dict(counts.get(aid) or {})
        st.kept[aid] = int(st.contact_counts[aid].get("kept") or 0)
        try:
            found = svc.accounts.check(aid)
        except Exception:                                  # noqa: BLE001 - a label; the page opens without it
            found = None
        if found:
            st.checks[aid] = found
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
