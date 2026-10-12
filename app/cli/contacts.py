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
from pathlib import Path
from typing import Any

from app.cli._common import EXIT_ERROR, EXIT_OK
from app.cli.accounts import open_service

__all__ = ["add_contacts_parser", "cmd_contacts"]


def _when(ts: Any) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def cmd_contacts(args: argparse.Namespace) -> int:
    if args.contacts_action == "enrich":
        # Before `open_service`, which migrates the index: measuring never writes (order 1s, section 1).
        return _measure(args)
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

    if action == "merge-safe":
        from app.contacts import book_actions as actions  # noqa: PLC0415 - plain functions; no window is made

        scan = actions.scan_book(svc.hub, use_cache=False)
        steps = actions.plan_safe_merges(scan.groups, scan.cards, svc.hub.region)
        folded = sum(len(s.others) for s in steps)
        print(f"{len(steps)} groups of contacts are certainly one person (same email or mobile number, names "
              f"that agree): {folded} duplicate(s) would be folded in.")
        for step in steps[:6]:
            print(f"  {' + '.join(step.names[:4])}  [{', '.join(step.reasons)}]")
        if len(steps) > 6:
            print(f"  ... and {len(steps) - 6} more")
        if not steps or not args.yes:
            print("Nothing was changed." + ("" if not steps else " Add --yes to merge them as one step "
                  "(undo with: contacts unmerge --all --yes)."))
            return EXIT_OK
        print(f"Copy written: {svc.backup_contacts('before-merge-safe')}")
        summary = actions.merge_safe(svc.hub, steps)
        print(f"Merged {summary.groups} groups ({summary.contacts} contacts) in one step; "
              f"{svc.hub.count()} contacts in the book.")
        return EXIT_OK

    if action == "backup":
        path = svc.backup_contacts("manual")
        print(f"Wrote {path.with_suffix('.vcf')} and {path}")
        return EXIT_OK

    if action == "unmerge":
        accounts: set[int] = set()
        if args.all:
            batches = svc.hub.merge_batches()
            if not args.yes:
                print(f"This undoes all {len(batches)} merges still standing: every merged contact becomes its own "
                      "contact again, linked to the one account it came from. Nothing is deleted at any "
                      "account. A copy of the book is written first. Add --yes to go ahead.")
                return EXIT_ERROR
            print(f"Copy written: {svc.backup_contacts('before-undo-merges')}")
            res = svc.hub.undo_all_merges(actor="cli")
            print(f"Undid {len(batches)} merges ({res.undone} changes, {res.skipped} skipped). "
                  f"{svc.hub.count()} contacts in the book.")
            return EXIT_OK
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


def _measure(args: argparse.Namespace) -> int:
    """`contacts enrich --measure`: the enrichment funnel, read-only (app/contacts/enrich/measure.py)."""
    from app.accounts.store import AccountStore  # noqa: PLC0415 - read through the read-only store below
    from app.cli._common import _load  # noqa: PLC0415
    from app.contacts.enrich.measure import ReadOnlyStore, measure, write_sample  # noqa: PLC0415
    from app.contacts.hub import ContactHub  # noqa: PLC0415

    if not args.measure:
        raise SystemExit("contacts enrich needs --measure (the only mode on the command line)")
    if args.sample and not args.out:
        raise SystemExit("--sample needs --out FILE.csv")
    settings = _load(args)
    store = ReadOnlyStore(settings.fts_db)
    try:
        acct = AccountStore(store).settings()
        hub = ContactHub(store, region=getattr(acct, "phone_region", "") or "GB")
        months = 0 if args.any_age else int(args.months if args.months is not None
                                              else getattr(acct, "enrich_max_months", 6))
        from app.contacts.enrich.run import owner_addresses  # noqa: PLC0415
        # 3h: the owner's own details are never counted as anybody's proposal.
        fun = measure(store, hub, acct, months=months, keep_samples=bool(args.sample),
                      owner_emails=owner_addresses(AccountStore(store)),
                      read_originals=True if getattr(args, "read_originals", False) else None)
    finally:
        store.close()
    if args.json:
        print(json.dumps(fun.as_dict(), indent=2))
    else:
        since = datetime.fromtimestamp(fun.since).strftime("%d %B %Y") if fun.since else "any age"
        print(f"Mail since: {since}   (read-only; nothing was written and nothing was asked of the internet)")
        for label, n in (("Live contacts", fun.live), ("... with an email address", fun.with_email),
                         ("... with at least one message from them", fun.with_mail),
                         ("... where a signature block was found", fun.block_found),
                         ("... where something was read from the block", fun.read),
                         ("... with at least one proposal", fun.proposed),
                         ("... read, but all of it is already on the card", fun.already_on_card),
                         ("No mail from them, a proposal from a quoted signature", fun.quoted_only)):
            print(f"  {label:<52} {n:>7,}")
        print("Per source (contacts):")
        for name, n in sorted(fun.sources.items()):
            print(f"  {name:<52} {n:>7,}")
        print("Signature proposals per field: "
              + (", ".join(f"{k} {v:,}" for k, v in fun.fields.most_common()) or "none"))
        print(f"Time: {fun.seconds:,.1f} s, {fun.per_contact():.3f} s a contact")
        if fun.read_originals:
            print(f"Originals re-read (read-only): {fun.rereads:,}, median {fun.reread_ms_median:,.1f} ms each")
    if args.sample:
        n = write_sample(fun, int(args.sample), args.out)
        print(f"Wrote {n} proposals to {args.out}")
    return EXIT_OK


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
    ms = c.add_parser("merge-safe", parents=[common],
                      help="merge the contacts that are certainly one person (shows them first; --yes to do it)")
    ms.add_argument("--yes", action="store_true", help="really do it")
    c.add_parser("backup", parents=[common], help="copy the contact book now (a vCard file and the contact tables)")
    um = c.add_parser("unmerge", parents=[common], help="give a wrongly merged contact back as its own person")
    um.add_argument("ids", nargs="*", help="the contact id(s) that were folded into another")
    um.add_argument("--all", action="store_true", help="undo every merge still standing (needs --yes)")
    um.add_argument("--yes", action="store_true", help="really do it")
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
    en = c.add_parser("enrich", parents=[common],
                      help="measure enrichment over the whole book, read-only (order 1s): --measure")
    en.add_argument("--measure", action="store_true", help="count what every source would find; write nothing")
    window = en.add_mutually_exclusive_group()
    window.add_argument("--months", type=int, help="only mail from the last N months (default: the setting)")
    window.add_argument("--any-age", action="store_true", help="mail of any age")
    en.add_argument("--read-originals", action="store_true",
                    help="measure as if 'Read the original messages' were on: re-read them, read-only, and "
                         "say how many and how long one took")
    en.add_argument("--sample", type=int, help="also write N random signature proposals for checking")
    en.add_argument("--out", type=Path, help="the CSV file for --sample")
    p.set_defaults(func=cmd_contacts)
