"""Microsoft Graph mail connector (order 1k 5b) against hand-written responses.

Layer: L7 (test)

Same method as the Gmail tests: a scripted transport that answers exact URLs,
fails on any other, and records the verbs. No network.
"""

from __future__ import annotations

import json

import pytest

from app.accounts.provider import HttpResponse, MailProvider, RemoteMessage, RemoteMessageGone, TokenExpired
from app.accounts.providers.graph_mail import GraphMailProvider
from app.core.errors import AppErrorException

G = "https://graph.microsoft.com/v1.0"
SELECT = "$select=id,receivedDateTime,isRead,categories,changeKey"


def ok(obj) -> HttpResponse:
    return HttpResponse(200, {}, json.dumps(obj).encode())


def status(code: int, obj=None) -> HttpResponse:
    return HttpResponse(code, {}, json.dumps(obj or {}).encode())


def mime(subject: str) -> HttpResponse:
    body = (f"From: a@example.com\r\nTo: b@example.com\r\nSubject: {subject}\r\n"
            f"Message-ID: <{subject}@x>\r\n\r\nBody {subject}\r\n").encode()
    return HttpResponse(200, {"content-type": "message/rfc822"}, body)


def raw_of(subject: str) -> bytes:
    return mime(subject).body


class Scripted:
    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append((method, url, dict(headers or {})))
        queue = self.routes.get(url)
        assert queue, f"unexpected request: {method} {url}"
        return queue.pop(0)

    @property
    def urls(self):
        return [u for _m, u, _h in self.calls]


def provider(t, **kw):
    kw.setdefault("page_size", 2)
    kw.setdefault("pace_s", 0.0)
    return GraphMailProvider(t, lambda: "tok-g", account_name="Work", **kw)


def pages(p, token=None, **kw):
    return list(p.pull(token, **kw))


def folder(fid, name, total=0, children=0):
    return {"id": fid, "displayName": name, "totalItemCount": total, "childFolderCount": children}


def folder_routes(*, specials=True):
    routes = {
        f"{G}/me/mailFolders?$top=100": [ok({"value": [
            folder("AAA", "Inbox", 3, 1), folder("BBB", "Sent Items", 1),
            folder("DDD", "Deleted Items", 5), folder("JJJ", "Junk Email", 2)]})],
        f"{G}/me/mailFolders/AAA/childFolders?$top=100": [ok({"value": [folder("CCC", "Projects", 1)]})],
    }
    if specials:
        routes[f"{G}/me/mailFolders/deleteditems?$select=id"] = [ok({"id": "DDD"})]
        routes[f"{G}/me/mailFolders/junkemail?$select=id"] = [ok({"id": "JJJ"})]
    return routes


def delta(fid, since=None):
    url = f"{G}/me/mailFolders/{fid}/messages/delta?{SELECT}"
    return url + (f"&$filter=receivedDateTime%20ge%20{since}" if since else "")


def item(mid, **kw):
    return {"id": mid, "receivedDateTime": "2024-03-05T10:00:00Z", "isRead": False,
            "categories": [], "changeKey": "ck1", **kw}


def first_routes():
    return {
        **folder_routes(),
        delta("AAA"): [ok({"value": [item("a1", categories=["Blue"]), item("a2", isRead=True)],
                           "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-aaa"})],
        f"{G}/next-aaa": [ok({"value": [item("a3")], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-aaa"})],
        delta("CCC"): [ok({"value": [], "@odata.deltaLink": f"{G}/delta-ccc"})],
        delta("BBB"): [ok({"value": [item("b1")], "@odata.deltaLink": f"{G}/delta-bbb"})],
        f"{G}/me/messages/a1/$value": [mime("a1")],
        f"{G}/me/messages/a2/$value": [mime("a2")],
        f"{G}/me/messages/a3/$value": [mime("a3")],
        f"{G}/me/messages/b1/$value": [mime("b1")],
    }


def test_it_satisfies_the_mail_provider_contract():
    assert isinstance(provider(Scripted({})), MailProvider)


def test_first_fetch_walks_folders_pages_and_collects_delta_links():
    t = Scripted(first_routes())
    got = pages(provider(t))
    assert {m for m, _u, _h in t.calls} == {"GET"}
    # deleted items and junk are never delta-ed, nor are their ids fetched
    assert not any("DDD" in u or "JJJ" in u for u in t.urls if "/messages/delta" in u)
    delta_urls = [u for u in t.urls if "delta" in u or "next-aaa" in u]
    assert delta_urls == [delta("AAA"), f"{G}/next-aaa", delta("CCC"), delta("BBB")]
    prefer = t.calls[-1][2]["Prefer"]
    assert 'odata.maxpagesize=2' in prefer and 'IdType="ImmutableId"' in prefer
    assert t.calls[0][2]["Authorization"] == "Bearer tok-g"

    assert [p.done for p in got] == [False, False, False, True]
    assert got[0].total_hint == 5                      # Inbox 3 + Projects 1 + Sent 1
    first, second = got[0].changes
    assert isinstance(first, RemoteMessage)
    assert first.remote_id == "AAA/a1" and first.folder == "Inbox"
    assert first.raw == raw_of("a1") and first.labels == ["Blue"]
    assert first.seen is False and second.seen is True
    assert first.received_at == 1709632800.0 and first.version == "ck1"
    assert got[2].changes == [] and got[3].changes[0].folder == "Sent Items"

    mid = json.loads(got[0].next_token)
    assert mid["cur"] == {"folder": "AAA", "next": f"{G}/next-aaa"} and mid["f"] == {}
    end = json.loads(got[3].next_token)
    assert end["cur"] is None
    assert end["f"] == {"AAA": {"delta": f"{G}/delta-aaa"}, "CCC": {"delta": f"{G}/delta-ccc"},
                        "BBB": {"delta": f"{G}/delta-bbb"}}


def test_a_child_folder_is_named_by_its_path():
    t = Scripted({**first_routes(), delta("CCC"): [ok({"value": [item("c1")], "@odata.deltaLink": f"{G}/delta-ccc"})],
                  f"{G}/me/messages/c1/$value": [mime("c1")]})
    got = pages(provider(t))
    c1 = [c for p in got for c in p.changes if c.remote_id == "CCC/c1"][0]
    assert c1.folder == "Inbox/Projects"


def test_resume_from_a_mid_fetch_token_continues_at_the_next_link():
    t = Scripted(first_routes())
    first = next(provider(t).pull(None))
    t2 = Scripted({**folder_routes(),
                   f"{G}/next-aaa": [ok({"value": [item("a3")], "@odata.deltaLink": f"{G}/delta-aaa"})],
                   delta("CCC"): [ok({"value": [], "@odata.deltaLink": f"{G}/delta-ccc"})],
                   delta("BBB"): [ok({"value": [], "@odata.deltaLink": f"{G}/delta-bbb"})],
                   f"{G}/me/messages/a3/$value": [mime("a3")]})
    rest = pages(provider(t2), first.next_token)
    assert delta("AAA") not in t2.urls                 # the first page is not asked for again
    assert [c.remote_id for p in rest for c in p.changes] == ["AAA/a3"]
    assert rest[-1].done


def test_since_bounds_the_first_delta_of_every_folder():
    routes = {**folder_routes(),
              delta("AAA", "2024-01-01T00:00:00Z"): [ok({"value": [], "@odata.deltaLink": f"{G}/d1"})],
              delta("CCC", "2024-01-01T00:00:00Z"): [ok({"value": [], "@odata.deltaLink": f"{G}/d2"})],
              delta("BBB", "2024-01-01T00:00:00Z"): [ok({"value": [], "@odata.deltaLink": f"{G}/d3"})]}
    t = Scripted(routes)
    got = pages(provider(t), since=1704067200)
    assert got[-1].done


def test_incremental_uses_saved_delta_links_and_follows_removals():
    token = json.dumps({"v": 1, "f": {"AAA": {"delta": f"{G}/delta-aaa"}, "BBB": {"delta": f"{G}/delta-bbb"},
                                      "CCC": {"delta": f"{G}/delta-ccc"}}, "cur": None})
    t = Scripted({
        **folder_routes(),
        f"{G}/delta-aaa": [ok({"value": [{"id": "a1", "@removed": {"reason": "deleted"}},
                                         item("a2", isRead=True, categories=["Red"], changeKey="ck2")],
                               "@odata.deltaLink": f"{G}/delta-aaa-2"})],
        f"{G}/delta-ccc": [ok({"value": [], "@odata.deltaLink": f"{G}/delta-ccc-2"})],
        f"{G}/delta-bbb": [ok({"value": [], "@odata.deltaLink": f"{G}/delta-bbb-2"})],
        f"{G}/me/messages/a2/$value": [mime("a2")],
    })
    got = pages(provider(t), token)
    assert [u for u in t.urls if "delta" in u] == [f"{G}/delta-aaa", f"{G}/delta-ccc", f"{G}/delta-bbb"]
    gone, changed = got[0].changes
    assert isinstance(gone, RemoteMessageGone) and gone.remote_id == "AAA/a1"
    assert changed.labels == ["Red"] and changed.version == "ck2"
    assert json.loads(got[-1].next_token)["f"]["AAA"] == {"delta": f"{G}/delta-aaa-2"}


def test_a_message_deleted_before_its_mime_is_fetched_is_gone():
    t = Scripted({**folder_routes(),
                  delta("AAA"): [ok({"value": [item("a1")], "@odata.deltaLink": f"{G}/d1"})],
                  delta("CCC"): [ok({"value": [], "@odata.deltaLink": f"{G}/d2"})],
                  delta("BBB"): [ok({"value": [], "@odata.deltaLink": f"{G}/d3"})],
                  f"{G}/me/messages/a1/$value": [status(404)]})
    got = pages(provider(t))
    assert isinstance(got[0].changes[0], RemoteMessageGone)


def test_a_folder_that_left_the_selection_is_dropped_from_the_token():
    token = json.dumps({"v": 1, "f": {"AAA": {"delta": f"{G}/delta-aaa"}, "ZZZ": {"delta": f"{G}/zzz"}}, "cur": None})
    t = Scripted({**folder_routes(),
                  f"{G}/delta-aaa": [ok({"value": [], "@odata.deltaLink": f"{G}/d1"})],
                  delta("CCC"): [ok({"value": [], "@odata.deltaLink": f"{G}/d2"})],
                  delta("BBB"): [ok({"value": [], "@odata.deltaLink": f"{G}/d3"})]})
    got = pages(provider(t), token)
    assert "ZZZ" not in json.loads(got[-1].next_token)["f"]


def test_chosen_folders_by_path_and_junk_when_asked():
    routes = {**folder_routes(specials=False),
              delta("BBB"): [ok({"value": [], "@odata.deltaLink": f"{G}/d3"})],
              delta("JJJ"): [ok({"value": [], "@odata.deltaLink": f"{G}/d4"})]}
    t = Scripted(routes)
    pages(provider(t), folders=["sent items"], include_spam_trash=False)
    assert delta("AAA") not in t.urls and delta("BBB") in t.urls

    t2 = Scripted({**folder_routes(specials=False),
                   **{delta(f): [ok({"value": [], "@odata.deltaLink": f"{G}/d-{f}"})] for f in ("AAA", "CCC", "BBB", "DDD", "JJJ")}})
    pages(provider(t2), include_spam_trash=True)
    assert delta("DDD") in t2.urls and delta("JJJ") in t2.urls


def test_410_is_an_expired_token():
    token = json.dumps({"v": 1, "f": {"AAA": {"delta": f"{G}/delta-aaa"}}, "cur": None})
    t = Scripted({**folder_routes(), f"{G}/delta-aaa": [status(410, {"error": {"code": "SyncStateNotFound"}})]})
    with pytest.raises(TokenExpired):
        pages(provider(t), token)


def test_a_token_this_code_did_not_write_is_expired():
    for bad in ("nope", "[1]", json.dumps({"v": 7})):
        with pytest.raises(TokenExpired):
            pages(provider(Scripted({})), bad)


@pytest.mark.parametrize("code,body,expected", [
    (401, {}, "ERR_ACCOUNT_SIGNIN_EXPIRED"),
    (429, {}, "ERR_PROVIDER_LIMIT"),
    (403, {"error": {"message": "AADSTS65001: The user or administrator has not consented"}}, "ERR_ACCOUNT_ADMIN_CONSENT"),
    (403, {"error": {"message": "Access is denied"}}, "ERR_ACCOUNT_REJECTED"),
    (500, {}, "ERR_ACCOUNT_REJECTED"),
])
def test_http_failures_are_app_errors(code, body, expected):
    t = Scripted({f"{G}/me/mailFolders?$top=100": [status(code, body)]})
    with pytest.raises(AppErrorException) as err:
        pages(provider(t))
    assert err.value.error.code == expected and err.value.error.suggestion


def test_list_folders_returns_paths():
    t = Scripted(folder_routes(specials=False))
    assert provider(t).list_folders() == ["Inbox", "Inbox/Projects", "Sent Items", "Deleted Items", "Junk Email"]


def test_no_folders_still_finishes():
    t = Scripted({f"{G}/me/mailFolders?$top=100": [ok({"value": []})]})
    got = pages(provider(t))
    assert len(got) == 1 and got[0].done and got[0].changes == []
