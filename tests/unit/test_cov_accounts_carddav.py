"""CardDAV batches, group cards that move, deletes and the server's refusals.

Order 1u §4d. `test_provider_carddav.py` and `test_provider_carddav_server.py` cover discovery, paging, delta
and single writes. These pin what a sync of many contacts leans on: `create_many` / `update_many` /
`delete_many` (a conflict or a refusal costs one contact, a sign-in failure stops the batch), each group card
written once a batch, a group card that moved under us retried once, `get_many` and `list_ids` leaving group
cards out, and the refusals that must not pass for success (too many redirects, a refused saved sync-token,
a sync report with no token, a contact name taken three times).

The real-HTTP server of `test_provider_carddav_server.py` is reused; `Flaky` wraps its transport to answer
chosen requests with a chosen status, the way a server that refuses one thing would.
"""

from __future__ import annotations

from typing import Callable, Optional

import pytest

from app.accounts.provider import HttpResponse, TokenExpired, VersionConflict
from app.accounts.providers.carddav import CardDavProvider, basic_auth_header
from app.core.errors import AppErrorException

from tests.unit.test_provider_carddav import FakeTransport, multi, row
from tests.unit.test_provider_carddav_server import BOOK, LoopbackTransport, Server, drain, person


class Flaky:
    """The loopback transport, with chosen requests answered by `status` instead (`times` of them)."""

    def __init__(self) -> None:
        self.inner = LoopbackTransport()
        self.rules: list[list] = []
        self.calls: list[tuple[str, str]] = []

    def fail(self, method: str, match: Callable[[str], bool], status: int, times: int = 1_000, body: bytes = b""):
        self.rules.append([method, match, status, times, body])

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append((method, url))
        for rule in self.rules:
            if rule[0] == method and rule[3] > 0 and rule[1](url):
                rule[3] -= 1
                return HttpResponse(rule[2], {}, rule[4])
        return self.inner.request(method, url, headers=headers, body=body, timeout=timeout)


@pytest.fixture
def srv():
    s = Server()
    yield s
    s.stop()


def make(srv: Server, t: Optional[Flaky] = None, **kw) -> CardDavProvider:
    kw.setdefault("server_url", srv.base)
    return CardDavProvider(t or Flaky(), basic_auth_header("alice", lambda: "app-pw"), **kw)


def groups_on_server(srv: Server) -> dict[str, list[str]]:
    """Group name -> member uids, read straight from the server's cards."""
    out = {}
    for _name, (_etag, text) in srv.items.items():
        if "KIND:group" in text:
            gname = next(ln[3:] for ln in text.split("\r\n") if ln.startswith("FN:"))
            out[gname] = sorted(ln.rsplit(":", 1)[1] for ln in text.split("\r\n") if "MEMBER:urn:uuid:" in ln)
    return out


def uid_of(srv: Server, name: str) -> str:
    text = srv.items[name][1]
    return next(ln[4:] for ln in text.split("\r\n") if ln.startswith("UID:"))


# ---------------------------------------------------------------- batches of writes

def test_creating_many_contacts_writes_each_group_card_once_for_the_whole_batch(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="cards")
    cards = [person(n) for n in ("Ann", "Bob", "Cy")]
    for c in cards:
        c.groups = ["Team"]
    out = cd.create_many(cards)

    assert [r.ok for r in out] == [True, True, True]
    uids = sorted(uid_of(srv, r.remote_id) for r in out)
    assert groups_on_server(srv) == {"Team": uids}
    contact_puts = {r.remote_id for r in out}
    group_puts = [u for m, u in t.calls if m == "PUT" and u.rsplit("/", 1)[-1] not in contact_puts]
    assert len(group_puts) == 1                      # one group card for three contacts, not three writes


def test_updating_many_moves_each_contact_into_exactly_the_groups_it_names(srv):
    cd = make(srv, group_mode="cards")
    a, b = person("Ann"), person("Bob")
    a.groups = b.groups = ["Team"]
    made = cd.create_many([a, b])
    drain(cd)                                         # the connector learns the group cards as they are now
    ua, ub = uid_of(srv, made[0].remote_id), uid_of(srv, made[1].remote_id)

    a.groups, b.groups = [], ["Team", "Club"]
    current = {n: srv.items[n][0] for n in (made[0].remote_id, made[1].remote_id)}
    out = cd.update_many([(made[0].remote_id, current[made[0].remote_id], a),
                          (made[1].remote_id, current[made[1].remote_id], b)])

    assert [r.ok for r in out] == [True, True]
    assert groups_on_server(srv) == {"Team": [ub], "Club": [ub]}
    assert ua not in groups_on_server(srv)["Team"]


def test_a_stale_contact_in_a_batch_is_a_conflict_and_the_others_are_still_written(srv):
    cd = make(srv, group_mode="categories")
    made = cd.create_many([person("Ann"), person("Bob")])
    fresh = srv.items[made[1].remote_id][0]
    out = cd.update_many([(made[0].remote_id, '"not-the-etag"', person("Ann2")),
                          (made[1].remote_id, fresh, person("Bob2"))])
    assert out[0].conflict and not out[0].ok
    assert out[1].ok and "Bob2" in srv.items[made[1].remote_id][1]
    assert "Ann2" not in srv.items[made[0].remote_id][1]


def test_one_contact_the_server_refuses_costs_only_that_contact(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="categories")
    made = cd.create_many([person("Ann"), person("Bob")])
    t.fail("PUT", lambda u: u.endswith(made[0].remote_id), 500)
    out = cd.update_many([(made[0].remote_id, srv.items[made[0].remote_id][0], person("Ann2")),
                          (made[1].remote_id, srv.items[made[1].remote_id][0], person("Bob2"))])
    assert out[0].error is not None and out[0].error.code == "ERR_ACCOUNT_REJECTED" and not out[0].ok
    assert out[1].ok


def test_a_sign_in_failure_in_a_batch_stops_the_batch_for_the_whole_account(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="categories")
    cd.create_many([person("Ann")])
    t.fail("PUT", lambda u: True, 401)
    with pytest.raises(AppErrorException) as caught:
        cd.create_many([person("Bob"), person("Cy")])
    assert caught.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"


def test_a_group_card_that_cannot_be_written_leaves_the_contacts_reported_as_written(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="cards")
    a = person("Ann")
    a.groups = ["Team"]
    # the contact PUT goes through; the new group card's PUT (the second PUT) is refused
    t.inner_puts = 0
    original = t.request

    def second_put_fails(method, url, **kw):
        if method == "PUT":
            t.inner_puts += 1
            if t.inner_puts == 2:
                return HttpResponse(500, {}, b"")
        return original(method, url, **kw)
    t.request = second_put_fails
    out = cd.create_many([a])
    assert out[0].ok and out[0].remote_id in srv.items
    assert groups_on_server(srv) == {}


def test_deleting_many_reports_each_contact_on_its_own(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="categories")
    made = cd.create_many([person(n) for n in ("Ann", "Bob", "Cy", "Di")])
    ids = [r.remote_id for r in made]
    t.fail("DELETE", lambda u: u.endswith(ids[3]), 500)
    out = cd.delete_many([(ids[0], srv.items[ids[0]][0]),          # deleted
                          (ids[1], '"stale"'),                      # changed since: kept
                          ("nobody.vcf", '"x"'),                    # already gone: fine
                          (ids[3], srv.items[ids[3]][0])])          # refused by the server
    assert out[0].ok and ids[0] not in srv.items
    assert out[1].conflict and ids[1] in srv.items
    assert out[2].ok
    assert out[3].error is not None and not out[3].ok and ids[3] in srv.items


def test_a_sign_in_failure_while_deleting_many_stops_the_batch(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="categories")
    made = cd.create_many([person("Ann")])
    t.fail("DELETE", lambda u: True, 401)
    with pytest.raises(AppErrorException) as caught:
        cd.delete_many([(made[0].remote_id, "")])
    assert caught.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"


def test_deleting_a_contact_in_cards_mode_takes_it_out_of_its_group_cards(srv):
    cd = make(srv, group_mode="cards")
    a, b = person("Ann"), person("Bob")
    a.groups = b.groups = ["Team"]
    made = cd.create_many([a, b])
    drain(cd)
    ub = uid_of(srv, made[1].remote_id)
    cd.delete(made[0].remote_id, srv.items[made[0].remote_id][0])
    assert groups_on_server(srv) == {"Team": [ub]}


def test_a_group_card_that_moves_twice_while_a_contact_is_deleted_does_not_fail_the_delete(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="cards")
    a = person("Ann")
    a.groups = ["Team"]
    made = cd.create_many([a])
    drain(cd)
    group = next(n for n in srv.items if n != made[0].remote_id)
    t.fail("PUT", lambda u: u.endswith(group), 412)               # every edit of the group card loses the race
    cd.delete(made[0].remote_id, srv.items[made[0].remote_id][0])
    assert made[0].remote_id not in srv.items


# ---------------------------------------------------------------- group cards that move

def test_a_group_card_changed_by_someone_else_is_read_again_and_the_edit_applied_once_more(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="cards")
    a, b = person("Ann"), person("Bob")
    a.groups = ["Team"]
    made = cd.create_many([a, b])
    drain(cd)
    group = next(n for n in srv.items if n not in (made[0].remote_id, made[1].remote_id))
    t.fail("PUT", lambda u: u.endswith(group), 412, times=1)      # the first try loses the race
    b.groups = ["Team"]
    out = cd.update_many([(made[1].remote_id, srv.items[made[1].remote_id][0], b)])
    assert out[0].ok
    assert sorted(groups_on_server(srv)["Team"]) == sorted(
        [uid_of(srv, made[0].remote_id), uid_of(srv, made[1].remote_id)])


def test_a_group_card_that_keeps_moving_gives_up_after_one_retry(srv):
    t = Flaky()
    cd = make(srv, t, group_mode="cards")
    a = person("Ann")
    a.groups = ["Team"]
    made = cd.create_many([a])
    drain(cd)
    group = next(n for n in srv.items if n != made[0].remote_id)
    t.fail("PUT", lambda u: u.endswith(group), 412)
    before = len(t.calls)
    with pytest.raises(VersionConflict):
        cd._apply_group_delta(group, set(), {uid_of(srv, made[0].remote_id)})
    with pytest.raises(VersionConflict):
        cd._edit_group(group, uid_of(srv, made[0].remote_id), False)
    assert len([u for m, u in t.calls[before:] if m == "PUT" and u.endswith(group)]) == 4    # two tries each


def test_a_group_card_deleted_elsewhere_is_forgotten_not_rewritten(srv):
    cd = make(srv, group_mode="cards")
    a = person("Ann")
    a.groups = ["Team"]
    made = cd.create_many([a])
    drain(cd)
    group = next(n for n in srv.items if n != made[0].remote_id)
    srv.remove(group)
    cd._apply_group_delta(group, {"X"}, set())
    assert group not in cd._g and group not in srv.items
    cd._g[group] = ["Team", []]
    cd._edit_group(group, "X", True)
    assert group not in cd._g and group not in srv.items


# ---------------------------------------------------------------- reading many, listing

def test_reading_many_pages_through_and_leaves_group_cards_and_gone_contacts_out(srv):
    cd = make(srv, group_mode="cards", page_size=2)
    a, b, c = person("Ann"), person("Bob"), person("Cy")
    a.groups = ["Team"]
    made = cd.create_many([a, b, c])
    group = next(n for n in srv.items if n not in {r.remote_id for r in made})
    wanted = [r.remote_id for r in made] + [group, "nobody.vcf"]
    found = cd.get_many(wanted)
    assert set(found) == set(wanted)
    assert found[group] is None and found["nobody.vcf"] is None
    assert found[made[0].remote_id].card.groups == ["Team"]
    assert found[made[1].remote_id].card.scalars["given"] == "Bob"


def test_the_check_lists_contacts_and_not_the_group_cards(srv):
    cd = make(srv, group_mode="cards")
    a = person("Ann")
    a.groups = ["Team"]
    made = cd.create_many([a, person("Bob")])
    listed = cd.list_ids()
    assert set(listed) == {r.remote_id for r in made}
    assert listed[made[0].remote_id][0] == srv.items[made[0].remote_id][0]


# ---------------------------------------------------------------- refusals that are not success

def _canned(**kw) -> tuple[FakeTransport, CardDavProvider]:
    t = FakeTransport()
    kw.setdefault("addressbook_url", "https://dav.example.org/book/")
    return t, CardDavProvider(t, lambda: "Basic x", **kw)


def test_a_redirect_loop_is_a_registered_error_not_a_hang():
    t = FakeTransport()
    t.add("GET", "https://dav.example.org/book/a.vcf", "", 302, {"Location": "https://dav.example.org/book/a.vcf"})
    cd = CardDavProvider(t, lambda: "Basic x", addressbook_url="https://dav.example.org/book/")
    with pytest.raises(AppErrorException) as caught:
        cd.get("a.vcf")
    assert caught.value.error.code == "ERR_ACCOUNT_REJECTED"
    assert "redirect" in caught.value.error.context["reason"]
    assert len(t.calls) == 6


def test_a_saved_sync_token_the_server_answers_400_to_is_token_expired():
    t, cd = _canned()
    t.add("REPORT", "https://dav.example.org/book/", "", 400)
    cd._has_sync = True
    with pytest.raises(TokenExpired):
        list(cd.pull('{"v":1,"m":"sync","t":"tok-old","i":{},"g":{}}'))


def test_a_sync_report_without_a_new_token_is_an_error_not_an_empty_change_list():
    t, cd = _canned()
    t.add("REPORT", "https://dav.example.org/book/", multi(row("/book/a.vcf", "<d:getetag>e1</d:getetag>")))
    cd._has_sync = True
    with pytest.raises(AppErrorException) as caught:
        list(cd.pull('{"v":1,"m":"sync","t":"tok-1","i":{},"g":{}}'))
    assert caught.value.error.code == "ERR_ACCOUNT_REJECTED"


def test_a_name_taken_three_times_running_is_an_error_not_a_silent_skip():
    t, cd = _canned(group_mode="categories")
    real = t.request

    def any_put_is_taken(method, url, **kw):
        if method == "PUT":
            t.calls.append({"method": method, "url": url, "headers": {}, "body": ""})
            return HttpResponse(412, {}, b"")
        return real(method, url, **kw)
    t.request = any_put_is_taken
    with pytest.raises(AppErrorException) as caught:
        cd.create(person("Ann"))
    assert caught.value.error.code == "ERR_ACCOUNT_REJECTED"
    assert len(t.of("PUT")) == 3


def test_updating_a_contact_the_connector_never_saw_reads_its_uid_and_a_missing_one_is_a_conflict(srv):
    srv.put_card("uid-7", person("Ann"))
    cd = make(srv, group_mode="categories")
    etag = srv.items["uid-7.vcf"][0]
    new = cd.update("uid-7.vcf", etag, person("Ann2"))
    assert new == srv.items["uid-7.vcf"][0]
    assert "UID:uid-7" in srv.items["uid-7.vcf"][1].splitlines()
    with pytest.raises(VersionConflict):              # never written back as a new card, even with no etag
        cd.update("nobody.vcf", "", person("Bob"))
    assert "nobody.vcf" not in srv.items


def test_a_contact_read_the_server_refuses_is_an_error_not_a_missing_contact(srv):
    t = Flaky()
    t.fail("GET", lambda u: u.endswith("a.vcf"), 500)
    cd = make(srv, t)
    with pytest.raises(AppErrorException) as caught:
        cd.get("a.vcf")
    assert caught.value.error.code == "ERR_ACCOUNT_REJECTED"
    assert caught.value.error.context["reason"] == "read a contact: HTTP 500"


def test_a_delete_the_server_refuses_is_an_error(srv):
    srv.put_card("uid-1", person("Ann"))
    t = Flaky()
    t.fail("DELETE", lambda u: True, 403)
    cd = make(srv, t)
    with pytest.raises(AppErrorException):
        cd.delete("uid-1.vcf", "")
    assert "uid-1.vcf" in srv.items
