r"""A contact written to Outlook or Google is filed as the owner chose - "First Last" by default.

Layer: L7

2026-10-25, the owner: "the file as in contact does not come up in outlook, by default it should
be first space last name, not sure others have this function but needs to be there and make it
configurable". Neither provider wrote the field, so Outlook's "File as" was empty for every
contact Leasha wrote.
"""

from __future__ import annotations

import pytest

from app.accounts.providers.google_people import _WRITE_FIELDS, GooglePeopleProvider
from app.accounts.providers.graph_contacts import GraphContactsProvider
from app.accounts.store import AccountSettings
from app.contacts.card import Card, Field
from app.contacts.file_as import DEFAULT, FORMATS, file_as
from tests.unit.test_sync_transport_and_batches import Fake

ANN = {"given": "Ann", "family": "Lee", "company": "Acme"}


@pytest.mark.parametrize("fmt, expected", [
    ("first_last", "Ann Lee"),
    ("last_first", "Lee, Ann"),
    ("company", "Acme"),
    ("last_first_company", "Lee, Ann (Acme)"),
    ("company_last_first", "Acme (Lee, Ann)"),
    ("nonsense", "Ann Lee"),                       # an unknown key is read as the default
])
def test_each_format_files_a_contact_as_outlook_would(fmt, expected):
    assert file_as(ANN, fmt) == expected


def test_the_default_is_first_space_last_and_every_format_is_offered():
    assert DEFAULT == "first_last"
    assert [key for key, _label in FORMATS] == [
        "first_last", "last_first", "company", "last_first_company", "company_last_first"]
    assert AccountSettings().file_as == "first_last"


def test_a_contact_without_the_names_is_filed_by_what_it_has():
    assert file_as({"given": "Ann"}, "last_first") == "Ann"
    assert file_as({"company": "Acme"}, "first_last") == "Acme"
    assert file_as({}, "first_last", ["ann@x.example"]) == "ann@x.example"
    assert file_as({}, "first_last") == ""
    assert file_as({"family": "Lee"}, "last_first_company") == "Lee"
    assert file_as({"given": "  Ann ", "family": " Lee"}, "first_last") == "Ann Lee"


def _card() -> Card:
    card = Card()
    card.scalars.update(ANN)
    card.fields.append(Field("email", "ann@x.example"))
    return card


def test_outlook_is_sent_file_as_in_the_chosen_format():
    assert GraphContactsProvider(Fake(), lambda: "t")._to_body(_card())["fileAs"] == "Ann Lee"
    assert GraphContactsProvider(Fake(), lambda: "t", file_as="last_first")._to_body(
        _card())["fileAs"] == "Lee, Ann"


def test_google_is_sent_file_ases_and_the_update_mask_names_it():
    person = GooglePeopleProvider(Fake(), lambda: "t", file_as="company")._to_person(_card())
    assert person["fileAses"] == [{"value": "Acme"}]
    assert "fileAses" in _WRITE_FIELDS.split(","), "Google ignores a field the update does not name"


def test_the_setting_round_trips_and_an_old_one_reads_as_the_default():
    saved = AccountSettings(file_as="company_last_first").to_json()
    assert AccountSettings.from_json(saved).file_as == "company_last_first"
    assert AccountSettings.from_json('{"conflict_policy": "newest"}').file_as == "first_last"


def test_the_account_service_hands_the_setting_to_both_providers(tmp_path):
    from app.accounts.secrets import SecretStore
    from app.accounts.service import AccountService
    from app.storage.sqlite_store import SqliteStore
    from tests.unit.test_account_service import MemoryKeyring

    with SqliteStore(tmp_path / "k.db") as store:
        service = AccountService(store, secrets=SecretStore(backend=MemoryKeyring()))
        settings = service.accounts.settings()
        settings.file_as = "last_first"
        service.accounts.save_settings(settings)
        for provider in ("microsoft", "google"):
            aid = service.accounts.add(provider, f"{provider}@x.example", provider.title())
            made = service._contact_provider(service.accounts.get(aid), Fake())
            assert made._file_as == "last_first", provider
