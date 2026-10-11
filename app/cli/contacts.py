r"""`leasha contacts` - the contact book, headless.

Layer: L7

Order 1k 3h (rule 8).

    contacts sync [--account X] [--dry-run] [--approve-first] [--allow-brake]
    contacts log [--account X] [--limit N]
    contacts undo <sync-log-id>          put a whole sync back (the accounts too)
    contacts undo --entry <journal-id>   undo one change
    contacts list [--text T] [--view V]  search / list the book
    contacts show <id>
    contacts history <id>

`sync --dry-run` reads and plans and writes nothing: not Leasha, not the account,
not the saved position. The first sync of an account is a dry run by itself until
`--approve-first`.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

from app.cli._common import EXIT_ERROR, EXIT_OK
from app.cli.accounts import open_service

__all__ = ["add_contacts_parser", "cmd_contacts"]


def _when(ts: Any) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def cmd_contacts(args: argparse.Namespace) -> int:
    store, svc = open_service(args)
    try:
        return _dispatch(args, svc)
    finally:
        store.close()


def _dispatch(args: argparse.Namespace, svc: Any) -> int:
    as_json = getattr(args, "json", False)
    action = args.contacts_action

    if action == "sync":
        ids = None
        if args.account:
            acct = svc.accounts.find(args.account)
            if acct is None:
                raise SystemExit(f"no account called {args.account!r}")
            ids = [acct.id]
        if args.dry_run:
            outcomes = svc.engine.sync(ids, trigger="cli", dry_run=True)
        else:
            outcomes = svc.sync_now(ids[0] if ids else None, "contacts", trigger="cli",
                                    approve_first=args.approve_first, allow_brake=args.allow_brake)
        rows = []
        for o in outcomes:
            p = o.plan
            rows.append({"account": o.account_name, "status": o.status, "run": o.run_id,
                         "added": p.added, "changed": p.changed, "deleted": p.deleted, "held": p.held,
                         "possible_duplicates": p.possible_duplicates,
                         "error": o.error.render() if o.error else None})
        if as_json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(f"{r['account']}: {r['status']}  +{r['added']} ~{r['changed']} -{r['deleted']} "
                      f"held {r['held']}  (log #{r['run']})")
                if r["possible_duplicates"]:
                    print(f"    {r['possible_duplicates']} new contact(s) look like ones already here")
                if r["error"]:
                    print("    " + r["error"].replace("\n", "\n    "))
            if not rows:
                print("No account has contact sync on (or a sync of it is already running).")
        return EXIT_ERROR if any(r["status"] in ("failed",) for r in rows) else EXIT_OK

    if action == "log":
        acct = svc.accounts.find(args.account) if args.account else None
        runs = svc.accounts.runs(account_id=acct.id if acct else None, kind="contacts", limit=args.limit)
        if as_json:
            print(json.dumps([r.__dict__ for r in runs], indent=2, default=str))
        for r in runs if not as_json else []:
            print(f"#{r.id:<4} {_when(r.started_at)}  {r.account_name:<14} {r.trigger:<18} {r.status:<15} "
                  f"+{r.added} ~{r.changed} -{r.deleted} held {r.held}"
                  + (f"  {r.error_code}" if r.error_code else "") + ("  (undone)" if r.undone_at else ""))
        return EXIT_OK

    if action == "backup":
        path = svc.backup_contacts("manual")
        print(f"Wrote {path.with_suffix('.vcf')} and {path}")
        return EXIT_OK

    if action == "unmerge":
        accounts: set[int] = set()
        if args.survivor:
            for raw in args.ids:
                done = svc.hub.rebuild_merged(int(raw), actor="cli")
                print(f"{raw}: " + ("rebuilt from its own card and the contacts still folded into it" if done
                                    else "left as it is (edited since the merge, or never merged)"))
            return EXIT_OK
        for raw in args.ids:
            res = svc.hub.unmerge(int(raw), actor="cli")
            if not res.restored:
                print(f"{raw}: not a merged contact - nothing to do")
                continue
            accounts |= res.accounts
            print(f"{raw}: given back as a person of their own"
                  + (f"; contact {res.survivor} rebuilt without them" if res.survivor_rebuilt
                     else f"; contact {res.survivor} left as it is (edited since the merge)"))
        print("The next sync writes the change to the accounts.")
        return EXIT_OK

    if action == "undo":
        if args.entry:
            res = svc.hub.undo_entry(int(args.target))
            if res.accounts:
                svc.engine.push(res.accounts, trigger="undo")
        else:
            res = svc.undo_run(int(args.target))
        print(f"Undone {res.undone}, skipped {res.skipped}; wrote to {len(res.accounts)} account(s).")
        return EXIT_OK

    if action == "list":
        from app.contacts.hub import ContactFilter  # noqa: PLC0415
        rows = svc.hub.list_rows(ContactFilter(args.view, text=args.text or ""))
        if as_json:
            print(json.dumps([{"id": r.id, "name": r.display_name, "company": r.company, "email": r.email,
                               "phone": r.phone, "accounts": r.accounts} for r in rows], indent=2))
        for r in rows if not as_json else []:
            print(f"{r.id:>6}  {r.display_name:<28} {r.company:<20} {r.email:<28} {r.phone}")
        return EXIT_OK

    if action == "show":
        card = svc.hub.get_card(int(args.id))
        if card is None:
            raise SystemExit("no such contact")
        print(json.dumps(card.to_json(), indent=2))
        print("kept in:", svc.hub.kept_in(int(args.id)))
        return EXIT_OK

    if action == "history":
        for h in svc.hub.history(int(args.id)):
            print(f"#{h.id:<6} {h.actor:<14} {h.action:<10} sync {h.sync_id or '-'}"
                  + ("  (undone)" if h.undone_at else ""))
        return EXIT_OK

    return EXIT_ERROR


def add_contacts_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser("contacts", parents=[common], help="the contact book: sync, log, undo, list")
    c = p.add_subparsers(dest="contacts_action", required=True)
    s = c.add_parser("sync", parents=[common], help="sync contacts with the connected accounts")
    s.add_argument("--account")
    s.add_argument("--dry-run", action="store_true", help="plan only; write nothing anywhere")
    s.add_argument("--approve-first", action="store_true", help="run an account's first sync for real")
    s.add_argument("--allow-brake", action="store_true", help="allow a sync the delete brake stopped")
    lg = c.add_parser("log", parents=[common], help="the sync log")
    lg.add_argument("--account")
    lg.add_argument("--limit", type=int, default=30)
    u = c.add_parser("undo", parents=[common], help="undo a whole sync (or one journal entry)")
    u.add_argument("target", help="sync log id (or a journal id with --entry)")
    u.add_argument("--entry", action="store_true")
    c.add_parser("backup", parents=[common], help="copy the contact book now (a vCard file and the contact tables)")
    um = c.add_parser("unmerge", parents=[common], help="give a wrongly merged contact back as its own person")
    um.add_argument("ids", nargs="+", help="the contact id(s) that were folded into another")
    um.add_argument("--survivor", action="store_true",
                    help="the ids are the contacts others were folded into: rebuild each without the people given back")
    ls = c.add_parser("list", parents=[common], help="list or search contacts")
    ls.add_argument("--text")
    ls.add_argument("--view", default="all",
                    choices=["all", "favourites", "conflict", "deleted_elsewhere", "bin", "single_account"])
    sh = c.add_parser("show", parents=[common], help="one contact")
    sh.add_argument("id")
    hi = c.add_parser("history", parents=[common], help="a contact's journal")
    hi.add_argument("id")
    p.set_defaults(func=cmd_contacts)
