"""What Settings > Accounts decides, with no Qt in it.

Layer: L5 (decisions only)

Every sentence, choice list and rule the page and the tray use lives here so it
is tested without a display: the schedule choices (10g), the pause choices (10h),
the words of an account row (10o), the sync-log row and its CSV (10s), which
toasts to send (10m) and where the user guide's set-up steps are (1a).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from app.accounts import policy as pol

__all__ = [
    "MODE_CHOICES", "PAUSE_CHOICES", "CONFLICT_CHOICES", "MAPS_CHOICES", "DIRECTION_CHOICES",
    "BACK_CHOICES", "GUIDE_ANCHORS", "PROVIDER_TEXT", "interval_text", "interval_items",
    "pause_value", "hold_text", "when_text", "RowView", "row_view", "provider_glyph", "kind_chips",
    "signin_text", "run_row", "plan_text", "fix_text", "runs_csv", "Toast", "toasts_for",
    "schedule_to_option", "schedule_from_option", "guide_path", "can_undo", "can_approve",
    "can_allow", "duration_text", "trigger_text", "ENRICH_OWN", "ENRICH_NET",
]

MODE_CHOICES = (("manual", "Manual - only when I press Sync now"),
                ("interval", "Every few minutes or hours"),
                ("daily", "Daily at a time"))

PAUSE_CHOICES = (("hour", "For 1 hour"), ("tomorrow", "Until tomorrow"),
                 ("forever", "Until I turn it back on"))

CONFLICT_CHOICES = (("newest", "The newest edit wins"),
                    ("priority", "The account higher in the list wins"),
                    ("ask", "Ask me every time"))

#: Section 7's switches: (AccountSettings field, label, the sentence under it). The first five
#: read only your own index and are on by default; the last three go to the internet, are off by
#: default, and each says exactly what leaves the computer.
ENRICH_OWN = (
    ("enrich_signatures", "Email signatures",
     "Read phone, title, company, address and website from the newest signature in mail from the person."),
    ("enrich_company", "Company from the email domain",
     "Fill a blank company from a work email address (free mail such as gmail.com is skipped)."),
    ("enrich_faces", "Photo from named faces", "Offer a photo from the person's named faces in your photos."),
    ("enrich_history", "Contact history", "Show first and last contact, how often, and who else is on the thread."),
    ("enrich_suggest", "Suggested contacts", "List people who mail you often and are in no contact."),
)
ENRICH_NET = (
    ("enrich_gravatar", "Gravatar",
     "A SHA-256 hash of the email address is sent to Gravatar, which may answer with the photo and public "
     "profile the person published."),
    ("enrich_logo", "Company logo",
     "Fetches the website's own icon. Only the company's own domain is contacted."),
    ("enrich_wikidata", "Wikidata",
     "The company name is looked up; companies only, never people. Website, headquarters and industry."),
)

MAPS_CHOICES = (("openstreetmap", "OpenStreetMap"), ("google", "Google Maps"),
                ("apple", "Apple Maps"), ("bing", "Bing Maps"))

DIRECTION_CHOICES = (("two-way", "Two-way"), ("in-only", "Into Leasha only"), ("off", "Off"))

BACK_CHOICES = (("all", "Everything"), ("1y", "The last year"), ("2y", "The last 2 years"),
                ("5y", "The last 5 years"), ("10y", "The last 10 years"), ("since", "Since a date"))

#: Sections of the user guide that explain a step (1a), by what asks for them.
GUIDE_ANCHORS = {
    "google": "accounts-google-setup",
    "microsoft": "accounts-microsoft-setup",
    "apple": "accounts-icloud-password",
    "imap": "accounts-imap",
    "carddav": "accounts-imap",
}

#: The wizard's choices: key -> (title, glyph, one line, help). A glyph, never a logo.
PROVIDER_TEXT: dict[str, tuple[str, str, str, str]] = {
    "google": ("Google", "mail", "Gmail and Google Contacts, signed in through your browser.",
               "You sign in on Google's own page; Leasha never sees your password. This needs your "
               "own Google client ID and secret (below on this page). Google shows an 'unverified "
               "app' screen for an app you made yourself - continue past it."),
    "google-imap": ("Read Gmail over IMAP instead", "inbox",
                    "Mail only, with a Google app password instead of the Gmail permission.",
                    "App passwords exist only for personal Google accounts with 2-Step Verification "
                    "switched on. Work and school accounts cannot make one: use Google above."),
    "microsoft": ("Microsoft", "mail", "Outlook.com, Microsoft 365 and Exchange Online, through your browser.",
                  "Personal, work and school accounts all work. A work or school account may need "
                  "your administrator to approve the app first; if so Leasha says exactly what to ask."),
    "apple": ("Apple (iCloud)", "contact", "iCloud mail and contacts with an app-specific password.", ""),
    "imap": ("Other IMAP", "server", "Any mail server: Yahoo, Fastmail, a company or hosting server.",
             "Enter the server, port and security your provider gives, and an app password if it "
             "offers them. Mail is read only: Leasha never changes the mailbox."),
    "carddav": ("Other CardDAV", "server", "Any contacts server: Fastmail, Nextcloud and the like.",
                "Enter the server and an app password. If Leasha cannot find the address book by "
                "itself, paste its address."),
}

GMAIL_IMAP = {"server": "imap.gmail.com", "port": 993, "security": "ssl"}


def interval_text(minutes: int) -> str:
    m = int(minutes)
    if m % 60 == 0:
        h = m // 60
        return f"{h} hour" + ("" if h == 1 else "s")
    return f"{m} minute" + ("" if m == 1 else "s")


def interval_items(current: Optional[int] = None) -> list[tuple[int, str]]:
    """The interval choices; a saved value outside them (the CLI accepts any) is kept as one more."""
    values = list(pol.INTERVAL_CHOICES_MIN)
    if current and current not in values:
        values.append(int(current))
        values.sort()
    return [(m, interval_text(m)) for m in values]


def schedule_to_option(sc: Any) -> dict:
    return {"mode": sc.mode, "minutes": int(sc.minutes), "daily_at": sc.daily_at}


def schedule_from_option(value: Any, fallback: Any) -> Any:
    from app.accounts.store import ScheduleSetting  # noqa: PLC0415
    if isinstance(value, dict):
        return ScheduleSetting(**{k: v for k, v in value.items() if k in ScheduleSetting.__dataclass_fields__})
    return fallback


def pause_value(choice: str, now: datetime) -> Optional[float]:
    """The `paused_until` setting for a pause choice (`9e15`: until turned back on)."""
    until = pol.pause_until(now, choice)
    if until is None:
        return None
    return 9e15 if until == datetime.max else until.timestamp()


def hold_text(settings: Any, now: datetime) -> Optional[str]:
    """Why a scheduled sync is held right now, in words (10h, 10i), else None."""
    until = None
    if settings.paused_until is not None:
        until = datetime.max if settings.paused_until >= 9e15 else datetime.fromtimestamp(settings.paused_until)
    return pol.hold_reason(
        now, quiet_start=pol.parse_hhmm(settings.quiet_start) if settings.quiet_on else None,
        quiet_end=pol.parse_hhmm(settings.quiet_end) if settings.quiet_on else None, paused_until=until)


def when_text(value: Any) -> str:
    """A moment in the format chosen in Settings > Window; blank when there is none."""
    from app.core.date_format import format_seconds  # noqa: PLC0415
    if value is None:
        return ""
    ts = value.timestamp() if isinstance(value, datetime) else value
    return format_seconds(ts)


def provider_glyph(account: Any) -> str:
    """A glyph from icons.py for the kind of account - never a provider's logo."""
    if account.provider in ("imap", "carddav"):
        return "server"
    return "contact" if account.contacts_on and not account.mail_on else "mail"


def kind_chips(account: Any) -> list[tuple[str, str]]:
    chips = []
    if account.mail_on:
        chips.append(("mail", "Mail"))
    if account.contacts_on and account.contacts_direction != "off":
        chips.append(("contact", "Contacts"))
    if account.provider in ("imap", "carddav"):
        chips.append(("server", account.server or "Server"))
    return chips


@dataclass
class RowView:
    account_id: int
    name: str
    colour: str
    glyph: str
    type_text: str
    address: str
    word: str
    word_icon: str
    last: str
    next: str
    note: str
    warn: bool


def row_view(status: Any, now: Optional[datetime] = None) -> RowView:
    """One line of the account list (10o): the words come from `AccountService.status`."""
    a = status.account
    note, warn = "", False
    if status.hold and status.next_sync is not None:
        note = f"{status.hold} - holds the next scheduled sync."
    if status.warning:
        note, warn = (note + " " + status.warning).strip(), True
    if status.word == "Needs sign-in":
        note, warn = "Press Reconnect in this account's panel.", True
    nxt = when_text(status.next_sync) if status.next_sync else ("Manual" if not status.hold else "")
    return RowView(a.id, a.name, a.colour, provider_glyph(a), PROVIDER_TEXT.get(a.provider, (a.provider,))[0],
                   a.address, status.word, status.icon, when_text(status.last_sync) or "Never", nxt, note, warn)


def signin_text(account: Any, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    if account.provider not in ("google", "microsoft"):
        return "Signed in with an app password kept in Windows Credential Manager."
    if account.last_error_code == "ERR_ACCOUNT_SIGNIN_EXPIRED":
        return "The sign-in has expired or was revoked. Press Reconnect."
    exp = account.options.get("signin_expires_at")
    since = account.options.get("signed_in_at")
    text = "Signed in" + (f" on {when_text(since)}" if since else "")
    if exp:
        text += f"; the sign-in expires {when_text(exp)}"
    else:
        text += "; no expiry date is known - Leasha asks you again only if the provider does"
    return text + "."


# ----------------------------------------------------------------- sync log
_TRIGGERS = {"sync-now": "Sync now", "schedule": "Schedule", "schedule (catch-up)": "Schedule (catch-up)",
             "approved": "Approved first sync", "undo": "Undo", "conflict": "Your choice"}


def trigger_text(trigger: str) -> str:
    return _TRIGGERS.get(trigger, trigger.capitalize())


def duration_text(run: Any) -> str:
    if not run.finished_at:
        return "Running" if run.status == "running" else ""
    s = max(0, int(run.finished_at - run.started_at))
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60} s"


def fix_text(run: Any) -> str:
    """The FIX of a failed or held run (rule 2)."""
    if not run.error_code and not run.error_text:
        return ""
    text = run.error_text or ""
    if " FIX: " in text:
        return text.split(" FIX: ", 1)[1]
    if run.error_code:
        from app.core.errors import make_error  # noqa: PLC0415
        return make_error(run.error_code, "ui.accounts", account=run.account_name, count=run.held).suggestion
    return ""


def _error_message(run: Any) -> str:
    return (run.error_text or "").split(" FIX: ", 1)[0]


_STATUS_WORDS = {"ok": "Done", "held": "Held", "failed": "Failed", "dry-run": "Dry run",
                 "needs-approval": "Waiting for you", "running": "Running"}


def run_row(run: Any) -> dict[str, str]:
    result = _STATUS_WORDS.get(run.status, run.status)
    if run.undone_at:
        result = "Undone"
    return {"time": when_text(run.started_at), "account": run.account_name, "kind": run.kind.capitalize(),
            "trigger": trigger_text(run.trigger), "added": str(run.added), "changed": str(run.changed),
            "deleted": str(run.deleted), "held": str(run.held), "took": duration_text(run),
            "result": result, "error": _error_message(run), "fix": fix_text(run)}


def plan_text(run: Any) -> str:
    """What a dry run would do, or what a held run was about to do, in a sentence."""
    p = run.plan or {}
    if not p:
        return ""
    parts = []
    for key, text in (("add_in", "add {} to Leasha"), ("merge_in", "change {} in Leasha"),
                      ("possible_duplicates", "{} look like contacts you already have"),
                      ("create_out", "create {} in the account"), ("update_out", "change {} in the account"),
                      ("delete_out", "delete {} from the account"),
                      ("flagged_deleted", "list {} as deleted elsewhere"), ("conflicts", "{} changed in two places")):
        if p.get(key):
            parts.append(text.format(p[key]))
    head = "It would " if run.dry_run or run.status in ("dry-run", "needs-approval") else "It was about to "
    body = head + (", ".join(parts) if parts else "change nothing") + "."
    names = [n for bucket in ("delete_out", "add_in") for n in (p.get("names") or {}).get(bucket, [])][:8]
    return body + (" For example: " + ", ".join(names) + "." if names else "")


def can_undo(run: Any) -> bool:
    return run.status == "ok" and not run.dry_run and not run.undone_at and (run.added or run.changed or run.deleted)


def can_approve(run: Any, latest_for_account: bool = True) -> bool:
    return run.status == "needs-approval" and latest_for_account


def can_allow(run: Any) -> bool:
    return run.status == "held"


def runs_csv(runs: Iterable[Any]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Time", "Account", "Kind", "Started by", "Added", "Changed", "Deleted", "Held",
                "Took", "Result", "Error", "Fix"])
    for r in runs:
        d = run_row(r)
        w.writerow([d["time"], d["account"], d["kind"], d["trigger"], d["added"], d["changed"], d["deleted"],
                    d["held"], d["took"], d["result"], d["error"], d["fix"]])
    return out.getvalue()


# ------------------------------------------------------------- notifications
@dataclass(frozen=True)
class Toast:
    key: str
    title: str
    text: str


def toasts_for(settings: Any, statuses: Iterable[Any], outcomes: Iterable[Any], now: datetime,
               told: set[str]) -> list[Toast]:
    """The Windows toasts to send now (10m), honouring each switch; nothing for an ordinary success.

    `told` is the set of keys already sent; a condition that has cleared is forgotten so it can
    be told again if it comes back. It is updated in place."""
    out: list[Toast] = []
    persistent: set[str] = set()

    def add(key: str, title: str, text: str) -> None:
        if key not in told:
            told.add(key)
            out.append(Toast(key, title, text))

    for o in outcomes or ():
        if getattr(o, "status", "") == "held" and settings.notify_brake:
            add(f"brake:{o.account_id}:{o.run_id}", "Sync held",
                f"{o.account_name}: this sync would delete more contacts than your safety limit, so "
                "nothing was deleted. Open Settings > Accounts to look.")
        plan = getattr(o, "plan", None)
        if plan is not None and (plan.conflicts or plan.held_conflicts) and settings.notify_conflict:
            add(f"conflict:{o.account_id}:{o.run_id}", "Changed in two places",
                f"{o.account_name}: a contact was changed in two places. Open Contacts to see it.")
    for st in statuses or ():
        a = st.account
        exp = a.options.get("signin_expires_at")
        soon = bool(exp) and 0 < float(exp) - now.timestamp() <= 2 * 86400
        if settings.notify_signin and (st.word == "Needs sign-in" or soon):
            key = f"signin:{a.id}:{'expired' if st.word == 'Needs sign-in' else 'soon'}"
            persistent.add(key)
            add(key, "Sign in again",
                f"{a.name}: the sign-in has expired. Press Reconnect in Settings > Accounts."
                if st.word == "Needs sign-in" else
                f"{a.name}: the sign-in expires within 2 days. Press Reconnect in Settings > Accounts.")
        if settings.notify_failed_24h and st.warning:
            key = f"failing:{a.id}"
            persistent.add(key)
            add(key, "Sync is failing", f"{a.name} has not synced for more than 24 hours. "
                "Open Settings > Accounts to see why.")
    told.intersection_update(persistent | {k for k in told if k.startswith(("brake:", "conflict:"))})
    return out


def guide_path() -> Optional[Path]:
    """The user guide's file, or None when this copy has none."""
    for root in (Path(__file__).resolve().parents[3], ):
        p = root / "docs" / "USER_GUIDE.html"
        if p.is_file():
            return p
    try:
        from app.ui.tray import assets_dir  # noqa: PLC0415
        p = assets_dir().parent / "docs" / "USER_GUIDE.html"
        return p if p.is_file() else None
    except Exception:                                    # noqa: BLE001
        return None
