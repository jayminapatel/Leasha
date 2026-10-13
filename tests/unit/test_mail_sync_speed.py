"""Mail sync speed: Graph downloads in parallel, pages read one ahead, kept connections.

Layer: L7 (test)

Measured 2026-10-13 on the owner's Microsoft account: about 59 messages a minute, a
page of 50 spending ~25 s on 50 one-at-a-time downloads (each on a new TLS
connection, ~240 ms against ~50 ms kept) and ~19 s embedding, in turn. These pin
the three changes that let the two overlap. No network.
"""

from __future__ import annotations

import json
import threading

import pytest

from app.accounts.mail_sync import _ReadAhead, build_mail_provider
from app.accounts.provider import HttpResponse, MailPage, RemoteMessage
from app.accounts.providers.graph_mail import MAX_WORKERS, GraphMailProvider
from app.core.errors import AppErrorException

G = "https://graph.microsoft.com/v1.0"
DELTA = f"{G}/me/mailFolders/AAA/messages/delta?$select=id,receivedDateTime,isRead,categories,changeKey"


def ok(obj) -> HttpResponse:
    return HttpResponse(200, {}, json.dumps(obj).encode())


class Mailbox:
    """One folder of `n` messages; every `$value` waits at `barrier` when one is given."""

    def __init__(self, n: int, *, barrier=None, fail=None):
        self.ids = [f"m{i}" for i in range(n)]
        self.barrier, self.fail = barrier, fail
        self.lock = threading.Lock()
        self.in_flight = self.most = 0

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        assert method == "GET"
        if url == f"{G}/me/mailFolders?$top=100":
            return ok({"value": [{"id": "AAA", "displayName": "Inbox", "totalItemCount": len(self.ids)}]})
        if url.endswith("?$select=id"):                     # the bins
            return HttpResponse(404, {}, b"{}")
        if url == DELTA:
            return ok({"value": [{"id": i, "receivedDateTime": "2024-03-05T10:00:00Z"} for i in self.ids],
                       "@odata.deltaLink": f"{G}/delta-aaa"})
        mid = url.split("/messages/")[1].split("/")[0]
        with self.lock:
            self.in_flight += 1
            self.most = max(self.most, self.in_flight)
        try:
            if self.barrier is not None:
                self.barrier.wait()
            if mid == self.fail:
                return HttpResponse(500, {}, b"{}")
            return HttpResponse(200, {}, f"Subject: {mid}\r\n\r\nbody\r\n".encode())
        finally:
            with self.lock:
                self.in_flight -= 1


def pull_all(box, **kw):
    p = GraphMailProvider(box, lambda: "tok", account_name="Work", pace_s=0.0, **kw)
    try:
        return [c for page in p.pull(None) for c in page.changes]
    finally:
        p.close()


def test_graph_downloads_four_messages_at_once_and_keeps_their_order():
    box = Mailbox(12, barrier=threading.Barrier(MAX_WORKERS, timeout=5))
    got = pull_all(box)                                   # one at a time would break the barrier
    assert box.most == MAX_WORKERS
    assert [c.remote_id for c in got] == [f"AAA/m{i}" for i in range(12)]
    assert all(isinstance(c, RemoteMessage) and c.raw.startswith(b"Subject: m") for c in got)


def test_graph_never_asks_for_more_than_four_at_once():
    assert pull_all(Mailbox(1), workers=50) and GraphMailProvider(Mailbox(1), lambda: "t", workers=50)._workers == 4


def test_a_failed_download_still_fails_the_page():
    with pytest.raises(AppErrorException):
        pull_all(Mailbox(8, fail="m5"))


def test_workers_1_is_the_old_one_at_a_time():
    box = Mailbox(6)
    assert len(pull_all(box, workers=1)) == 6 and box.most == 1


def _page(n: int, done: bool = False) -> MailPage:
    return MailPage([], f"tok-{n}", done=done)


def test_read_ahead_fetches_the_next_page_while_this_one_is_applied():
    fetched: list[int] = []
    second_read = threading.Event()

    def pages():
        for n in range(3):
            fetched.append(n)
            if n == 1:
                second_read.set()
            yield _page(n, done=n == 2)

    ahead = _ReadAhead(pages())
    try:
        first = next(ahead)
        assert first.next_token == "tok-0"
        assert second_read.wait(5)                         # page 1 came while page 0 was in hand
        assert [p.next_token for p in ahead] == ["tok-1", "tok-2"]
    finally:
        ahead.close()


def test_read_ahead_raises_the_providers_failure_after_the_pages_before_it():
    def pages():
        yield _page(0)
        raise RuntimeError("the server went away")

    ahead = _ReadAhead(pages())
    try:
        assert next(ahead).next_token == "tok-0"
        with pytest.raises(RuntimeError, match="went away"):
            next(ahead)
    finally:
        ahead.close()


def test_closing_the_read_ahead_stops_and_closes_the_provider_pages():
    closed = threading.Event()

    def pages():
        try:
            n = 0
            while True:
                yield _page(n)
                n += 1
        finally:
            closed.set()

    ahead = _ReadAhead(pages())
    next(ahead)
    ahead.close()
    assert closed.wait(5)


def test_microsoft_mail_goes_over_the_kept_connection_transport():
    sent = object()

    class Service:
        def contact_transport(self, account):
            return sent

        def transport_for(self, account):                  # the one-connection-a-request transport
            raise AssertionError("mail must use the kept connections")

        def token_source(self, account):
            return lambda: "tok"

    class Account:
        provider, name = "microsoft", "Work"

        def option(self, _k):
            return None

    p = build_mail_provider(Service(), Account())
    assert isinstance(p, GraphMailProvider) and p._transport is sent
