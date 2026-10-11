"""7i: run enrichment on demand - never on a timer.

Layer: L7

Order 1k section 7i. Nothing in this package starts by itself: no timer, no
thread that wakes up. The UI calls `proposals_for` when the person presses
"Enrich this contact" or "Enrich selection", and shows what comes back; only
`apply_proposals` (in `base`) changes a contact, for the proposals the person
accepted.

**Switches.** Each source runs only if its `AccountSettings.enrich_*` switch is
on. The five local ones are on by default, the three internet ones are off, and
a source that is off is not called at all - for the internet ones that means no
request is made and no byte is sent.

**No repeat for 90 days.** An internet lookup that was made is remembered in
`index_state` as `enrich:seen:<kind>:<key>` -> the time it was made, and the same
lookup is not made again for 90 days: `<key>` is the address's hash (Gravatar),
the domain (logo) or the company name (Wikidata), never the address itself. A
lookup that found nothing counts; a lookup that failed to get an answer does not.
`force=True` ("Look again") ignores the memory. Local sources are cheap and
change as mail arrives, so they are not remembered.

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
from app.contacts.enrich import company, faces, gravatar, logo, signatures, wikidata
from app.contacts.enrich.base import GuardedTransport, Proposal, emails_of
from app.contacts.enrich.history import History, contact_history
from app.contacts.enrich.suggest import DEFAULT_MIN_COUNT, SuggestedContact, suggest_contacts
from app.core.errors import AppErrorException

__all__ = ["REPEAT_DAYS", "LOCAL_SOURCES", "INTERNET_SOURCES", "SWITCHES", "enrich_contact", "enrich_selection",
           "proposals_for", "history_for", "suggestions_for", "seen_key", "owner_addresses"]

#: The same lookup is not repeated within this many days (7i).
REPEAT_DAYS = 90

LOCAL_SOURCES = ("signature", "company", "face")
INTERNET_SOURCES = ("gravatar", "logo", "wikidata")

#: source -> the `AccountSettings` switch that turns it on.
SWITCHES = {
    "signature": "enrich_signatures", "company": "enrich_company", "face": "enrich_faces",
    "history": "enrich_history", "suggest": "enrich_suggest",
    "gravatar": "enrich_gravatar", "logo": "enrich_logo", "wikidata": "enrich_wikidata",
}
_DEFAULT_ON = {"signature", "company", "face", "history", "suggest"}


def _on(settings: Any, source: str) -> bool:
    return bool(getattr(settings, SWITCHES[source], source in _DEFAULT_ON))


def seen_key(kind: str, key: str) -> str:
    return f"enrich:seen:{kind}:{key}"


def _recent(store: Any, kind: str, key: str, now: float) -> bool:
    raw = store.get_state(seen_key(kind, key))
    try:
        return raw is not None and (now - float(raw)) < REPEAT_DAYS * 86400
    except ValueError:
        return False


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
                   _net: Optional[_Net] = None, _wiki: Optional[dict] = None) -> list[Proposal]:
    """Proposals for one contact from every source that is switched on. Applies nothing."""
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

    if "signature" in wanted and _on(settings, "signature"):
        out += signatures.signature_proposals(store, contact_id, card, region=hub.region)
    if "company" in wanted and _on(settings, "company"):
        out += company.company_proposals(contact_id, card)
    if "face" in wanted and _on(settings, "face"):
        out += faces.face_proposals(store, contact_id, card)

    if "gravatar" in wanted and _on(settings, "gravatar") and emails_of(card):
        keys = [gravatar.email_hash(e) for e in emails_of(card)[:gravatar.MAX_ADDRESSES]]
        if force or not all(_recent(store, "gravatar", k, now) for k in keys):
            try:
                out += gravatar.gravatar_proposals(net.transport, contact_id, card)
                for k in keys:
                    _remember(store, "gravatar", k, now)
            except AppErrorException:
                note("Gravatar could not be reached.")
    if "logo" in wanted and _on(settings, "logo"):
        domain = logo.company_domain(card)
        if domain and (force or not _recent(store, "logo", domain, now)):
            try:
                out += logo.logo_proposals(net.transport, net.allowed, contact_id, card)
                _remember(store, "logo", domain, now)
            except AppErrorException:
                note(f"{domain} could not be reached.")
    if "wikidata" in wanted and _on(settings, "wikidata"):
        term = wikidata.company_term(card)
        if term:
            key = company.company_key(term)
            if force or not _recent(store, "wikidata", key, now):
                try:
                    cache = _wiki if _wiki is not None else {}
                    if key not in cache:
                        cache[key] = wikidata.lookup_company(net.transport, term)
                    out += wikidata.proposals_from_facts(contact_id, card, cache[key])
                    _remember(store, "wikidata", key, now)
                except AppErrorException:
                    note("Wikidata could not be reached.")
    return out


def enrich_selection(hub: Any, settings: Any, contact_ids: Iterable[int], *, transport: Any = None,
                     clock: Callable[[], float] = time.time, force: bool = False,
                     sources: Optional[Iterable[str]] = None, notes: Optional[list[str]] = None) -> list[Proposal]:
    """`enrich_contact` for each id, in order. Colleagues at one company share one Wikidata lookup."""
    net = _Net(transport)
    wiki: dict = {}
    out: list[Proposal] = []
    wanted = list(sources) if sources is not None else None
    for cid in dict.fromkeys(int(c) for c in contact_ids):
        out += enrich_contact(hub, settings, cid, transport=transport, clock=clock, force=force, sources=wanted,
                              notes=notes, _net=net, _wiki=wiki)
    return out


def proposals_for(hub: Any, accounts_store: Any, contact_ids: Iterable[int], transport: Any = None,
                  **kw: Any) -> list[Proposal]:
    """What the UI calls: settings are read from `accounts_store` (or an `AccountSettings`)."""
    return enrich_selection(hub, _settings_of(accounts_store), contact_ids, transport=transport, **kw)


def history_for(hub: Any, accounts_store: Any, contact_id: int) -> Optional[History]:
    """7d for the contact's page, or None when its switch is off."""
    if not _on(_settings_of(accounts_store), "history"):
        return None
    return contact_history(hub.store, contact_id, hub=hub, owner_emails=owner_addresses(accounts_store))


def suggestions_for(hub: Any, accounts_store: Any, *, min_count: int = DEFAULT_MIN_COUNT
                    ) -> list[SuggestedContact]:
    """7e's list, or [] when its switch is off."""
    if not _on(_settings_of(accounts_store), "suggest"):
        return []
    return suggest_contacts(hub.store, min_count=min_count, owner_emails=owner_addresses(accounts_store))
