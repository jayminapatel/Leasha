"""Order 1k, items 3f, 0b, 10e: the allow-list, the transport, Retry-After and backoff.

Layer: L7

No real network: the real `UrllibTransport` is only pointed at a server on
127.0.0.1 started by the test, and every test runs with non-loopback sockets
forbidden. Retry timing uses a fake clock and sleep, so the file is instant.
"""

from __future__ import annotations

import http.server
import socket
import threading
from email.utils import format_datetime
from datetime import datetime, timezone

import pytest

from app.accounts.http import (
    ENRICHMENT_HOSTS, PROVIDER_HOSTS, AllowedHosts, RetryingTransport, UrllibTransport, backoff_minutes,
    parse_retry_after, retry_after_of,
)
from app.accounts.provider import HttpResponse
from app.core.errors import AppErrorException


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host in ("127.0.0.1", "::1"), f"non-loopback connect to {host!r}"
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    real_cc = socket.create_connection

    def cc(address, *a, **k):
        assert address[0] in ("127.0.0.1", "::1"), f"non-loopback connect to {address[0]!r}"
        return real_cc(address, *a, **k)

    monkeypatch.setattr(socket, "create_connection", cc)


# ---- the allow-list ---------------------------------------------------------

def test_the_pinned_provider_hosts():
    """If this changes, a human decided the app may talk to a new host."""
    assert PROVIDER_HOSTS == {
        "google": ("accounts.google.com", "oauth2.googleapis.com", "people.googleapis.com",
                   "gmail.googleapis.com", "www.googleapis.com"),
        "microsoft": ("login.microsoftonline.com", "graph.microsoft.com"),
        "apple": ("contacts.icloud.com", "imap.mail.me.com"),
    }


def test_the_pinned_enrichment_hosts():
    """Internet enrichment (7f, 7h) may reach exactly these, and only when asked for by name."""
    assert ENRICHMENT_HOSTS == {
        "gravatar": ("www.gravatar.com", "gravatar.com"),
        "wikidata": ("www.wikidata.org", "query.wikidata.org"),
    }
    assert not set(ENRICHMENT_HOSTS) & set(PROVIDER_HOSTS)


def test_enrichment_hosts_are_refused_unless_the_source_is_named():
    plain = AllowedHosts()                      # every account provider, no enrichment source
    for hosts in ENRICHMENT_HOSTS.values():
        for h in hosts:
            with pytest.raises(AppErrorException):
                plain.check(f"https://{h}/x")
    gravatar = AllowedHosts(("gravatar",))
    for h in ENRICHMENT_HOSTS["gravatar"]:
        assert gravatar.check(f"https://{h}/x") == h
    for h in ENRICHMENT_HOSTS["wikidata"] + PROVIDER_HOSTS["google"]:
        with pytest.raises(AppErrorException):
            gravatar.check(f"https://{h}/x")
    for bad in ("https://gravatar.com.evil.com/", "https://evil-gravatar.com/", "http://www.gravatar.com/x"):
        with pytest.raises(AppErrorException):
            gravatar.check(bad)


def test_listed_hosts_allowed_and_unlisted_refused_with_an_apperror():
    allowed = AllowedHosts()
    for hosts in PROVIDER_HOSTS.values():
        for h in hosts:
            assert allowed.check(f"https://{h}/x") == h
    for bad in ("https://evil.example.com/", "https://googleapis.com/", "https://accounts.google.com.evil.com/",
                "https://notgraph.microsoft.com.evil.net/"):
        with pytest.raises(AppErrorException) as err:
            allowed.check(bad)
        e = err.value.error
        assert e.code == "ERR_ACCOUNT_UNREACHABLE" and "allow-list" in (e.details or "")
        assert e.suggestion


def test_provider_scoping():
    google_only = AllowedHosts(("google",))
    assert google_only.is_allowed("gmail.googleapis.com")
    assert not google_only.is_allowed("graph.microsoft.com")
    assert not google_only.is_allowed("p07-contacts.icloud.com")


def test_apple_suffix_rule_is_anchored():
    a = AllowedHosts()
    for ok in ("p07-contacts.icloud.com", "p123-contacts.icloud.com", "contacts.icloud.com",
               "x.contacts.icloud.com", "imap.mail.me.com"):
        assert a.is_allowed(ok), ok
    for bad in ("evilcontacts.icloud.com", "p07-contacts.icloud.com.evil.com", "icloud.com",
                "evil-icloud.com", "pxx-contacts.icloud.com", "smtp.mail.me.com"):
        assert not a.is_allowed(bad), bad


def test_generic_server_is_allowed_only_for_the_exact_typed_host():
    a = AllowedHosts()
    assert not a.is_allowed("imap.fastmail.com")
    assert a.allow_server(" IMAP.Fastmail.com ") == "imap.fastmail.com"
    assert a.is_allowed("imap.fastmail.com")
    assert not a.is_allowed("evil.imap.fastmail.com") and not a.is_allowed("fastmail.com")
    a.forget_server("imap.fastmail.com")
    assert not a.is_allowed("imap.fastmail.com")
    for junk in ("", "https://x.com", "a b", "x.com/path", "x.com:993"):
        with pytest.raises(AppErrorException):
            a.allow_server(junk)


def test_loopback_always_allowed_but_localhost_name_is_not():
    a = AllowedHosts(())
    assert a.check("http://127.0.0.1:5000/") == "127.0.0.1"
    assert a.is_allowed("::1") and a.is_allowed("127.0.0.5")
    assert not a.is_allowed("localhost")


def test_plain_http_is_refused_off_loopback_and_odd_schemes_everywhere():
    a = AllowedHosts()
    with pytest.raises(AppErrorException):
        a.check("http://graph.microsoft.com/v1.0/me")
    with pytest.raises(AppErrorException):
        a.check("ftp://graph.microsoft.com/")
    with pytest.raises(AppErrorException):
        a.check("file:///etc/passwd")


def test_describe_lists_what_is_allowed():
    a = AllowedHosts()
    a.allow_server("dav.example.org")
    d = a.describe()
    assert "graph.microsoft.com" in d["fixed"] and d["servers"] == ["dav.example.org"]


# ---- the real transport, against a loopback server --------------------------

class _H(http.server.BaseHTTPRequestHandler):
    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://evil.example.com/steal")
            self.end_headers()
            return
        status = 404 if self.path == "/missing" else 429 if self.path == "/limit" else 200
        payload = (self.command + ":" + self.path + ":").encode() + body
        self.send_response(status)
        self.send_header("X-Custom-Thing", "yes")
        self.send_header("Retry-After", "7")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = _go

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_transport_returns_status_codes_instead_of_raising(server):
    t = UrllibTransport(AllowedHosts(()))
    ok = t.request("POST", server + "/a", body=b"hello", headers={"Content-Type": "text/plain"})
    assert ok.status == 200 and ok.text() == "POST:/a:hello"
    assert ok.header("X-Custom-Thing") == "yes" and "x-custom-thing" in ok.headers   # lower-cased names
    assert t.request("GET", server + "/missing").status == 404
    limited = t.request("GET", server + "/limit")
    assert limited.status == 429 and limited.header("retry-after") == "7"


def test_transport_does_not_follow_redirects(server):
    r = UrllibTransport(AllowedHosts(())).request("GET", server + "/redirect")
    assert r.status == 302 and r.header("location") == "https://evil.example.com/steal"


def test_transport_refuses_an_unlisted_host_before_any_socket(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    with pytest.raises(AppErrorException) as err:
        UrllibTransport(AllowedHosts()).request("GET", "https://attacker.example.net/token")
    assert err.value.error.code == "ERR_ACCOUNT_UNREACHABLE"


def test_no_answer_is_account_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                    # nothing listens here now
    with pytest.raises(AppErrorException) as err:
        UrllibTransport(AllowedHosts(()), account="Home").request("GET", f"http://127.0.0.1:{port}/", timeout=2)
    assert err.value.error.code == "ERR_ACCOUNT_UNREACHABLE"
    assert "Home" in err.value.error.message


def test_transport_verifies_tls():
    t = UrllibTransport(AllowedHosts())
    import ssl
    assert t._ctx.verify_mode == ssl.CERT_REQUIRED and t._ctx.check_hostname


# ---- Retry-After and backoff ------------------------------------------------

class _Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls += 1
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _r(status, retry_after=None):
    return HttpResponse(status, {"retry-after": retry_after} if retry_after is not None else {}, b"x")


def _retrying(inner, **kw):
    sleeps = []
    clock = kw.pop("clock", lambda: 1_000_000.0)
    return RetryingTransport(inner, sleep=sleeps.append, clock=clock, **kw), sleeps


def test_success_and_other_statuses_pass_through_without_sleeping():
    for status in (200, 204, 400, 401, 404, 500):
        inner = _Scripted(_r(status))
        t, sleeps = _retrying(inner)
        assert t.request("GET", "https://graph.microsoft.com/x").status == status
        assert inner.calls == 1 and sleeps == []


def test_exponential_backoff_when_the_server_gives_no_retry_after():
    inner = _Scripted(_r(429), _r(503), _r(429), _r(200))
    t, sleeps = _retrying(inner, base_delay_s=1.0)
    assert t.request("GET", "https://graph.microsoft.com/x").status == 200
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_after_seconds_always_wins_even_when_shorter_than_our_backoff():
    inner = _Scripted(_r(429, "0"), _r(429, "3"), _r(429, "1"), _r(200))
    t, sleeps = _retrying(inner, base_delay_s=10.0)
    assert t.request("GET", "https://graph.microsoft.com/x").status == 200
    assert sleeps == [0.0, 3.0, 1.0]                 # not 10, 20, 40


def test_retry_after_http_date_uses_the_injected_clock():
    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    when = datetime(2026, 10, 11, 12, 0, 30, tzinfo=timezone.utc)
    inner = _Scripted(_r(503, format_datetime(when, usegmt=True)), _r(200))
    t, sleeps = _retrying(inner, clock=lambda: now.timestamp())
    assert t.request("GET", "https://graph.microsoft.com/x").status == 200
    assert sleeps == [30.0]
    assert parse_retry_after("garbage", now_epoch=0) is None and parse_retry_after("", now_epoch=0) is None
    assert parse_retry_after(format_datetime(now, usegmt=True), now_epoch=now.timestamp() + 99) == 0.0


def test_exhausted_attempts_raise_an_honest_provider_limit():
    inner = _Scripted(_r(429, "2"))
    t, sleeps = _retrying(inner, max_attempts=3, account="Work")
    with pytest.raises(AppErrorException) as err:
        t.request("GET", "https://graph.microsoft.com/x")
    e = err.value.error
    assert e.code == "ERR_PROVIDER_LIMIT" and "Work" in e.message and "429" in e.message
    assert inner.calls == 3 and sleeps == [2.0, 2.0]           # no sleep after the last attempt
    assert retry_after_of(err.value) == 2.0


def test_a_retry_after_longer_than_we_will_wait_is_handed_back_not_slept():
    inner = _Scripted(_r(429, "3600"), _r(200))
    t, sleeps = _retrying(inner, max_wait_s=300)
    with pytest.raises(AppErrorException) as err:
        t.request("GET", "https://graph.microsoft.com/x")
    assert err.value.error.code == "ERR_PROVIDER_LIMIT"
    assert sleeps == [] and inner.calls == 1
    assert retry_after_of(err.value) == 3600.0                 # the scheduler waits this long instead


def test_exhausted_without_retry_after_has_none_for_the_scheduler():
    t, _ = _retrying(_Scripted(_r(503)), max_attempts=2)
    with pytest.raises(AppErrorException) as err:
        t.request("GET", "https://graph.microsoft.com/x")
    assert retry_after_of(err.value) is None


def test_unreachable_is_not_retried():
    class Down:
        calls = 0

        def request(self, *a, **k):
            Down.calls += 1
            from app.core.errors import make_error
            raise AppErrorException(make_error("ERR_ACCOUNT_UNREACHABLE", "t", account="A", reason="down"))

    t, sleeps = _retrying(Down())
    with pytest.raises(AppErrorException):
        t.request("GET", "https://graph.microsoft.com/x")
    assert Down.calls == 1 and sleeps == []


def test_backoff_minutes_doubles_caps_at_the_interval_and_resets():
    assert [backoff_minutes(n, 60) for n in range(0, 9)] == [0, 1, 2, 4, 8, 16, 32, 60, 60]
    assert [backoff_minutes(n, 5) for n in range(1, 6)] == [1, 2, 4, 5, 5]
    assert backoff_minutes(0, 60) == 0                         # reset by a success
    assert backoff_minutes(10_000, 30) == 30                   # no overflow
    assert backoff_minutes(3, 0) == 4                          # manual: bounded by a default cap, not unbounded
