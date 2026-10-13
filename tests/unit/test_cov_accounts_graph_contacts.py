"""Microsoft contacts: batches that half succeed, throttling that never ends, folders that vanish, refusals.

Order 1u section 4d. `test_provider_graph_contacts.py` covers the single reads and writes and the delta walk;
`test_sync_transport_and_batches.py` an update batch and one throttled read. These pin the rest of what a
sync of many contacts leans on: `create_many`, `update_many` and `delete_many` answering each contact on its
own (a conflict, a refusal, a contact already gone, a whole change refused and sent again one property at a
time), a `$batch` that is refused or throttled past its rounds, a folder deleted while it is walked, a delta
page with no delta link, and the refusals of a single read or write that must never pass for success.
"""

from __future__ import annotations

import json

import pytest

from app.accounts.provider import HttpResponse, TokenExpired, VersionConflict
from app.accounts.providers.graph_contacts import API_BASE as M, GraphContactsProvider
from app.contacts.card import Card, Field
from app.core.errors import AppErrorException

from tests.unit.accounts_fakes import person
from tests.unit.test_sync_transport_and_batches import Fake

BATCH = f"{M}/$batch"


@pytest.fixture
def mf():
    f = Fake()
    f.add("GET", f"{M}/me/contacts", {"value": [{"id": "c0", "parentFolderId": "DEF"}]})
    f.add("GET", f"{M}/me/contactFolders", {"value": [{"id": "DEF", "displayName": "Contacts"},
                                                     {"id": "FAM", "displayName": "Family"}]})
    f.add("GET", f"{M}/me/contactFolders/DEF/childFolders", {"value": []})
    f.add("GET", f"{M}/me/contactFolders/FAM/childFolders", {"value": []})
    return f


def gp(f) -> GraphContactsProvider:
    return GraphContactsProvider(f, lambda: "t", account="Work")


def answers(*items) -> dict:
    return {"responses": [{"id": str(i), **a} for i, a in enumerate(items)]}


def sent_batches(f) -> list[list[dict]]:
    return [c["body"]["requests"] for c in f.calls if c["url"] == BATCH]


# ---------------------------------------------------------------- create_many

def test_creating_many_sends_one_batch_and_answers_each_contact_on_its_own(mf):
    mf.add("POST", BATCH, answers(
        {"status": 201, "body": {"id": "n1", "changeKey": "k1", "givenName": "Ann", "parentFolderId": "FAM"}},
        {"status": 400, "body": {"error": {"code": "ErrorInvalidProperty", "message": "bad phone"}}},
        {"status": 201, "body": {}}))                                  # "created" with no id is not created
    fam = person("Ann")
    fam.groups = ["Family", "Red"]
    out = gp(mf).create_many([fam, person("Bob"), person("Cy")])

    assert out[0].ok and out[0].remote_id == "n1" and out[0].version == "k1"
    assert out[0].card.groups[0] == "Family"
    assert not out[1].ok and out[1].error.code == "ERR_ACCOUNT_REJECTED"
    assert "ErrorInvalidProperty" in out[1].error.context["reason"] and out[1].error.details == "bad phone"
    assert not out[2].ok and out[2].error is not None
    [reqs] = sent_batches(mf)
    assert reqs[0]["url"] == "/me/contactFolders/FAM/contacts"       # the group that names a folder puts it there
    assert reqs[0]["body"]["categories"] == ["Red"]                   # and is not also a category
    assert reqs[1]["url"] == "/me/contacts"
    assert reqs[0]["headers"]["Content-Type"] == "application/json"


def test_nothing_to_write_sends_nothing(mf):
    p = gp(mf)
    assert p.create_many([]) == [] and p.update_many([]) == [] and p.delete_many([]) == []
    assert mf.calls == []


# ---------------------------------------------------------------- update_many

def test_updating_many_answers_gone_refused_changed_and_conflicting_contacts_each_on_their_own(mf):
    mf.add("POST", BATCH, answers(                                       # the change keys, read first
        {"status": 404, "body": {}},
        {"status": 500, "body": {"error": {"code": "ErrorInternalServerError"}}},
        {"status": 200, "body": {"changeKey": "MOVED", "parentFolderId": "DEF"}},
        {"status": 200, "body": {"changeKey": "k3", "parentFolderId": "DEF"}},
        {"status": 200, "body": {"changeKey": "k4", "parentFolderId": "DEF"}}))
    mf.add("POST", BATCH, answers(                                       # the PATCHes, for c3 and c4
        {"status": 412, "body": {}},
        {"status": 403, "body": {"error": {"code": "ErrorAccessDenied", "message": "no"}}}))
    out = gp(mf).update_many([(f"c{i}", f"k{i}", person(f"P{i}")) for i in range(5)])

    assert out[0].conflict                                   # gone at Microsoft
    assert out[1].error is not None and "read a contact before changing it" in out[1].error.context["reason"]
    assert out[2].conflict                                   # changed since it was read: never written blind
    assert out[3].conflict                                   # Outlook refused the PATCH as stale
    assert out[4].error is not None and "ErrorAccessDenied" in out[4].error.context["reason"]
    assert [len(b) for b in sent_batches(mf)] == [5, 2]


def test_a_whole_change_refused_in_a_batch_is_sent_again_one_property_at_a_time(mf):
    mf.add("POST", BATCH, answers({"status": 200, "body": {"changeKey": "k1", "parentFolderId": "DEF"}},
                                  {"status": 200, "body": {"changeKey": "k2", "parentFolderId": "DEF"}}))
    mf.add("POST", BATCH, answers({"status": 500, "body": {}}, {"status": 400, "body": {}}))
    # c1: every property alone goes through. c2: the first property alone is refused as stale.
    mf.add("PATCH", f"{M}/me/contacts/c1", {"changeKey": "k1b"})
    mf.add("PATCH", f"{M}/me/contacts/c2", {}, status=409)
    out = gp(mf).update_many([("c1", "k1", person("Ann")), ("c2", "k2", person("Bob"))])
    assert out[0].ok and out[0].version == "k1b"
    assert out[1].conflict
    one = [c for c in mf.calls if c["method"] == "PATCH" and c["url"].endswith("/c1")]
    assert len(one) > 3 and all(len(c["body"]) == 1 for c in one)


def test_a_piecewise_retry_where_nothing_goes_through_is_an_error_for_that_contact(mf):
    mf.add("POST", BATCH, answers({"status": 200, "body": {"changeKey": "k1", "parentFolderId": "DEF"}}))
    mf.add("POST", BATCH, answers({"status": 400, "body": {}}))
    mf.add("PATCH", f"{M}/me/contacts/c1", {}, status=400)
    out = gp(mf).update_many([("c1", "k1", person("Ann"))])
    assert not out[0].ok and out[0].error.code == "ERR_ACCOUNT_REJECTED"


def test_a_sign_in_failure_during_a_piecewise_retry_stops_the_batch_at_the_first_property(mf):
    """Found by this test (order 1u 4d): a 401 on each property alone was counted as that property refused, so
    an expired sign-in sent every property of every contact and reported each contact as refused by Outlook."""
    mf.add("POST", BATCH, answers({"status": 200, "body": {"changeKey": "k1", "parentFolderId": "DEF"}},
                                  {"status": 200, "body": {"changeKey": "k2", "parentFolderId": "DEF"}}))
    mf.add("POST", BATCH, answers({"status": 400, "body": {}}, {"status": 400, "body": {}}))
    mf.add("PATCH", f"{M}/me/contacts/c1", {}, status=401)
    with pytest.raises(AppErrorException) as caught:
        gp(mf).update_many([("c1", "k1", person("Ann")), ("c2", "k2", person("Bob"))])
    assert caught.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"
    assert len([c for c in mf.calls if c["method"] == "PATCH"]) == 1


def test_an_update_batch_clears_an_address_only_on_a_contact_that_has_one(mf):
    mf.add("POST", BATCH, answers(
        {"status": 200, "body": {"changeKey": "k1", "parentFolderId": "DEF", "homeAddress": {"city": "Leeds"}}},
        {"status": 200, "body": {"changeKey": "k2", "parentFolderId": "DEF"}}))
    mf.add("POST", BATCH, answers({"status": 200, "body": {"id": "c1", "changeKey": "k1b"}},
                                  {"status": 200, "body": {}}))
    out = gp(mf).update_many([("c1", "k1", person("Ann")), ("c2", "k2", person("Bob"))])
    patches = sent_batches(mf)[1]
    assert patches[0]["body"]["homeAddress"] is None                   # had one: cleared
    assert "homeAddress" not in patches[1]["body"]                     # had none: not sent (Graph answers 500)
    assert out[0].ok and out[0].card is not None
    assert out[1].ok and out[1].card is None                           # an answer with no contact in it


# ---------------------------------------------------------------- delete_many

def test_deleting_many_looks_first_and_answers_each_contact_on_its_own(mf):
    mf.add("POST", BATCH, answers(
        {"status": 404, "body": {}},                                   # already gone: fine
        {"status": 503, "body": {"error": {"code": "ServiceUnavailable"}}},
        {"status": 200, "body": {"changeKey": "MOVED"}},              # changed since: kept
        {"status": 200, "body": {"changeKey": "k3"}},
        {"status": 200, "body": {"changeKey": "k4"}},
        {"status": 200, "body": {"changeKey": "k5"}}))
    mf.add("POST", BATCH, answers({"status": 204}, {"status": 412}, {"status": 500, "body": {"error": {}}}))
    out = gp(mf).delete_many([(f"c{i}", f"k{i}") for i in range(6)])

    assert out[0].ok and out[0].remote_id == "c0"
    assert out[1].error is not None and "before deleting" in out[1].error.context["reason"]
    assert out[2].conflict
    assert out[3].ok and out[3].remote_id == "c3"
    assert out[4].conflict
    assert out[5].error is not None and not out[5].ok
    assert [r["method"] for r in sent_batches(mf)[1]] == ["DELETE"] * 3


# ---------------------------------------------------------------- $batch itself

def test_a_refused_batch_is_an_error_for_the_whole_call(mf):
    mf.add("POST", BATCH, {"error": {"message": "nope"}}, status=401)
    with pytest.raises(AppErrorException) as caught:
        gp(mf).get_many(["c1"])
    assert caught.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"


def test_throttling_that_never_lets_up_ends_as_the_providers_limit_with_its_wait(mf, monkeypatch):
    import time as _time
    slept = []
    monkeypatch.setattr(_time, "sleep", slept.append)
    mf.add("POST", BATCH, answers({"status": 429, "headers": {"Retry-After": "7"}}))
    with pytest.raises(AppErrorException) as caught:
        gp(mf).get_many(["c1"])
    assert caught.value.error.code == "ERR_PROVIDER_LIMIT"
    assert caught.value.error.context.get("retry_after_s") == 7.0
    assert slept == [7.0, 7.0, 7.0]                                    # three rounds, then it says so
    assert len(sent_batches(mf)) == 4


def test_a_throttle_with_an_unreadable_wait_waits_a_second_and_an_answer_with_a_strange_id_is_ignored(
        mf, monkeypatch):
    import time as _time
    slept = []
    monkeypatch.setattr(_time, "sleep", slept.append)
    mf.add("POST", BATCH, {"responses": [{"id": "0", "status": 429, "headers": {"retry-after": "soon"}},
                                         {"id": "x", "status": 200, "body": {"id": "zz"}},
                                         {"id": "9", "status": 200, "body": {"id": "zz"}}]})
    mf.add("POST", BATCH, answers({"status": 200, "body": {"id": "c1", "givenName": "Ann"}}))
    found = gp(mf).get_many(["c1"])
    assert slept == [1.0]
    assert found["c1"].card.get("given") == "Ann"


def test_a_contact_the_batch_did_not_answer_is_read_as_missing(mf):
    mf.add("POST", BATCH, {"responses": []})
    assert gp(mf).get_many(["c1"]) == {"c1": None}


# ---------------------------------------------------------------- folders and the delta walk

def test_a_folder_list_the_server_refuses_is_an_error_and_a_child_folder_gone_mid_walk_is_skipped():
    f = Fake()
    f.add("GET", f"{M}/me/contactFolders", {}, status=500)
    with pytest.raises(AppErrorException):
        gp(f)._load_folders()
    f = Fake()
    f.add("GET", f"{M}/me/contactFolders", {"value": [{"id": "A", "displayName": "A"}, {"id": "B"}]})
    f.add("GET", f"{M}/me/contactFolders/A/childFolders", {}, status=404)
    f.add("GET", f"{M}/me/contactFolders/B/childFolders", {"value": [{"id": "A"}]})   # seen already: once
    assert gp(f)._load_folders() == {"A": "A", "B": "B"}               # a folder with no name is its id


def test_the_default_folder_probe_refused_is_an_error():
    f = Fake()
    f.add("GET", f"{M}/me/contacts", {}, status=403)
    with pytest.raises(AppErrorException):
        gp(f)._ensure_default_id()


def test_a_folder_deleted_during_a_pull_is_dropped_from_the_token(mf):
    mf.add("GET", f"{M}/me/contacts/delta", {"value": [], "@odata.deltaLink": f"{M}/d0"})
    mf.add("GET", f"{M}/me/contactFolders/FAM/contacts/delta", {}, status=404)
    pages = list(gp(mf).pull(json.dumps({"v": 1, "d": {"FAM": f"{M}/me/contactFolders/FAM/contacts/delta"}})))
    assert pages[-1].done
    assert json.loads(pages[-1].next_token) == {"v": 1, "d": {"": f"{M}/d0"}}


def test_a_delta_page_with_no_delta_link_is_an_error_not_a_finished_pull(mf):
    mf.add("GET", f"{M}/me/contacts/delta", {"value": []})
    with pytest.raises(AppErrorException):
        list(gp(mf).pull(None))


def test_a_delta_read_the_server_refuses_is_an_error(mf):
    mf.add("GET", f"{M}/me/contacts/delta", {}, status=500)
    with pytest.raises(AppErrorException) as caught:
        list(gp(mf).pull(None))
    assert caught.value.error.context["reason"] == "read changes: HTTP 500"
    with pytest.raises(TokenExpired):
        list(gp(mf).pull("not json"))


def test_a_resume_with_every_folder_already_done_finishes_without_reading(mf):
    token = json.dumps({"v": 1, "d": {"": f"{M}/d0", "FAM": f"{M}/d1", "OLD": f"{M}/d2"},
                        "done": ["", "FAM"]})
    pages = list(gp(mf).pull(token))
    assert len(pages) == 1 and pages[0].done and pages[0].changes == []
    assert json.loads(pages[0].next_token) == {"v": 1, "d": {"": f"{M}/d0", "FAM": f"{M}/d1"}}   # OLD is gone
    assert not [c for c in mf.calls if "/delta" in c["url"]]


def test_the_check_skips_a_folder_gone_meanwhile_and_a_refusal_is_an_error(mf):
    mf.routes[("GET", f"{M}/me/contacts")] = [HttpResponse(200, {}, json.dumps(
        {"value": [{"id": "c0", "changeKey": "y", "parentFolderId": "DEF"}, {"changeKey": "no id"}]}).encode())]
    mf.add("GET", f"{M}/me/contactFolders/FAM/contacts", {}, status=404)
    assert gp(mf).list_ids() == {"c0": ("y", "")}
    mf.routes[("GET", f"{M}/me/contactFolders/FAM/contacts")] = [HttpResponse(500, {}, b"")]
    with pytest.raises(AppErrorException):
        gp(mf).list_ids()


# ---------------------------------------------------------------- single reads and writes that are refused

def test_a_single_read_the_server_refuses_is_an_error_not_a_missing_contact(mf):
    mf.add("GET", f"{M}/me/contacts/c1", {}, status=500)
    with pytest.raises(AppErrorException):
        gp(mf).get("c1")


def test_reading_before_a_write_refused_is_an_error_and_writes_nothing(mf):
    mf.add("GET", f"{M}/me/contacts/c1", {}, status=500)
    with pytest.raises(AppErrorException):
        gp(mf).update("c1", "k1", person("Ann"))
    assert not [c for c in mf.calls if c["method"] == "PATCH"]


def test_an_update_answered_with_a_status_outside_the_contract_is_an_error(mf):
    mf.add("GET", f"{M}/me/contacts/c1", {"changeKey": "k1", "parentFolderId": "DEF"})
    mf.add("PATCH", f"{M}/me/contacts/c1", {}, status=403)
    with pytest.raises(AppErrorException):
        gp(mf).update("c1", "k1", person("Ann"))


def test_a_piecewise_update_refused_as_stale_is_a_conflict_and_a_partial_one_is_written(mf, caplog):
    mf.add("GET", f"{M}/me/contacts/c1", {"changeKey": "k1", "parentFolderId": "DEF"})
    p = gp(mf)
    mf.add("PATCH", f"{M}/me/contacts/c1", {}, status=412)
    with pytest.raises(VersionConflict):
        p._update_piecewise("c1", "k1", {"givenName": "A", "surname": "B"}, HttpResponse(500, {}, b""))
    mf.routes[("PATCH", f"{M}/me/contacts/c1")] = [
        HttpResponse(200, {}, b'{"changeKey": "k1b"}'), HttpResponse(400, {}, b"")]
    assert p._update_piecewise("c1", "k1", {"givenName": "A", "surname": "B"}, HttpResponse(500, {}, b"")) == "k1b"


def test_a_delete_refused_as_stale_is_a_conflict_and_any_other_refusal_an_error(mf):
    mf.add("GET", f"{M}/me/contacts/c1", {"changeKey": "k1"})
    mf.add("DELETE", f"{M}/me/contacts/c1", {}, status=409)
    with pytest.raises(VersionConflict):
        gp(mf).delete("c1", "k1")
    mf.routes[("DELETE", f"{M}/me/contacts/c1")] = [HttpResponse(500, {}, b"")]
    with pytest.raises(AppErrorException):
        gp(mf).delete("c1", "k1")


# ---------------------------------------------------------------- what is sent

def test_the_body_fits_what_outlook_has_room_for():
    c = Card(scalars={"given": "Ann"})
    c.fields += [Field("phone", "+1", "home"), Field("phone", "+2", "home"), Field("phone", "+3", "home"),
                 Field("phone", "+4", "work"),
                 Field("address", "1 Work St", "work"), Field("address", "2 Work St", "business"),
                 Field("url", "https://a.example"),
                 Field("date", "someday", "birthday"),
                 Field("related", "Bo", "spouse"), Field("related", "Cy", "friend"),
                 Field("im", "ann@chat")]
    body = GraphContactsProvider(Fake(), lambda: "t")._to_body(c)
    assert body["homePhones"] == ["+1", "+2"] and body["businessPhones"] == ["+4", "+3"]   # the third home moves
    assert body["businessAddress"] == {"street": "1 Work St"}           # the first work address only
    assert body["businessHomePage"] == "https://a.example"
    assert body["birthday"] is None                                     # an unreadable date is not sent
    assert body["spouseName"] == "Bo" and body["imAddresses"] == ["ann@chat"]
