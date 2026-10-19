"""The few words of the ambient sync status: the chip on the Contacts and Mail pages, and the tray's line. Qt-free.

Layer: L5 presenter. Drawn by `app.ui.widgets.sync_chip.SyncChip` and `app.ui.tray.TrayPresence`.

Review 2026-10-14, CS-8: what a sync was doing was only in the Sync window (polled every 4 s) and on the Accounts
page while it was open - nothing said "syncing" anywhere else. `SyncTracker` follows the account scheduler's own
signals (`run_started`, `progress`, `run_finished`, `run_failed`) and words one line from them:

    Syncing 2 accounts - writing 120 of 1,436      while a run goes
    Held - look                                    a run held deletes or changes for you
    (nothing)                                      idle - the chip hides; the tray shows no line

IA-5: the Mail page's chip also says what mail is doing, from the activity rows (`mail_words`), because a mail
fetch sends no progress events of its own.

A hold from before start-up: the signals only tell of this session's runs, so a chip that attaches is seeded once
from the accounts' last runs (`SyncTracker.seed`, the activity snapshot read on a worker). A contacts run that held
(delete brake, churn guard) or waits on a clash (`ERR_SYNC_CONFLICT_HELD`) is "Held - look"; one that failed is
"Sync failed - look". The next run's signals replace it, as for any hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

__all__ = ["ChipWords", "SyncTracker", "mail_words", "IDLE_MAIL"]

#: The Mail chip's words when nothing is known yet: it is still the way to the Sync window.
IDLE_MAIL = "Sync status"


@dataclass(frozen=True)
class ChipWords:
    text: str            # "" when there is nothing to say (the chip hides)
    tip: str
    glyph: str
    tone: str            # a theme token: accent_text, warning, danger, text_dim


_NONE = ChipWords("", "", "refresh-cw", "text_dim")


class SyncTracker:
    """What the account scheduler is doing, as its signals tell it. Plain state; no Qt, no I/O."""

    def __init__(self) -> None:
        self.running = False
        self.live: dict[int, dict] = {}          # account id -> its latest contacts progress event
        self.held: list[str] = []                # accounts the last run held, by name
        self.failed = ""                         # the last run's failure, in its own words
        self.heard = False                       # a scheduler signal (or a seed) has said how things stand

    # -- the scheduler's four signals
    def started(self, _why: Any = "") -> None:
        self.heard = True
        self.running, self.held, self.failed = True, [], ""
        self.live.clear()

    def progress(self, event: Mapping[str, Any]) -> None:
        self.running = True
        aid = int(event.get("account_id") or 0)
        self.live.setdefault(aid, {}).update(dict(event))

    def finished(self, outcomes: Optional[Iterable[Any]]) -> None:
        self.heard = True
        self.running = False
        self.live.clear()
        outcomes = list(outcomes or [])
        held = [str(getattr(o, "account_name", "") or "") for o in outcomes if getattr(o, "status", "") == "held"]
        self.held = [h for h in held if h] or (["an account"] if held else [])
        self.failed = ""

    def failed_run(self, error: Any) -> None:
        self.heard = True
        self.running = False
        self.live.clear()
        self.failed = str(getattr(error, "message", None) or error or "")

    # -- before this session's first run
    def seed(self, snapshots: Optional[Iterable[Mapping[str, Any]]]) -> bool:
        """Take the hold or failure of the accounts' last contact runs (`AccountService.activity_snapshot` rows),
        once, and only while no scheduler signal has said anything newer. True when it was taken."""
        if self.heard or self.running:
            return False
        self.heard = True
        held: list[str] = []
        failed = ""
        for snap in snapshots or []:
            if snap.get("kind") != "contacts" or not snap.get("enabled", True):
                continue
            run = snap.get("run") or {}
            name = str(snap.get("name") or "") or "an account"
            if run.get("status") == "held" or run.get("error_code") == "ERR_SYNC_CONFLICT_HELD":
                held.append(name)
            elif run.get("status") == "failed" and not failed:
                failed = str(run.get("error_text") or "").strip() or f"{name}."   # the tip's words need a reason
        self.held, self.failed = held, ("" if held else failed)
        return True

    # -- the words
    def step(self) -> str:
        """"writing 120 of 1,436", "reading the accounts", "checking" - the furthest-on step of the run."""
        writing = [e for e in self.live.values() if e.get("phase") == "writing" and int(e.get("total") or 0)]
        if writing:
            done = sum(int(e.get("done") or 0) for e in writing)
            total = sum(int(e.get("total") or 0) for e in writing)
            return f"writing {done:,} of {total:,}"
        phases = {str(e.get("phase") or "") for e in self.live.values()}
        if "checking" in phases:
            return "checking the accounts"
        if "reading" in phases:
            return "reading the accounts"
        return ""

    def accounts(self) -> int:
        return len([a for a, e in self.live.items() if a and e.get("phase") != "done"])

    def words(self) -> ChipWords:
        """The chip's words now; `text` is "" when idle and nothing is waiting."""
        if self.running:
            n = self.accounts()
            head = f"Syncing {n} account{'s' if n != 1 else ''}" if n else "Syncing"
            step = self.step()
            return ChipWords(head + (f" - {step}" if step else ""),
                             "A sync is running. Click to open the Sync window: every account, how far it has got, "
                             "and Stop", "refresh-cw", "accent_text")
        if self.held:
            return ChipWords("Held - look",
                             f"The last sync held something for you ({', '.join(self.held)}). Click to open the Sync "
                             "window, see what was held and Allow it", "triangle-alert", "warning")
        if self.failed:
            return ChipWords("Sync failed - look", f"The last sync failed: {self.failed} Click to open the Sync window",
                             "circle-alert", "danger")
        return _NONE

    def tray_line(self) -> str:
        """The tray menu's account-sync line (and the tooltip's second line); "" when idle."""
        w = self.words()
        return f"Accounts: {w.text[0].lower()}{w.text[1:]}" if w.text else ""


def mail_words(activities: Iterable[Any], running: bool = False) -> ChipWords:
    """The Mail page's chip, from the mail rows of the activity snapshot (`account_activity.describe`)."""
    rows = [a for a in activities if getattr(a, "kind", "") == "mail"]
    fetching = [a for a in rows if a.state == "running"]
    if fetching:
        fracs = [a.fraction for a in fetching if a.fraction is not None]
        pct = f" {int(min(fracs) * 100)}%" if fracs else ""
        who = fetching[0].name if len(fetching) == 1 else f"{len(fetching)} accounts"
        return ChipWords(f"Mail: fetching{pct}", f"Fetching mail from {who}: {fetching[0].headline}. Click to open "
                         "the Sync window", "refresh-cw", "accent_text")
    stuck = [a for a in rows if a.state in ("failed", "signin")]
    if stuck:
        return ChipWords("Mail: needs you", f"{stuck[0].name}: {stuck[0].headline}. Click to open the Sync window",
                         "circle-alert", "danger")
    if running:
        return ChipWords("Syncing", "A sync is running. Click to open the Sync window", "refresh-cw", "accent_text")
    if rows and all(a.state in ("idle", "manual", "partial", "mirror") for a in rows):
        return ChipWords("Mail: up to date", "Every account's mail is fetched. Click to open the Sync window",
                         "circle-check", "text_dim")
    return ChipWords(IDLE_MAIL, "Open the Sync window: what every account's contacts and mail are doing now",
                     "refresh-cw", "text_dim")
