"""Google contacts: batches that half succeed, batches refused and sent one at a time, deletes, refusals.

Order 1u section 4d. `test_provider_google_people.py` covers the single reads and writes and the sync token;
`test_sync_transport_and_batches.py` an update batch, a refused one and a delete batch. These pin the rest of
what a sync of many contacts leans on: `create_many` answering each contact on its own, a refused batch sent
again contact by contact where a sign-in failure still stops everything, a delete batch refused and done one
at a time, the label list read over several pages, and the refusals of a single read or write that must
never pass for success (a contact that could not be read is not a contact that is gone).
"""

from __future__ import annotations

import pytest

from app.accounts.provider import TokenExpired, VersionConflict
from app.accounts.providers.google_people import API_BASE as G, GooglePeopleProvider
from app.contacts.card import Card, Field
from app.core.errors import AppErrorException

from tests.unit.accounts_fakes import person
from tests.unit.test_sync_transport_and_batches import Fake, _person


@pytest.fixture
def gf():
    f = Fake()
    f.add("GET", f"{G}/contactGroups", {"contactGroups": []})
    return f


def gp(f) -> GooglePeopleProvider:
    return GooglePeopleProvider(f, lambda: "t", account="Home")


def calls_to(f, suffix: str) -> list[dict]:
    return [c for c in f.calls if c["url"].split("?")[0].endswith(suffix)]


# ---------------------------------------------------------------- create_many

def test_creating_many_answers_each_contact_on_its_own(gf):
    gf.add("POST", f"{G}/people:batchCreateContacts", {"createdPeople": [
        {"person": _person("people/n1", "e1", "Ann")},
        {"status": {"code": 3, "message": "invalid phone"}, "person": {}}]})     # and no answer for the third
    out = gp(gf).create_many([person("Ann"), person("Bob"), person("Cy")])

    assert out[0].ok and out[0].remote_id == "people/n1" and out[0].version == "e1"
    assert out[0].card.get("given") == "Ann"
    assert not out[1].ok and "invalid phone" in out[1].error.context["reason"]
    assert not out[2].ok and "no answer" in out[2].error.context["reason"]
    sent = calls_to(gf, "batchCreateContacts")[0]["body"]
    assert len(sent["contacts"]) == 3 and "readMask" in sent


def test_nothing_to_write_sends_nothing(gf):
    p = gp(gf)
    assert p.create_many([]) == [] and p.update_many([]) == [] and p.delete_many([]) == []
    assert gf.calls == []


def test_a_create_batch_refused_for_its_content_is_sent_one_contact_at_a_time(gf):
    gf.add("POST", f"{G}/people:batchCreateContacts", {"error": {"message": "bad"}}, status=400)
    gf.add("POST", f"{G}/people:createContact", {"error": {"message": "bad phone"}}, status=400)
    gf.add("POST", f"{G}/people:createContact", {"resourceName": "people/n2", "etag": "e2"})
    out = gp(gf).create_many([person("Ann"), person("Bob")])
    assert not out[0].ok and out[0].error.code == "ERR_ACCOUNT_REJECTED"     # one bad contact costs only itself
    assert out[1].ok and out[1].remote_id == "people/n2" and out[1].version == "e2"


def test_a_sign_in_failure_while_sending_one_at_a_time_stops_the_batch(gf):
    gf.add("POST", f"{G}/people:batchCreateContacts", {}, status=400)
    gf.add("POST", f"{G}/people:createContact", {}, status=401)
    with pytest.raises(AppErrorException) as caught:
        gp(gf).create_many([person("Ann"), person("Bob")])
    assert caught.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"
    assert len(calls_to(gf, "people:createContact")) == 1


def test_a_stale_contact_sent_on_its_own_after_a_refused_batch_is_a_conflict(gf):
    gf.add("POST", f"{G}/people:batchUpdateContacts", {}, status=400)
    gf.add("PATCH", f"{G}/people/c1:updateContact", {"error": {"status": "FAILED_PRECONDITION"}}, status=400)
    out = gp(gf).update_many([("people/c1", "e1", person("Ann"))])
    assert out[0].conflict and not out[0].ok


def test_an_update_batch_answer_that_is_an_error_or_empty_is_an_error_for_that_contact(gf):
    gf.add("POST", f"{G}/people:batchUpdateContacts", {"updateResult": {
        "people/c1": {"status": {"code": 3, "message": "invalid"}},
        "people/c2": {}}})
    out = gp(gf).update_many([("people/c1", "e1", person("Ann")), ("people/c2", "e2", person("Bob"))])
    assert not out[0].ok and not out[0].conflict and "invalid" in out[0].error.context["reason"]
    assert not out[1].ok and "no answer" in out[1].error.context["reason"]


# ---------------------------------------------------------------- reads and deletes of many

def test_reading_many_refused_is_an_error_and_an_answer_naming_nobody_is_skipped(gf):
    gf.add("GET", f"{G}/people:batchGet", {"responses": [
        {"status": {}, "person": {}}, {"requestedResourceName": "people/c1", "person": _person("people/c1", "e", "A")}]})
    assert set(gp(gf).get_many(["people/c1"])) == {"people/c1"}
    gf.routes[("GET", f"{G}/people:batchGet")] = []
    gf.add("GET", f"{G}/people:batchGet", {}, status=500)
    with pytest.raises(AppErrorException):
        gp(gf).get_many(["people/c1"])


def test_a_contact_the_read_did_not_answer_for_is_never_deleted(gf):
    gf.add("GET", f"{G}/people:batchGet", {"responses": []})
    out = gp(gf).delete_many([("people/c1", "e1")])
    assert not out[0].ok and out[0].error is not None
    assert not calls_to(gf, "batchDeleteContacts")


def test_a_delete_batch_refused_is_done_one_at_a_time_and_a_refusal_costs_only_that_contact(gf):
    gf.add("GET", f"{G}/people:batchGet", {"responses": [
        {"requestedResourceName": "people/c1", "person": _person("people/c1", "e1", "Ann")},
        {"requestedResourceName": "people/c2", "person": _person("people/c2", "e2", "Bob")}]})
    gf.add("POST", f"{G}/people:batchDeleteContacts", {}, status=400)
    gf.add("DELETE", f"{G}/people/c1:deleteContact", {}, status=500)
    gf.add("DELETE", f"{G}/people/c2:deleteContact", {})
    out = gp(gf).delete_many([("people/c1", "e1"), ("people/c2", "e2")])
    assert not out[0].ok and out[0].error.code == "ERR_ACCOUNT_REJECTED"
    assert out[1].ok and out[1].remote_id == "people/c2"


def test_a_sign_in_failure_while_deleting_one_at_a_time_stops_the_batch(gf):
    gf.add("GET", f"{G}/people:batchGet", {"responses": [
        {"requestedResourceName": "people/c1", "person": _person("people/c1", "e1", "Ann")}]})
    gf.add("POST", f"{G}/people:batchDeleteContacts", {}, status=400)
    gf.add("DELETE", f"{G}/people/c1:deleteContact", {}, status=401)
    with pytest.raises(AppErrorException) as caught:
        gp(gf).delete_many([("people/c1", "e1")])
    assert caught.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"


def test_a_delete_batch_refused_for_the_account_raises_at_once(gf):
    gf.add("GET", f"{G}/people:batchGet", {"responses": [
        {"requestedResourceName": "people/c1", "person": _person("people/c1", "e1", "Ann")}]})
    gf.add("POST", f"{G}/people:batchDeleteContacts", {}, status=429)
    with pytest.raises(AppErrorException) as caught:
        gp(gf).delete_many([("people/c1", "e1")])
    assert caught.value.error.code == "ERR_PROVIDER_LIMIT"
    assert not calls_to(gf, ":deleteContact")


# ---------------------------------------------------------------- single writes refused

def test_a_delete_whose_look_first_is_refused_deletes_nothing(gf):
    gf.add("GET", f"{G}/people/c1", {}, status=500)
    with pytest.raises(AppErrorException):
        gp(gf).delete("people/c1", "e1")
    assert not calls_to(gf, ":deleteContact")


def test_a_delete_of_a_contact_already_gone_is_quiet_and_a_refused_delete_is_an_error(gf):
    gf.add("GET", f"{G}/people/c1", {}, status=404)
    gp(gf).delete("people/c1", "e1")
    assert not calls_to(gf, ":deleteContact")
    gf.add("DELETE", f"{G}/people/c2:deleteContact", {}, status=403)
    with pytest.raises(AppErrorException):
        gp(gf).delete("people/c2", "")


def test_a_contact_changed_since_it_was_read_is_never_deleted(gf):
    gf.add("GET", f"{G}/people/c1", {"etag": "NEWER"})
    with pytest.raises(VersionConflict):
        gp(gf).delete("people/c1", "e1")
    assert not calls_to(gf, ":deleteContact")


def test_updating_a_contact_gone_at_google_is_a_conflict_and_a_refused_create_is_an_error(gf):
    gf.add("PATCH", f"{G}/people/c1:updateContact", {}, status=404)
    with pytest.raises(VersionConflict):
        gp(gf).update("people/c1", "e1", person("Ann"))
    gf.add("POST", f"{G}/people:createContact", {}, status=500)
    with pytest.raises(AppErrorException):
        gp(gf).create(person("Ann"))


def test_a_list_refused_without_a_sync_token_is_an_error_not_an_expired_token(gf):
    gf.add("GET", f"{G}/people/me/connections", {"error": {"message": "sync token"}}, status=400)
    with pytest.raises(AppErrorException) as caught:
        list(gp(gf).pull(None))
    assert not isinstance(caught.value, TokenExpired)
    with pytest.raises(TokenExpired):
        list(gp(gf).pull('{"v":1,"s":"old"}'))


# ---------------------------------------------------------------- labels

def test_labels_are_read_over_every_page_and_a_refusal_is_an_error():
    f = Fake()
    f.add("GET", f"{G}/contactGroups", {"contactGroups": [{"resourceName": "contactGroups/a", "name": "A"}],
                                       "nextPageToken": "p2"})
    f.add("GET", f"{G}/contactGroups", {"contactGroups": [{"resourceName": "contactGroups/b", "formattedName": "B"},
                                                          {"resourceName": "contactGroups/myContacts"}]})
    p = gp(f)
    p._load_groups()
    assert p._by_resource == {"contactGroups/a": "A", "contactGroups/b": "B"}
    assert "pageToken=p2" in f.calls[1]["url"]
    f2 = Fake()
    f2.add("GET", f"{G}/contactGroups", {}, status=500)
    with pytest.raises(AppErrorException):
        gp(f2)._load_groups()


def test_a_new_label_that_cannot_be_made_stops_the_write(gf):
    gf.add("POST", f"{G}/contactGroups", {}, status=403)
    card = person("Ann")
    card.groups = ["Brand new"]
    with pytest.raises(AppErrorException):
        gp(gf).create(card)
    assert not calls_to(gf, "people:createContact")


# ---------------------------------------------------------------- the check

def test_the_check_reads_every_page_skips_deleted_people_and_a_refusal_is_an_error(gf):
    deleted = _person("people/gone", "e9", "Gone")
    deleted["metadata"]["deleted"] = True
    gf.add("GET", f"{G}/people/me/connections", {"connections": [_person("people/c1", "e1", "Ann")],
                                                 "nextPageToken": "n2"})
    gf.add("GET", f"{G}/people/me/connections", {"connections": [deleted, {"etag": "no name"},
                                                                 {"resourceName": "people/c2", "etag": "e2"}]})
    assert gp(gf).list_ids() == {"people/c1": ("e1", "Ann"), "people/c2": ("e2", "")}
    gf.routes[("GET", f"{G}/people/me/connections")] = []
    gf.add("GET", f"{G}/people/me/connections", {}, status=500)
    with pytest.raises(AppErrorException):
        gp(gf).list_ids()


# ---------------------------------------------------------------- dates and what is sent

def test_dates_google_can_hold_are_sent_and_one_it_cannot_is_left_out(gf):
    c = Card(scalars={"given": "Ann"})
    c.fields += [Field("date", "--04-05", "birthday"), Field("date", "2001-02-03", "birthday"),
                 Field("date", "2020-06-07", "anniversary"), Field("date", "spring", "other"),
                 Field("im", "ann@chat", "xmpp"), Field("im", "ann2"), Field("custom", "blue", "")]
    body = gp(gf)._to_person(c)
    assert body["birthdays"] == [{"date": {"month": 4, "day": 5}}]                       # the first birthday
    assert body["events"] == [{"date": {"year": 2001, "month": 2, "day": 3}, "type": "birthday"},
                              {"date": {"year": 2020, "month": 6, "day": 7}, "type": "anniversary"}]
    assert body["imClients"] == [{"username": "ann@chat", "protocol": "xmpp"}, {"username": "ann2"}]
    assert body["userDefined"] == [{"key": "Custom", "value": "blue"}]


def test_a_date_with_no_month_or_day_reads_as_nothing(gf):
    card = gp(gf)._to_card({"resourceName": "people/c1", "birthdays": [{"date": {"year": 1990}}],
                            "events": [{"date": {"month": 3, "day": 4}, "type": ""}]})
    assert [(f.value, f.label) for f in card.fields if f.kind == "date"] == [("--03-04", "event")]
