r"""`leasha accounts` - connect, schedule and sync accounts, headless.

Layer: L7

Order 1k 1g and section 10 (rule 8: the command line ships before the window).

    accounts add google|microsoft [--name Work] [--mail] [--no-contacts]
    accounts add apple|imap|carddav --address me@x.com [--server S --port P --security ssl|starttls|none]
    accounts list | status
    accounts signin <account>             sign in again (Google, Microsoft)
    accounts password <account>           a new app password (Apple, IMAP, CardDAV)
    accounts remove <account> [--drop-contacts] [--keep-mail]
    accounts set <account> <key> <value>  name | colour | direction | mail | contacts
    accounts schedule [--mail M] [--contacts M] [--same-for-all yes|no] [--pause P] [--quiet A-B|off]
    accounts config [--google-client-id ID] [--microsoft-client-id ID] [--default-account A] ...
    accounts due                          what the schedule would start now (starts nothing)

A password is never an argument (it would sit in shell history and in the run log):
it is read from the terminal without echo, or from stdin with `--password-stdin`.
Client secrets are the same. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime
from typing import Any, Optional

from app.cli._common import EXIT_ERROR, EXIT_OK, _load
from app.core.logging import setup_logging

__all__ = ["add_accounts_parser", "cmd_accounts", "open_service"]

_SCHEDULE_HELP = "manual | every:<minutes> | daily:HH:MM   (e.g. every:60)"


def open_service(args: argparse.Namespace):
    """(store, service) for this run. The caller closes the store."""
    from app.accounts.service import AccountService  # noqa: PLC0415
    from app.storage.sqlite_store import SqliteStore  # noqa: PLC0415

    settings = _load(args)
    setup_logging(settings.log_path)
    store = SqliteStore(settings.fts_db).connect()
    return store, AccountService(store)


def _secret(prompt: str, from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass(prompt)


def _schedule(text: str):
    from app.accounts.store import ScheduleSetting  # noqa: PLC0415
    t = (text or "manual").strip().lower()
    if t == "manual":
        return ScheduleSetting("manual")
    if t.startswith("every:"):
        return ScheduleSetting("interval", max(1, int(t.split(":", 1)[1])))
    if t.startswith("daily:"):
        return ScheduleSetting("daily", 60, t.split(":", 1)[1])
    raise SystemExit(f"schedule must be {_SCHEDULE_HELP}")


def _resolve(svc: Any, ref: str):
    acct = svc.accounts.find(ref)
    if acct is None:
        raise SystemExit(f"no account called {ref!r}; try `accounts list`")
    return acct


def _row(a: Any) -> dict[str, Any]:
    return {"id": a.id, "name": a.name, "provider": a.provider, "address": a.address,
            "mail": a.mail_on, "contacts": a.contacts_on, "direction": a.contacts_direction,
            "last_sync": a.last_synced_at, "last_error": a.last_error_code}


def cmd_accounts(args: argparse.Namespace) -> int:
    store, svc = open_service(args)
    try:
        return _dispatch(args, svc)
    finally:
        store.close()


def _dispatch(args: argparse.Namespace, svc: Any) -> int:
    as_json = getattr(args, "json", False)
    action = args.accounts_action

    if action == "list":
        rows = [_row(a) for a in svc.accounts.list()]
        if as_json:
            print(json.dumps(rows, indent=2))
        for r in rows if not as_json else []:
            print(f"{r['id']:>3}  {r['name']:<16} {r['provider']:<10} {r['address']:<30} "
                  f"mail={'on' if r['mail'] else 'off'} contacts={r['direction'] if r['contacts'] else 'off'}")
        if not rows and not as_json:
            print("No accounts yet. `accounts add google` (or microsoft, apple, imap, carddav).")
        return EXIT_OK

    if action == "status":
        now = datetime.now()
        out = []
        for st in svc.status(now):
            out.append({"account": st.account.name, "status": st.word,
                        "last_sync": st.last_sync.isoformat(timespec="minutes") if st.last_sync else None,
                        "next_sync": st.next_sync.isoformat(timespec="minutes") if st.next_sync else None,
                        "schedule": st.schedule_text, "held": st.hold, "warning": st.warning,
                        "error": st.account.last_error})
        if as_json:
            print(json.dumps(out, indent=2))
        else:
            for o in out:
                print(f"{o['account']:<16} {o['status']:<14} last {o['last_sync'] or '-':<17} "
                      f"next {o['next_sync'] or '-':<17} {o['schedule']}")
                for k in ("held", "warning", "error"):
                    if o[k]:
                        print(f"{'':<16} {k}: {o[k]}")
        return EXIT_OK

    if action == "add":
        provider = args.provider
        if provider in ("google", "microsoft"):
            acct = svc.add_oauth_account(
                provider, args.name or "", mail=args.mail, contacts=not args.no_contacts,
                direction=args.direction, login_hint=args.address or "",
                on_url=lambda url: print(f"If your browser does not open, go to:\n{url}", file=sys.stderr))
        else:
            if not args.address:
                raise SystemExit("--address is required")
            pw = _secret(f"App password for {args.address}: ", args.password_stdin)
            acct = svc.add_password_account(
                provider, args.address, pw, args.name or "", server=args.server, port=args.port,
                security=args.security, mail=args.mail, contacts=not args.no_contacts,
                direction=args.direction, options={k: v for k, v in (("addressbook_url", args.addressbook_url),)
                                                   if v})
        print(f"Added {acct.name} ({acct.provider}, id {acct.id}).")
        return EXIT_OK

    if action == "signin":
        acct = _resolve(svc, args.account)
        svc.reconnect(acct.id, on_url=lambda url: print(f"If your browser does not open, go to:\n{url}",
                                                          file=sys.stderr))
        print(f"Signed in {acct.name}.")
        return EXIT_OK

    if action == "password":
        acct = _resolve(svc, args.account)
        svc.set_app_password(acct.id, _secret(f"New app password for {acct.address}: ", args.password_stdin))
        print("Saved to Windows Credential Manager.")
        return EXIT_OK

    if action == "remove":
        acct = _resolve(svc, args.account)
        svc.remove_account(acct.id, keep_contacts=not args.drop_contacts, remove_mail=not args.keep_mail)
        print(f"Removed {acct.name}. Contacts {'binned' if args.drop_contacts else 'kept in Leasha'}; "
              f"mail {'kept' if args.keep_mail else 'taken out of the index'}.")
        return EXIT_OK

    if action == "set":
        acct = _resolve(svc, args.account)
        key, value = args.key, args.value
        mapping = {"name": ("name", value), "colour": ("colour", value),
                   "direction": ("contacts_direction", value),
                   "mail": ("mail_on", value.lower() in ("on", "yes", "true", "1")),
                   "contacts": ("contacts_on", value.lower() in ("on", "yes", "true", "1"))}
        if key in ("mail-schedule", "contacts-schedule"):
            sc = _schedule(value)
            svc.accounts.update(acct.id, options={f"{key.split('-')[0]}_schedule": sc.__dict__})
        elif key in mapping:
            col, val = mapping[key]
            svc.accounts.update(acct.id, **{col: val})
        else:
            raise SystemExit(f"unknown key {key!r}: name colour direction mail contacts mail-schedule contacts-schedule")
        print("Saved.")
        return EXIT_OK

    if action == "schedule":
        changes: dict[str, Any] = {}
        if args.mail:
            changes["mail"] = _schedule(args.mail)
        if args.contacts:
            changes["contacts"] = _schedule(args.contacts)
        if args.same_for_all:
            changes["same_for_all"] = args.same_for_all.lower() in ("yes", "true", "on")
        if args.pause:
            from app.accounts import policy as pol  # noqa: PLC0415
            if args.pause == "off":
                changes["paused_until"] = None
            else:
                until = pol.pause_until(datetime.now(), {"hour": "hour", "tomorrow": "tomorrow",
                                                         "forever": "forever"}.get(args.pause, "hour"))
                changes["paused_until"] = (9e15 if until == datetime.max else until.timestamp())
        if args.quiet:
            if args.quiet == "off":
                changes["quiet_on"] = False
            else:
                a, _, b = args.quiet.partition("-")
                changes.update(quiet_on=True, quiet_start=a, quiet_end=b)
        if changes:
            svc.accounts.update_settings(**changes)
        s = svc.settings()
        print(f"mail: {s.mail.mode} {s.mail.minutes if s.mail.mode == 'interval' else s.mail.daily_at}  "
              f"contacts: {s.contacts.mode} {s.contacts.minutes if s.contacts.mode == 'interval' else s.contacts.daily_at}  "
              f"same for all: {s.same_for_all}  quiet: {'%s-%s' % (s.quiet_start, s.quiet_end) if s.quiet_on else 'off'}  "
              f"paused: {svc.hold_reason(datetime.now()) or 'no'}")
        return EXIT_OK

    if action == "config":
        changes = {}
        if args.google_client_id is not None:
            changes["google_client_id"] = args.google_client_id
        if args.microsoft_client_id is not None:
            changes["microsoft_client_id"] = args.microsoft_client_id
        if args.default_account:
            changes["default_account_id"] = _resolve(svc, args.default_account).id
        for flag, name in (("conflict_policy", "conflict_policy"), ("phone_region", "phone_region"),
                           ("maps_site", "maps_site")):
            if getattr(args, flag):
                changes[name] = getattr(args, flag)
        if args.brake_count is not None:
            changes["brake_count"] = args.brake_count
        if args.brake_percent is not None:
            changes["brake_percent"] = args.brake_percent
        if args.dry_run_first:
            changes["dry_run_first"] = args.dry_run_first.lower() in ("yes", "on", "true")
        if args.google_client_secret_stdin:
            from app.accounts.secrets import client_secret_key  # noqa: PLC0415
            svc.secrets.put(client_secret_key("google"), sys.stdin.readline().rstrip("\r\n"))
        if changes:
            svc.accounts.update_settings(**changes)
        d = svc.settings()
        print(json.dumps({k: v for k, v in d.__dict__.items() if k not in ("mail", "contacts")}
                         | {"google_client_secret": "set" if svc.secrets.get(
                             __import__("app.accounts.secrets", fromlist=["x"]).client_secret_key("google")) else "not set"},
                         indent=2, default=str))
        return EXIT_OK

    if action == "due":
        work = svc.due_work(datetime.now())
        for w in work:
            print(f"{w.account.name}: {w.kind} ({w.reason})")
        if not work:
            print("Nothing is due." + (f" ({svc.hold_reason(datetime.now())})" if svc.hold_reason(datetime.now()) else ""))
        return EXIT_OK

    return EXIT_ERROR


def add_accounts_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser("accounts", parents=[common],
                       help="connect Google, Microsoft, Apple, IMAP and CardDAV accounts; schedules")
    a = p.add_subparsers(dest="accounts_action", required=True)
    a.add_parser("list", parents=[common], help="the accounts")
    a.add_parser("status", parents=[common], help="status, last and next sync of each account")
    a.add_parser("due", parents=[common], help="what the schedule would start now (starts nothing)")

    add = a.add_parser("add", parents=[common], help="connect an account")
    add.add_argument("provider", choices=["google", "microsoft", "apple", "imap", "carddav"])
    add.add_argument("--address", help="the account's address / login")
    add.add_argument("--name", help='your name for it ("Work", "Home")')
    add.add_argument("--server")
    add.add_argument("--port", type=int)
    add.add_argument("--security", choices=["ssl", "starttls", "none"])
    add.add_argument("--addressbook-url", help="CardDAV: the address book's URL if discovery is not enough")
    add.add_argument("--mail", action="store_true", help="also fetch mail (read only)")
    add.add_argument("--no-contacts", action="store_true")
    add.add_argument("--direction", choices=["two-way", "in-only", "off"], default="two-way")
    add.add_argument("--password-stdin", action="store_true", help="read the app password from stdin")
    add.set_defaults()

    si = a.add_parser("signin", parents=[common], help="sign in again")
    si.add_argument("account")
    pw = a.add_parser("password", parents=[common], help="a new app password")
    pw.add_argument("account")
    pw.add_argument("--password-stdin", action="store_true")
    rm = a.add_parser("remove", parents=[common], help="sign out and remove an account")
    rm.add_argument("account")
    rm.add_argument("--drop-contacts", action="store_true", help="bin the contacts only this account held")
    rm.add_argument("--keep-mail", action="store_true", help="leave its mail in the index")
    st = a.add_parser("set", parents=[common], help="change one setting of an account")
    st.add_argument("account")
    st.add_argument("key")
    st.add_argument("value")
    sc = a.add_parser("schedule", parents=[common], help="the sync schedules; pause; quiet hours")
    sc.add_argument("--mail", help=_SCHEDULE_HELP)
    sc.add_argument("--contacts", help=_SCHEDULE_HELP)
    sc.add_argument("--same-for-all", choices=["yes", "no"])
    sc.add_argument("--pause", choices=["hour", "tomorrow", "forever", "off"])
    sc.add_argument("--quiet", help="HH:MM-HH:MM or off")
    cf = a.add_parser("config", parents=[common], help="client IDs, conflict rule, safety limits, defaults")
    cf.add_argument("--google-client-id")
    cf.add_argument("--microsoft-client-id")
    cf.add_argument("--google-client-secret-stdin", action="store_true")
    cf.add_argument("--default-account")
    cf.add_argument("--conflict-policy", choices=["newest", "priority", "ask"])
    cf.add_argument("--phone-region")
    cf.add_argument("--maps-site", choices=["google", "openstreetmap", "apple", "bing"])
    cf.add_argument("--brake-count", type=int)
    cf.add_argument("--brake-percent", type=float)
    cf.add_argument("--dry-run-first", choices=["yes", "no"])
    p.set_defaults(func=cmd_accounts)
