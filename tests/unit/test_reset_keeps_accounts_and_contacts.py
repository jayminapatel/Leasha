"""Order 1k: resetting the index clears fetched mail and its position, and keeps the person's own data.

Layer: L7

Contacts and accounts are not derived from the files, so `clear_index` leaves them; the mail position of
each account is cleared with the mail it pointed at.
"""

from __future__ import annotations

from app.accounts.store import AccountStore
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore
from tests.unit.accounts_fakes import person


def test_a_reset_keeps_contacts_and_accounts_but_forgets_the_mail_position(tmp_path):
    with SqliteStore(tmp_path / "k.db") as store:
        accounts = AccountStore(store)
        hub = ContactHub(store)
        aid = accounts.add("google", "me@x.com", "Home", mail_on=True)
        accounts.save_token(aid, "mail", "history-123", extra={"first_fetch": {"years": 2}})
        accounts.save_token(aid, "contacts", "sync-456")
        cid = hub.create(person("Ann", "Lee", email="ann@x.com"), kept_in=[aid])
        store.clear_index()
        assert accounts.get(aid) is not None
        assert hub.count() == 1 and hub.kept_in(cid) == [aid]
        assert accounts.state(aid, "mail").token is None and accounts.state(aid, "mail").extra == {}
        assert accounts.state(aid, "contacts").token == "sync-456"
