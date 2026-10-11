"""Gmail API connector (order 1k 5a) against hand-written responses.

Layer: L7 (test)

A scripted transport answers exact URLs and nothing else: a request the
connector was not expected to make fails the test, which is how the URLs and the
verbs are asserted. No network.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.accounts.provider import HttpResponse, MailProvider, RemoteMessage, RemoteMessageGone, TokenExpired
from app.accounts.providers.gmail_api import GmailApiMailProvider
from app.core.errors import AppErrorException

B = "https://gmail.googleapis.com/gmail/v1/users/me"
HIST = ("startHistoryId={h}&historyTypes=messageAdded&historyTypes=messageDeleted"
        "&historyTypes=labelAdded&historyTypes=labelRemoved&maxResults=2")


def ok(obj) -> HttpResponse:
    return HttpResponse(200, {"content-type": "application/json"}, json.dumps(obj).encode())


def status(code: int, obj=None) -> HttpResponse:
    return HttpResponse(code, {}, json.dumps(obj or {}).encode())


def raw_mail(subject: str, mid: str) -> bytes:
    return (f"From: a@example.com\r\nTo: b@example.com\r\nSubject: {subject}\r\n"
            f"Message-ID: <{mid}@example.com>\r\nDate: Tue, 14 Nov 2023 22:13:20 +0000\r\n\r\nBody of {subject}\r\n").encode()


def gmail_msg(mid: str, labels: list[str], *, subject: str = "", internal_ms: int = 1700000000000,
              history_id: str = "90") -> dict:
    raw = raw_mail(subject or mid, mid)
    return {"id": mid, "threadId": "t" + mid, "labelIds": labels, "internalDate": str(internal_ms),
            "historyId": history_id, "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}


LABELS = {"labels": [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "SENT", "name": "SENT", "type": "system"},
    {"id": "SPAM", "name": "SPAM", "type": "system"},
    {"id": "TRASH", "name": "TRASH", "type": "system"},
    {"id": "UNREAD", "name": "UNREAD", "type": "system"},
    {"id": "CATEGORY_PROMOTIONS", "name": "CATEGORY_PROMOTIONS", "type": "system"},
    {"id": "Label_7", "name": "Receipts", "type": "user"},
]}


class Scripted:
    """Answers each exact URL from a queue; records every call."""

    def __init__(self, routes: dict[str, list[HttpResponse]]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append((method, url, dict(headers or {})))
        queue = self.routes.get(url)
        assert queue, f"unexpected request: {method} {url}"
        return queue.pop(0)

    @property
    def urls(self) -> list[str]:
        return [u for _m, u, _h in self.calls]


def provider(transport, **kw) -> GmailApiMailProvider:
    kw.setdefault("page_size", 2)
    kw.setdefault("pace_s", 0.0)
    return GmailApiMailProvider(transport, lambda: "tok-1", account_name="Home Gmail", **kw)


def pages(p, token=None, **kw):
    return list(p.pull(token, **kw))


def first_fetch_routes() -> dict[str, list[HttpResponse]]:
    return {
        f"{B}/labels": [ok(LABELS)],
        f"{B}/profile": [ok({"emailAddress": "me@gmail.com", "messagesTotal": 3, "historyId": "100"})],
        f"{B}/messages?maxResults=2": [ok({"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "P2"})],
        f"{B}/messages?maxResults=2&pageToken=P2": [ok({"messages": [{"id": "m3"}]})],
        f"{B}/messages/m1?format=raw": [ok(gmail_msg("m1", ["INBOX", "UNREAD", "Label_7"]))],
        f"{B}/messages/m2?format=raw": [ok(gmail_msg("m2", ["SENT", "CATEGORY_PROMOTIONS"]))],
        f"{B}/messages/m3?format=raw": [ok(gmail_msg("m3", ["INBOX"], internal_ms=1700000100000))],
    }


def test_it_satisfies_the_mail_provider_contract():
    assert isinstance(provider(Scripted({})), MailProvider)


def test_first_fetch_pages_labels_and_a_resumable_token():
    t = Scripted(first_fetch_routes())
    got = pages(provider(t))
    assert [m for m, _u, _h in t.calls] == ["GET"] * len(t.calls)
    assert t.urls == [f"{B}/labels", f"{B}/profile", f"{B}/messages?maxResults=2",
                      f"{B}/messages/m1?format=raw", f"{B}/messages/m2?format=raw",
                      f"{B}/messages?maxResults=2&pageToken=P2", f"{B}/messages/m3?format=raw"]
    assert all(h["Authorization"] == "Bearer tok-1" for _m, _u, h in t.calls)
    assert [p.done for p in got] == [False, True]
    assert got[0].total_hint == 3

    m1, m2 = got[0].changes
    assert isinstance(m1, RemoteMessage)
    assert m1.remote_id == "m1" and m1.folder == ""
    assert m1.raw == raw_mail("m1", "m1")
    assert m1.labels == ["Inbox", "Receipts"]               # UNREAD is a state, not a label
    assert m1.seen is False and m2.seen is True
    assert m2.labels == ["Sent", "Promotions"]
    assert m1.received_at == 1700000000.0
    assert m1.version == "90"

    mid = json.loads(got[0].next_token)
    assert mid["phase"] == "list" and mid["page_token"] == "P2" and mid["history_id"] == "100"
    end = json.loads(got[1].next_token)
    assert end == {"v": 1, "phase": "history", "history_id": "100"}


def test_resume_from_a_mid_fetch_token_asks_only_for_what_is_left():
    first = Scripted(first_fetch_routes())
    page1 = next(provider(first).pull(None))
    t = Scripted({k: v for k, v in first_fetch_routes().items()
                  if k in (f"{B}/labels", f"{B}/messages?maxResults=2&pageToken=P2", f"{B}/messages/m3?format=raw")})
    rest = pages(provider(t), page1.next_token)
    assert t.urls == [f"{B}/labels", f"{B}/messages?maxResults=2&pageToken=P2", f"{B}/messages/m3?format=raw"]
    assert [m.remote_id for p in rest for m in p.changes] == ["m3"]
    assert rest[-1].done and json.loads(rest[-1].next_token)["history_id"] == "100"


def test_a_first_fetch_is_bounded_by_since():
    t = Scripted({
        f"{B}/labels": [ok(LABELS)],
        f"{B}/profile": [ok({"messagesTotal": 9, "historyId": "5"})],
        f"{B}/messages?maxResults=2&q=after%3A1700000000": [ok({})],
    })
    got = pages(provider(t), since=1700000000.9)
    assert len(got) == 1 and got[0].done and got[0].changes == []
    assert json.loads(got[0].next_token)["history_id"] == "5"


def test_a_resumed_first_fetch_keeps_its_since_window():
    t = Scripted({
        f"{B}/labels": [ok(LABELS), ok(LABELS)],
        f"{B}/profile": [ok({"messagesTotal": 9, "historyId": "5"})],
        f"{B}/messages?maxResults=2&q=after%3A1700000000": [ok({"messages": [{"id": "m1"}], "nextPageToken": "Q"})],
        f"{B}/messages/m1?format=raw": [ok(gmail_msg("m1", ["INBOX"]))],
        f"{B}/messages?maxResults=2&q=after%3A1700000000&pageToken=Q": [ok({})],
    })
    p = provider(t)
    it = p.pull(None, since=1700000000)
    token = next(it).next_token
    resumed = pages(provider(t), token)            # the caller forgets since: the token remembers it
    assert resumed[-1].done


def test_spam_and_trash_are_left_out_unless_asked():
    base = {f"{B}/labels": [ok(LABELS)], f"{B}/profile": [ok({"historyId": "1"})]}
    t = Scripted({**base, f"{B}/messages?maxResults=2&includeSpamTrash=true": [ok({})]})
    pages(provider(t), include_spam_trash=True)
    t2 = Scripted({**base, f"{B}/messages?maxResults=2": [ok({})]})
    pages(provider(t2))
    assert "includeSpamTrash" not in " ".join(t2.urls)


def test_chosen_labels_are_listed_one_by_one_and_not_double_counted():
    t = Scripted({
        f"{B}/labels": [ok(LABELS)],
        f"{B}/profile": [ok({"historyId": "7"})],
        f"{B}/messages?maxResults=2&labelIds=INBOX": [ok({"messages": [{"id": "m1"}]})],
        f"{B}/messages?maxResults=2&labelIds=Label_7": [ok({"messages": [{"id": "m1"}, {"id": "m2"}]})],
        f"{B}/messages/m1?format=raw": [ok(gmail_msg("m1", ["INBOX", "Label_7"]))],
        f"{B}/messages/m2?format=raw": [ok(gmail_msg("m2", ["Label_7"]))],
    })
    got = pages(provider(t), folders=["inbox", "Receipts"])
    ids = [m.remote_id for p in got for m in p.changes]
    assert ids == ["m1", "m2"]                      # m1 is under both labels, fetched once
    assert got[-1].done


def test_a_message_deleted_between_list_and_get_is_skipped():
    t = Scripted({
        f"{B}/labels": [ok(LABELS)], f"{B}/profile": [ok({"historyId": "7"})],
        f"{B}/messages?maxResults=2": [ok({"messages": [{"id": "m1"}, {"id": "m2"}]})],
        f"{B}/messages/m1?format=raw": [status(404)],
        f"{B}/messages/m2?format=raw": [ok(gmail_msg("m2", ["INBOX"]))],
    })
    got = pages(provider(t))
    assert [m.remote_id for m in got[0].changes] == ["m2"]


# --- incremental: history ----------------------------------------------------

def hist_token(h="100", page=None) -> str:
    state = {"v": 1, "phase": "history", "history_id": h}
    if page:
        state["page_token"] = page
    return json.dumps(state)


def test_history_adds_deletes_and_label_changes():
    t = Scripted({
        f"{B}/labels": [ok(LABELS)],
        f"{B}/history?" + HIST.format(h="100"): [ok({
            "historyId": "120",
            "history": [
                {"id": "101", "messagesAdded": [{"message": {"id": "n1", "labelIds": ["INBOX"]}}]},
                {"id": "102", "messagesDeleted": [{"message": {"id": "d1"}}]},
                {"id": "103", "labelsAdded": [{"message": {"id": "o1"}, "labelIds": ["Label_7"]}]},
                {"id": "104", "labelsRemoved": [{"message": {"id": "r1"}, "labelIds": ["INBOX"]}]},
                {"id": "105", "messagesAdded": [{"message": {"id": "x1"}}]},
                {"id": "106", "messagesDeleted": [{"message": {"id": "x1"}}]},
            ]})],
        f"{B}/messages/n1?format=raw": [ok(gmail_msg("n1", ["INBOX", "UNREAD"]))],
        f"{B}/messages/o1?format=raw": [ok(gmail_msg("o1", ["INBOX", "Label_7"]))],
        f"{B}/messages/r1?format=raw": [ok(gmail_msg("r1", ["Label_7"]))],
    })
    got = pages(provider(t), hist_token())
    assert len(got) == 1 and got[0].done
    assert t.urls[1].startswith(f"{B}/history?startHistoryId=100&historyTypes=messageAdded")
    assert not any("x1" in u for u in t.urls)              # added then deleted: never fetched
    by_id = {c.remote_id: c for c in got[0].changes}
    assert isinstance(by_id["d1"], RemoteMessageGone) and isinstance(by_id["x1"], RemoteMessageGone)
    assert by_id["n1"].labels == ["Inbox"] and by_id["n1"].seen is False
    assert by_id["o1"].labels == ["Inbox", "Receipts"]       # new label seen
    assert by_id["r1"].labels == ["Receipts"]                # label removed
    assert json.loads(got[0].next_token) == {"v": 1, "phase": "history", "history_id": "120"}


def test_history_paging_carries_the_page_token_in_the_change_token():
    url1 = f"{B}/history?" + HIST.format(h="100")
    t = Scripted({
        f"{B}/labels": [ok(LABELS)],
        url1: [ok({"historyId": "130", "nextPageToken": "H2",
                   "history": [{"id": "101", "messagesDeleted": [{"message": {"id": "d1"}}]}]})],
        url1 + "&pageToken=H2": [ok({"historyId": "130",
                                     "history": [{"id": "102", "messagesDeleted": [{"message": {"id": "d2"}}]}]})],
    })
    got = pages(provider(t), hist_token())
    assert [p.done for p in got] == [False, True]
    assert json.loads(got[0].next_token) == {"v": 1, "phase": "history", "history_id": "100", "page_token": "H2"}
    assert json.loads(got[1].next_token)["history_id"] == "130"
    # and the mid token resumes at page 2
    t2 = Scripted({f"{B}/labels": [ok(LABELS)], url1 + "&pageToken=H2": [ok({"historyId": "130", "history": []})]})
    assert pages(provider(t2), got[0].next_token)[-1].done


def test_nothing_new_still_returns_one_done_page_and_the_same_cursor():
    t = Scripted({f"{B}/labels": [ok(LABELS)], f"{B}/history?" + HIST.format(h="100"): [ok({"historyId": "100"})]})
    got = pages(provider(t), hist_token())
    assert len(got) == 1 and got[0].done and got[0].changes == []
    assert json.loads(got[0].next_token)["history_id"] == "100"


def test_a_message_moved_to_trash_or_out_of_the_chosen_labels_is_gone():
    t = Scripted({
        f"{B}/labels": [ok(LABELS)],
        f"{B}/history?" + HIST.format(h="100"): [ok({"historyId": "9", "history": [
            {"id": "1", "labelsAdded": [{"message": {"id": "a"}, "labelIds": ["TRASH"]}]},
            {"id": "2", "labelsRemoved": [{"message": {"id": "b"}, "labelIds": ["INBOX"]}]}]})],
        f"{B}/messages/a?format=raw": [ok(gmail_msg("a", ["TRASH"]))],
        f"{B}/messages/b?format=raw": [ok(gmail_msg("b", ["Label_7"]))],
    })
    got = pages(provider(t), hist_token(), folders=["INBOX"])
    assert all(isinstance(c, RemoteMessageGone) for c in got[0].changes)
    assert {c.remote_id for c in got[0].changes} == {"a", "b"}


def test_history_404_is_an_expired_token():
    t = Scripted({f"{B}/labels": [ok(LABELS)], f"{B}/history?" + HIST.format(h="100"): [status(404)]})
    with pytest.raises(TokenExpired):
        pages(provider(t), hist_token())


def test_a_token_this_code_did_not_write_is_expired():
    for bad in ("not json", "[]", json.dumps({"v": 99, "phase": "history"})):
        with pytest.raises(TokenExpired):
            pages(provider(Scripted({f"{B}/labels": [ok(LABELS)]})), bad)


# --- errors --------------------------------------------------------------------

@pytest.mark.parametrize("code,expected", [(401, "ERR_ACCOUNT_SIGNIN_EXPIRED"), (429, "ERR_PROVIDER_LIMIT"),
                                           (403, "ERR_ACCOUNT_REJECTED"), (500, "ERR_ACCOUNT_REJECTED")])
def test_http_failures_are_app_errors_that_name_the_account(code, expected):
    t = Scripted({f"{B}/labels": [status(code)]})
    with pytest.raises(AppErrorException) as err:
        pages(provider(t))
    assert err.value.error.code == expected
    assert "Home Gmail" in err.value.error.message or code in (403, 500, 429)
    assert err.value.error.suggestion


def test_list_folders_returns_friendly_label_names():
    t = Scripted({f"{B}/labels": [ok(LABELS)]})
    names = provider(t).list_folders()
    assert "Inbox" in names and "Receipts" in names and "UNREAD" not in names and "Unread" not in names


def test_close_is_harmless():
    provider(Scripted({})).close()
