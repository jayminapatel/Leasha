"""7i: run enrichment on demand - never on a timer.

Layer: L7

Order 1k section 7i. Nothing in this package starts by itself: no timer, no
thread that wakes up. The UI calls `proposals_for` when the person presses
"Enrich this contact" or "Enrich selection", and shows what comes back; only
`apply_proposals` (in `base`) changes a contact, for the proposals the person
accepted.

**Switches.** Each source runs only if its `AccountSettings.enrich_*` switch is
on. The local ones are on by default (documents, order 1s 5e, excepted), the three internet ones are off, and
a source that is off is not called at all - for the internet ones that means no
request is made and no byte is sent.

**No repeat within the interval.** An internet lookup that was made is remembered in
`index_state` as `enrich:seen:<kind>:<key>` -> the time it was made, and the same
lookup is not made again for `AccountSettings.enrich_repeat_days` ("Ask the internet
again after", default `REPEAT_DAYS` = 90; 0 = never again): `<key>` is the address's
hash (Gravatar), the domain (logo) or the company name (Wikidata), never the address
itself. A lookup that found nothing counts; a lookup that failed to get an answer does
not. Order 1s 6a: what the lookup returned is kept too (`remember`), so a lookup not
made again still offers its answer. `force=True` ("Look again") ignores the memory and
replaces the kept answer. Local sources are cheap and change as mail arrives, so they
are not remembered.

**Network.** Every internet request goes through a `GuardedTransport` over an
`AllowedHosts` that lists only the enrichment hosts (`ENRICHMENT_HOSTS`) plus, for
the length of one logo call, the company's own domain. The default transport is
`UrllibTransport`; a test passes its own.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Optional

from app.accounts.http import AllowedHosts, UrllibTransport
from app.contacts.card import Card
from app.contacts.enrich import company, faces, gravatar, logo, remember, signatures, wikidata
from app.contacts.enrich import documents, invitations, linkedin, vcards       # order 1s section 5
from app.contacts.enrich.base import GuardedTransport, Proposal, emails_of, has_mail_before, window_start
from app.contacts.enrich.base import drop_declined   # order 1s D5, apart so the line above merges cleanly
from app.contacts.enrich.history import History, contact_history
from app.contacts.enrich.owner import learn_owner, owner_messages
from app.contacts.enrich.suggest import DEFAULT_MIN_COUNT, SuggestedContact, suggest_contacts
from app.core.errors import AppErrorException

__all__ = ["REPEAT_DAYS", "LOCAL_SOURCES", "INTERNET_SOURCES", "SWITCHES", "enrich_contact", "enrich_selection",
           "proposals_for", "history_for", "suggestions_for", "seen_key", "owner_addresses", "skipped_notes",
           "window_of", "window_notes", "MAIL_SOURCES", "repeat_days", "is_recent"]

#: The same lookup is not repeated within this many days (7i) - the default of the setting
#: `AccountSettings.enrich_repeat_days` (order 1s 6b), which is what a run uses.
REPEAT_DAYS = 90

LOCAL_SOURCES = ("signature", "company", "face", "vcard", "linkedin", "calendar", "document")
INTERNET_SOURCES = ("gravatar", "logo", "wikidata")

#: source -> the `AccountSettings` switch that turns it on.
SWITCHES = {
    "signature": "enrich_signatures", "company": "enrich_company", "face": "enrich_faces",
    "history": "enrich_history", "suggest": "enrich_suggest",
    "vcard": "enrich_vcards", "linkedin": "enrich_linkedin", "calendar": "enrich_calendar",
    "document": "enrich_documents",
    "gravatar": "enrich_gravatar", "logo": "enrich_logo", "wikidata": "enrich_wikidata",
}
_DEFAULT_ON = {"signature", "company", "face", "history", "suggest", "vcard", "linkedin", "calendar"}


def _on(settings: Any, source: str) -> bool:
    return bool(getattr(settings, SWITCHES[source], source in _DEFAULT_ON))


#: Order 1s D1: the sources that read mail, and so keep to the age window.
MAIL_SOURCES = ("signature",)


def window_of(settings: Any) -> Optional[int]:
    """The age window's start for these settings (Unix seconds), or None for any age. Default six months."""
    return window_start(int(getattr(settings, "enrich_max_months", 6) or 0))


def seen_key(kind: str, key: str) -> str:
    return f"enrich:seen:{kind}:{key}"


def repeat_days(settings: Any) -> int:
    """6b: "Ask the internet again after", in days; 0 means never again. A value that is not a whole number
    of days at least 0 falls back to the default rather than asking on every run."""
    try:
        days = int(getattr(settings, "enrich_repeat_days", REPEAT_DAYS))
    except (TypeError, ValueError):
        return REPEAT_DAYS
    return days if days >= 0 else REPEAT_DAYS


def is_recent(raw: Optional[str], now: float, days: int) -> bool:
    """Was a visit recorded at `raw` (Unix seconds as text) within `days`? With 0, any visit is recent."""
    try:
        return raw is not None and (days == 0 or (now - float(raw)) < days * 86400)
    except ValueError:
        return False


def _recent(store: Any, kind: str, key: str, now: float, days: int = REPEAT_DAYS) -> bool:
    return is_recent(store.get_state(seen_key(kind, key)), now, days)


def _remember(store: Any, kind: str, key: str, now: float) -> None:
    store.set_state(seen_key(kind, key), str(int(now)))


def owner_addresses(accounts_store: Any) -> list[str]:
    """The owner's own addresses (the accounts), for 'who else is on the thread'."""
    try:
        return [a.address for a in accounts_store.list() if getattr(a, "address", "")]
    except Exception:                                    # noqa: BLE001 - a convenience, never a failure
        return []


def _settings_of(accounts_store: Any) -> Any:
    return accounts_store.settings() if hasattr(accounts_store, "settings") else accounts_store


class _Net:
    """The guarded internet transport, built only when something needs it."""

    def __init__(self, transport: Any) -> None:
        self._given = transport
        self.allowed = AllowedHosts(("gravatar", "wikidata"))
        self._guard: Optional[GuardedTransport] = None

    @property
    def transport(self) -> GuardedTransport:
        if self._guard is None:
            inner = self._given if self._given is not None else UrllibTransport(self.allowed, account="enrichment")
            self._guard = GuardedTransport(inner, self.allowed)
        return self._guard


def enrich_contact(hub: Any, settings: Any, contact_id: int, *, transport: Any = None,
                   clock: Callable[[], float] = time.time, force: bool = False,
                   sources: Optional[Iterable[str]] = None, notes: Optional[list[str]] = None,
                   owner_emails: Iterable[str] = (), _net: Optional[_Net] = None, _wiki: Optional[dict] = None,
                   _skipped: Optional[dict] = None, _owner: Optional[dict] = None) -> list[Proposal]:
    """Proposals for one contact from every source that is switched on. Applies nothing.

    `owner_emails` are the owner's own addresses (`owner_addresses`): the signature reader never offers
    the owner's own details to anybody (order 1s 3h). `_owner` holds what was learned of the owner for a
    whole run, so it is read once, not once a contact.

    `_skipped` counts, per internet source, the contacts not asked again because the same lookup was made
    within the repeat interval (`repeat_days`) - so the run can say so rather than look as if the source found nothing."""
    row = hub.get_row(contact_id)
    card = hub.get_card(contact_id)
    if row is None or card is None or row.deleted_at is not None:
        return []
    wanted = set(sources) if sources is not None else set(LOCAL_SOURCES + INTERNET_SOURCES)
    store = hub.store
    now = clock()
    net = _net or _Net(transport)
    out: list[Proposal] = []

    def note(text: str) -> None:
        if notes is not None:
            notes.append(text)

    def skip(kind: str) -> None:
        if _skipped is not None:
            _skipped[kind] = _skipped.get(kind, 0) + 1

    if "signature" in wanted and _on(settings, "signature"):
        originals = bool(getattr(settings, "enrich_read_originals", False))
        memo = _owner if _owner is not None else {}
        if "owner" not in memo:
            memo["owner"] = learn_owner(store, owner_emails, region=hub.region, read_original=originals)
        out += signatures.signature_proposals(store, contact_id, card, region=hub.region, since=window_of(settings),
                                              owner=memo["owner"], read_original=originals,
                                              update_from=float(getattr(settings, "enrich_tick_from",
                                                                        signatures.UPDATE_FROM)))
    if "company" in wanted and _on(settings, "company"):
        out += company.company_proposals(contact_id, card)
    if "face" in wanted and _on(settings, "face"):
        out += faces.face_proposals(store, contact_id, card)
    if "vcard" in wanted and _on(settings, "vcard"):
        out += vcards.vcard_proposals(hub, contact_id, card, region=hub.region, since=window_of(settings))
    if "linkedin" in wanted and _on(settings, "linkedin") and getattr(settings, "enrich_linkedin_file", ""):
        out += linkedin.linkedin_proposals(contact_id, card, settings.enrich_linkedin_file)
    if "calendar" in wanted and _on(settings, "calendar"):
        out += invitations.calendar_proposals(hub, contact_id, card, since=window_of(settings))
    if "document" in wanted and _on(settings, "document"):
        out += documents.document_proposals(store, contact_id, card)

    days = repeat_days(settings)
    if "gravatar" in wanted and _on(settings, "gravatar") and emails_of(card):
        # One lookup - and one kept answer - per address hash (6a), so a card with a new second address
        # asks about that one only.
        found: list[Proposal] = []
        asked_all = True
        for e in emails_of(card)[:gravatar.MAX_ADDRESSES]:
            k = gravatar.email_hash(e)
            if not force and _recent(store, "gravatar", k, now, days):
                asked_all = False
                found += remember.offer_again(store, "gravatar", k, contact_id, card, hub.region) or []
                continue
            one = card.copy()
            one.fields = [f for f in one.fields if f.kind != "email" or f.value.strip().lower() == e]
            if any(p.is_photo for p in found):           # one photo is enough: do not ask for a second
                one.photo = next(p.new for p in found if p.is_photo)
            try:
                got = gravatar.gravatar_proposals(net.transport, contact_id, one)
            except AppErrorException:
                note("Gravatar could not be reached.")
                break
            _remember(store, "gravatar", k, now)
            remember.keep(store, "gravatar", k, got)
            found += got
        out += _one_each(found)
        if not asked_all:
            skip("gravatar")
    if "logo" in wanted and _on(settings, "logo"):
        domain = logo.company_domain(card)
        if domain and (force or not _recent(store, "logo", domain, now, days)):
            try:
                got = logo.logo_proposals(net.transport, net.allowed, contact_id, card)
                out += got
                _remember(store, "logo", domain, now)
                remember.keep(store, "logo", domain, got)
            except AppErrorException:
                note(f"{domain} could not be reached.")
        elif domain:
            out += remember.offer_again(store, "logo", domain, contact_id, card, hub.region) or []
            skip("logo")
    if "wikidata" in wanted and _on(settings, "wikidata"):
        term = wikidata.company_term(card)
        if term:
            key = company.company_key(term)
            cache = _wiki if _wiki is not None else {}
            if key in cache:                              # a colleague, looked up a moment ago in this run
                out += wikidata.proposals_from_facts(contact_id, card, cache[key])
            elif force or not _recent(store, "wikidata", key, now, days):
                try:
                    cache[key] = wikidata.lookup_company(net.transport, term)
                    out += wikidata.proposals_from_facts(contact_id, card, cache[key])
                    _remember(store, "wikidata", key, now)
                    remember.keep_facts(store, key, cache[key])
                except AppErrorException:
                    note("Wikidata could not be reached.")
            else:
                kept, facts = remember.facts_again(store, key)
                if kept:
                    cache[key] = facts
                    out += wikidata.proposals_from_facts(contact_id, card, facts)
                skip("wikidata")
    return out


def _one_each(found: list[Proposal]) -> list[Proposal]:
    """Gravatar's proposals over several addresses: one photo, and each link once."""
    out: list[Proposal] = []
    seen: set[str] = set()
    for p in found:
        k = "photo" if p.is_photo else f"{p.field_kind}:{p.new}"
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


#: How a skipped source is named to the person.
_SOURCE_NAMES = {"gravatar": "Gravatar", "logo": "Company logos", "wikidata": "Wikidata"}


def skipped_notes(skipped: dict, days: int = REPEAT_DAYS) -> list[str]:
    """One sentence per internet source that was not asked again, for the person to read.

    Without it a second run looks as though the internet sources found nothing, when they were never
    asked (found on the owner's index, 2026-10-12: 1,933 lookups remembered after the first run).
    `days` is the interval the run used (6b), so the sentence says the real one."""
    why = (f"looked up in the last {days} days" if days else
           "looked up before, and Ask the internet again after is set to Never")
    out = []
    for kind in INTERNET_SOURCES:
        n = skipped.get(kind, 0)
        if n:
            out.append(f"{_SOURCE_NAMES[kind]} was not asked again for {n:,} contact{'s' if n != 1 else ''}: "
                       f"{why}. Look again asks anyway.")
    return out


def enrich_selection(hub: Any, settings: Any, contact_ids: Iterable[int], *, transport: Any = None,
                     clock: Callable[[], float] = time.time, force: bool = False,
                     sources: Optional[Iterable[str]] = None, notes: Optional[list[str]] = None,
                     on_progress: Optional[Callable[[dict], Any]] = None,
                     cancelled: Optional[Callable[[], bool]] = None,
                     owner_emails: Iterable[str] = ()) -> list[Proposal]:
    """`enrich_contact` for each id, in order. Colleagues at one company share one Wikidata lookup.

    A look through the mail takes about a second a contact, so a thousand contacts is a quarter of an hour:
    `on_progress({"done", "total", "found"})` is called after each, and `cancelled()` is asked before each -
    when it says yes the proposals found so far are returned (a stop, not a failure)."""
    net = _Net(transport)
    wiki: dict = {}
    skipped: dict = {}
    owner: dict = {}
    out: list[Proposal] = []
    wanted = list(sources) if sources is not None else None
    ids = list(dict.fromkeys(int(c) for c in contact_ids))
    from app.extract.pst_attachment import kept_open  # noqa: PLC0415 - each archive opened once a run

    with kept_open():
        for done, cid in enumerate(ids):
            if cancelled is not None and cancelled():
                break
            out += enrich_contact(hub, settings, cid, transport=transport, clock=clock, force=force,
                                  sources=wanted, notes=notes, owner_emails=owner_emails, _net=net, _wiki=wiki,
                                  _skipped=skipped, _owner=owner)
            if on_progress is not None:
                on_progress({"done": done + 1, "total": len(ids), "found": len(out)})
    out = drop_declined(hub.store, out, region=hub.region)        # order 1s D5: never offered again
    if notes is not None:
        notes.extend(skipped_notes(skipped, repeat_days(settings)))
        notes.extend(window_notes(hub, settings, ids, out, wanted))
    return out


def window_notes(hub: Any, settings: Any, ids: list[int], found: list[Proposal],
                 wanted: Optional[list[str]]) -> list[str]:
    """2c: when mail gave nothing and the age window is why, say so - with how many contacts have older mail.

    One indexed look-up a contact (`has_mail_before`), never a second run: a person who sees nothing
    knows whether "Any age" would read more."""
    since = window_of(settings)
    reads_mail = [s for s in MAIL_SOURCES if (wanted is None or s in wanted) and _on(settings, s)]
    if since is None or not reads_mail or any(p.source in MAIL_SOURCES for p in found):
        return []
    older = 0
    for cid in ids:
        card = hub.get_card(cid)
        if card is not None and has_mail_before(hub.store, emails_of(card), since):
            older += 1
    when = time.strftime("%d %B %Y", time.gmtime(since)).lstrip("0")
    said = f"No suggestions from mail since {when}."
    if older:
        said += (f" {older:,} contact{'s have' if older != 1 else ' has'} older mail, which Any age "
                 "(Settings > Accounts > Enrichment) would read.")
    return [said]


def proposals_for(hub: Any, accounts_store: Any, contact_ids: Iterable[int], transport: Any = None,
                  **kw: Any) -> list[Proposal]:
    """What the UI calls: settings are read from `accounts_store` (or an `AccountSettings`)."""
    kw.setdefault("owner_emails", owner_addresses(accounts_store))
    return enrich_selection(hub, _settings_of(accounts_store), contact_ids, transport=transport, **kw)


def history_for(hub: Any, accounts_store: Any, contact_id: int) -> Optional[History]:
    """7d for the contact's page, or None when its switch is off."""
    settings = _settings_of(accounts_store)
    if not _on(settings, "history"):
        return None
    return contact_history(hub.store, contact_id, hub=hub, owner_emails=owner_addresses(accounts_store),
                           since=window_of(settings))


def suggestions_for(hub: Any, accounts_store: Any, *, min_count: int = DEFAULT_MIN_COUNT
                    ) -> list[SuggestedContact]:
    """7e's list, or [] when its switch is off."""
    settings = _settings_of(accounts_store)
    if not _on(settings, "suggest"):
        return []
    # The owner's own addresses are never a suggestion: the accounts' and every one the owner sent from
    # (order 1s, 2026-10-13: three old work addresses of the owner's topped the invitations' list).
    owner = sorted(owner_messages(hub.store, owner_addresses(accounts_store))[0])
    out = suggest_contacts(hub.store, min_count=min_count, owner_emails=owner)
    if _on(settings, "calendar"):                         # 5d: the people you meet, after the people who mail you
        mailed = {s.email for s in out}
        out += [s for s in invitations.calendar_suggestions(hub.store, owner_emails=owner, min_count=min_count,
                                                            since=window_of(settings)) if s.email not in mailed]
    return out
