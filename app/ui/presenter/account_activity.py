"""What each account is doing, in plain words: contacts and mail, each with a state, a progress and a next step. Qt-free.

Layer: L5 presenter. Drawn by `app.ui.widgets.activity_table.ActivityTable`, in the Sync window (every account) and in
each account's panel (that account).

The owner (2026-10-12): "contacts and mail sync progress and status for each account is a must, as currently it is
not clear what the account is doing". The one word the accounts list showed - "Waiting" - meant never synced, due
now, queued behind another account's mail, and idle, all at once. Here every account has two lines, one for
contacts and one for mail, and each says exactly one of:

    Running   what it is doing now, how far, how long is left
    Queued    what it is waiting for (another account's mail; the schedule; a pause)
    Paused    who paused it - you, quiet hours, or "pause all sync" - and how to carry on
    Failed    what the account said, and when it tries again
    Sign in   it cannot work until you reconnect
    Stopped   the app closed in the middle; it carries on at the next run
    Idle      up to date: when it last ran, what it did, when it runs next
    Manual / Off / Never   the schedule is manual; it is switched off; it has not run yet

`describe` is a pure function of a snapshot (`AccountService.activity_snapshot`), so every sentence is tested
without a window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

__all__ = ["Activity", "describe", "when_words", "ahead_words", "STATE_ICONS", "STATE_WORDS", "short_status",
           "account_line", "overall_line"]

STATE_WORDS = {
    "running": "Running", "queued": "Waiting its turn", "paused": "Paused", "failed": "Failed",
    "signin": "Sign in needed", "stopped": "Stopped", "idle": "Up to date", "manual": "Manual",
    "off": "Off", "never": "Not run yet",
}
STATE_ICONS = {
    "running": "refresh-cw", "queued": "clock", "paused": "pause", "failed": "circle-alert",
    "signin": "key-round", "stopped": "square", "idle": "circle-check", "manual": "play",
    "off": "ban", "never": "hourglass",
}
_KIND_WORDS = {"contacts": "Contacts", "mail": "Mail"}


@dataclass(frozen=True)
class Activity:
    account_id: int
    name: str
    kind: str                      # contacts | mail
    state: str                     # a key of STATE_WORDS
    headline: str                  # one sentence: what it is doing or why it is not
    detail: str                    # the second line: last run, next run, what is searchable
    fraction: Optional[float]      # 0..1 when a number is known, None when not
    busy: bool                     # True: a moving bar, there is work and no total yet

    @property
    def what(self) -> str:
        return _KIND_WORDS.get(self.kind, self.kind.title())

    @property
    def word(self) -> str:
        return STATE_WORDS.get(self.state, self.state)


# ---------------------------------------------------------------------------------------------- words for times
def when_words(ts: Optional[float], now: float) -> str:
    """"just now", "12 min ago", "today 07:58", "yesterday 21:10", "11 Oct 09:00"; "never" for none."""
    if not ts:
        return "never"
    ago = now - float(ts)
    if ago < 60:
        return "just now"
    if ago < 3600:
        return f"{int(ago // 60)} min ago"
    then, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    days = (today.date() - then.date()).days
    if days == 0:
        return f"today {then:%H:%M}"
    if days == 1:
        return f"yesterday {then:%H:%M}"
    return f"{then.day} {then:%b %H:%M}"


def ahead_words(ts: Optional[float], now: float) -> str:
    """"in 12 min (08:59)", "within a minute", "due now" for a time at or before now; "" for none."""
    if not ts:
        return ""
    gap = float(ts) - now
    if gap <= 0:
        return "due now"
    at = datetime.fromtimestamp(ts)
    if gap < 90:
        return "within a minute"
    if gap < 5400:
        return f"in {int(round(gap / 60))} min ({at:%H:%M})"
    if gap < 86400:
        return f"in {gap / 3600:.1f} h ({at:%H:%M})"
    return f"{at.day} {at:%b %H:%M}"


def _eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds <= 0:
        return ""
    if seconds < 90:
        return "about a minute left"
    if seconds < 5400:
        return f"about {int(round(seconds / 60))} min left"
    if seconds < 172800:
        return f"about {seconds / 3600:.1f} hours left"
    return f"about {seconds / 86400:.1f} days left"


def _counts(run: Optional[Mapping[str, Any]]) -> str:
    if not run:
        return ""
    bits = []
    for key, word in (("added", "added"), ("changed", "changed"), ("deleted", "removed")):
        n = int(run.get(key) or 0)
        if n:
            bits.append(f"{n:,} {word}")
    return ", ".join(bits)


def _short(text: str, limit: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


# --------------------------------------------------------------------------------------------------- the sentence
def describe(snap: Mapping[str, Any], now: float) -> Activity:
    """The one-line state of one account's contacts or mail, from its snapshot. See the module docstring."""
    kind = str(snap.get("kind") or "contacts")
    base = dict(account_id=int(snap.get("account_id") or 0), name=str(snap.get("name") or ""), kind=kind)
    run = snap.get("run") or None
    last_ok, next_at = snap.get("last_success"), snap.get("next")
    last_line = f"Last run {when_words(last_ok, now)}" if last_ok else "Has not finished a run yet"
    nxt = ahead_words(next_at, now)
    tail = " - ".join(x for x in (last_line, f"next {nxt}" if nxt else "") if x)
    counts = _counts(run) if run and run.get("status") in ("ok", "partial") else ""
    searchable = _searchable(snap)

    def done(state: str, headline: str, detail: str = "", *, fraction: Optional[float] = None,
             busy: bool = False) -> Activity:
        return Activity(state=state, headline=headline, detail=detail or tail, fraction=fraction, busy=busy,
                        **base)

    if not snap.get("enabled", True):
        return done("off", f"{_KIND_WORDS.get(kind, kind)} are not kept for this account",
                    "Turn it on in this account's settings.")
    if snap.get("signin_expired"):
        return done("signin", "Sign-in has expired - nothing will sync until you reconnect",
                    "Press Reconnect in this account's settings.")
    if snap.get("live"):
        return _running(snap, base, tail, searchable)
    if snap.get("paused_by_person"):
        return done("paused", "You paused the fetch for this account", "Press Resume fetch to carry on.")
    if snap.get("hold"):
        return done("paused", f"Held back: {snap['hold']}", tail)
    failures = int(snap.get("failures") or 0)
    if run and run.get("status") == "failed" or (failures and not last_ok):
        text = " ".join(str((run or {}).get("error_text") or "the account refused the request").split())
        what, _, fix = text.partition(" FIX:")
        again = f"; trying again {nxt}" if nxt and nxt != "due now" else ""
        return done("failed", f"Failed: {_short(what)}",
                    (f"What to do:{fix.rstrip('. ')}. " if fix.strip() else "") + f"{last_line}{again}")
    if run and run.get("status") in ("interrupted", "stopped"):
        why = "Stopped when the app closed" if run.get("status") == "interrupted" else "Stopped by you"
        return done("stopped", f"{why} - it carries on at the next run", tail)
    if snap.get("mode") == "manual":
        return done("manual", "Manual - it only syncs when you press Sync now", tail + (f". {searchable}" if searchable else ""))
    if not last_ok and not run:
        if snap.get("queued_behind"):
            return done("queued", f"Waiting for {snap['queued_behind']} to finish before it starts",
                        "Mail is fetched one account at a time.")
        return done("never", "Has not run yet - it starts " + (nxt or "at its next scheduled time"), tail)
    if next_at is not None and next_at <= now:
        if snap.get("queued_behind"):
            return done("queued", f"Due now, waiting for {snap['queued_behind']} to finish",
                        "Mail is fetched one account at a time." + (f" {searchable}" if searchable else ""))
        return done("queued", "Due now - it starts within a minute", tail)
    headline = "Up to date" + (f" ({counts} last time)" if counts else "")
    return done("idle", headline, tail + (f". {searchable}" if searchable else ""))


def short_status(a: Activity) -> str:
    """A few words for a list row: "up to date", "fetching 15%", "waiting its turn", "failed"."""
    if a.state == "running":
        return f"{a.word.lower()} {int(a.fraction * 100)}%" if a.fraction is not None else "running"
    return a.word.lower()


def account_line(acts: "list[Activity]") -> str:
    """"Contacts: up to date - Mail: fetching 15%" for one account's rows."""
    return " - ".join(f"{a.what}: {short_status(a)}" for a in acts)


def overall_line(acts: "list[Activity]", snaps: "list[Mapping[str, Any]]") -> str:
    """One sentence about everything: how many rows are in each state, and - once, not on every row - how much
    of the index still waits for the meaning model."""
    if not acts:
        return "No account is set up to sync contacts or mail."
    counts: dict[str, int] = {}
    for a in acts:
        counts[a.state] = counts.get(a.state, 0) + 1
    order = ("running", "queued", "failed", "signin", "paused", "stopped", "never", "manual", "idle")
    bits = [f"{counts[s]} {STATE_WORDS[s].lower()}" for s in order if counts.get(s)]
    line = ", ".join(bits)
    waiting = next((s.get("meaning_waiting") for s in snaps if s.get("meaning_waiting")), None)
    if waiting:
        line += (f". {int(waiting):,} passages are still waiting for the meaning model: fetched mail is searchable by "
                 "its words at once, and the index run adds meaning as it goes.")
    return line


def _searchable(snap: Mapping[str, Any]) -> str:
    """For mail: what is in the index from this account, and what the meaning model still has to do."""
    if snap.get("kind") != "mail":
        return ""
    n = snap.get("messages")
    bits = []
    if n is not None:
        bits.append(f"{int(n):,} message{'s' if int(n) != 1 else ''} searchable by their words")
    return "; ".join(bits)


def _running(snap: Mapping[str, Any], base: Mapping[str, Any], tail: str, searchable: str) -> Activity:
    kind = str(base["kind"])
    live = snap.get("live_progress") or {}
    if kind == "mail":
        mail = snap.get("mail") or {}
        fetched, total = int(mail.get("fetched") or 0), mail.get("total")
        if total:
            frac = min(1.0, fetched / max(1, int(total)))
            eta = _eta(mail.get("eta_s"))
            rate = float(mail.get("rate_per_s") or 0)
            bits = [f"Fetching mail: {fetched:,} of about {int(total):,} ({int(frac * 100)}%)"]
            if rate:
                bits.append(f"{rate:.0f} a second" if rate >= 1 else f"{rate * 60:.0f} a minute")
            if eta:
                bits.append(eta)
            return Activity(state="running", headline=" - ".join(bits),
                            detail=searchable or "Messages are searchable as they arrive.", fraction=frac,
                            busy=False, **base)
        if fetched:
            return Activity(state="running", headline=f"Fetching mail: {fetched:,} messages so far",
                            detail=searchable or "Messages are searchable as they arrive.", fraction=None,
                            busy=True, **base)
        return Activity(state="running", headline="Checking for new mail", detail=searchable or tail,
                        fraction=None, busy=True, **base)
    # contacts
    phase = str(live.get("phase") or "")
    done, total = int(live.get("done") or 0), int(live.get("total") or 0)
    if phase == "writing" and total:
        return Activity(state="running", headline=f"Writing {done:,} of {total:,} contacts to the account",
                        detail=(f"On {live['current']}. " if live.get("current") else "") + tail,
                        fraction=min(1.0, done / total), busy=False, **base)
    if phase == "reading":
        return Activity(state="running", headline=(f"Reading the account - {done:,} contacts so far" if done
                                                   else "Reading the account"),
                        detail=tail, fraction=None, busy=True, **base)
    return Activity(state="running", headline="Syncing contacts now", detail=tail, fraction=None, busy=True, **base)
