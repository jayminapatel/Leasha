"""The only code in Leasha that talks to an account server.

Layer: L7

Order 1k, items 3f, 0b and 10e. Three small pieces, each testable alone:

* `AllowedHosts` - the list of hosts an account may reach. Anything else is
  refused with an `AppError` *before* a socket is opened, so a bug (or a
  malicious redirect, or a poisoned `nextLink` in a response) cannot send a
  person's token to a stranger. `tests/unit/test_accounts_network_guard.py`
  pins `PROVIDER_HOSTS`.
* `UrllibTransport` - the real `provider.Transport`, standard library only.
  TLS verification is on and cannot be turned off here. It never raises for a
  status code; only "no answer at all" is an error.
* `RetryingTransport` - wraps any transport and waits as long as the provider
  asks on 429 and 503. The server's `Retry-After` always beats our own
  backoff, because it is the only party that knows its own limit.
* `KeepAliveTransport` - the same contract as `UrllibTransport` over one kept
  `http.client` connection per host (order 1t 5a): a contact sync made a new
  TCP and TLS connection for every request, two or three per contact.
* `Meter` - counts what a transport did (requests by method, bytes, seconds,
  retries, seconds slept), so a run can say where its time went (order 1t 1a).

Plus the pure `backoff_minutes` of item 10e, used by the sync scheduler after a
failed run (not per request - that is `RetryingTransport`'s seconds-scale
backoff).

**Redirects are not followed.** A 3xx comes back to the caller as a response
with its `location` header. Following one silently would re-send the
`Authorization` header to wherever the server pointed, and would make the
allow-list a check on the first hop only. A connector that must follow a
redirect (iCloud CardDAV moves a person to a numbered host) reads `location`
and calls `request` again, so every hop is checked.
"""

from __future__ import annotations

import email.utils
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlsplit

from app.accounts.provider import HttpResponse, Transport
from app.accounts.secrets import redact
from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "PROVIDER_HOSTS", "ENRICHMENT_HOSTS", "AllowedHosts", "UrllibTransport", "RetryingTransport",
    "KeepAliveTransport", "Meter", "backoff_minutes", "parse_retry_after", "retry_after_of",
]

_log = logger.bind(component="accounts.http")

# --------------------------------------------------------------------------
# The allow-list
# --------------------------------------------------------------------------

#: Fixed hosts per provider (lower case). Generic IMAP and CardDAV servers are
#: not here: the person types the server in, and `AllowedHosts.allow_server`
#: registers exactly that host at runtime. Apple's numbered hosts are matched by
#: the suffix and pattern rules below, not listed.
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "google": ("accounts.google.com", "oauth2.googleapis.com", "people.googleapis.com",
               "gmail.googleapis.com", "www.googleapis.com"),
    "microsoft": ("login.microsoftonline.com", "graph.microsoft.com"),
    "apple": ("contacts.icloud.com", "imap.mail.me.com"),
}

#: Fixed hosts per internet enrichment source (order 1k section 7f, 7h). Kept apart
#: from `PROVIDER_HOSTS` so an account cannot reach them by accident: an
#: `AllowedHosts` includes them only when it is built with the source's name
#: (`AllowedHosts(("gravatar",))`). The company-logo source (7g) has no fixed host;
#: it registers the company's own domain with `allow_server` for one call.
ENRICHMENT_HOSTS: dict[str, tuple[str, ...]] = {
    "gravatar": ("www.gravatar.com", "gravatar.com"),
    "wikidata": ("www.wikidata.org", "query.wikidata.org"),
}

#: iCloud moves a CardDAV account to a numbered host after sign-in
#: (p07-contacts.icloud.com), and the number is not ours to list. A suffix and a
#: pattern, both anchored to icloud.com so `evil-icloud.com` cannot match.
_APPLE_SUFFIXES = (".contacts.icloud.com",)
_APPLE_PATTERNS = (re.compile(r"^p\d{1,3}-contacts\.icloud\.com$"),)

_HOST_OK = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


#: What a connection that WAS made and then lost looks like: worth one more try. (Not "Name or service not known",
#: not "refused", not "no route": those stay as they are, quickly - `test_unreachable_is_not_retried`.)
_TRANSIENT = re.compile(r"forcibly closed|timed out|reset by peer|10054|10053|connection aborted|remote end closed|"
                        r"eof occurred|incomplete read", re.IGNORECASE)


def _norm_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def normalise_server(text: str) -> tuple[str, Optional[int], str]:
    """`(host, port, path)` from whatever was typed for a server: `dav.example.com`, `https://dav.example.com`,
    `dav.example.com:8443/dav.php/`. The host alone is what the allow-list and the connection need; a port or a path
    that came with it is kept for the caller, so a pasted address is not a "not a server name" error (the owner,
    2026-10-12, adding dav.privateemail.com)."""
    raw = (text or "").strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    try:
        port = parts.port
    except ValueError:
        port = None
    path = parts.path if parts.path not in ("", "/") else ""
    return _norm_host(parts.hostname or ""), port, path


#: Who we are to a server. Python's own default ("Python-urllib/3.x") is refused outright by sites behind a bot
#: filter - dav.privateemail.com answers it with Cloudflare's "error code: 1010" before CardDAV is even reached -
#: while every ordinary client name is let through.
def _user_agent() -> str:
    try:
        from app.core.version import __version__

        return f"Leasha/{__version__} (contacts and mail sync)"
    except Exception:                                     # noqa: BLE001 - a name; never stops a request
        return "Leasha (contacts and mail sync)"


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        # "localhost" is deliberately not accepted: a hosts file can point it
        # anywhere, while a loopback IP literal cannot.
        return False


class AllowedHosts:
    """Which hosts an account request may reach.

    Fixed provider hosts, plus any exact server host registered at runtime from
    what the person typed into an IMAP or CardDAV account, plus loopback (the
    sign-in redirect and the tests' fake servers) which is always allowed.
    """

    def __init__(self, providers: Optional[tuple[str, ...]] = None) -> None:
        names = tuple(providers) if providers is not None else tuple(PROVIDER_HOSTS)
        self._fixed = {h for p in names
                       for h in (*PROVIDER_HOSTS.get(p, ()), *ENRICHMENT_HOSTS.get(p, ()))}
        self._apple = "apple" in names
        self._servers: set[str] = set()
        self._lock = threading.Lock()

    # -- runtime registration (generic IMAP / CardDAV) --------------------

    def allow_server(self, host: str) -> str:
        """Allow exactly `host` (no wildcard, no port, no scheme). Returns it normalised."""
        h = _norm_host(host)
        if not h or not _HOST_OK.match(h) or "/" in h or ":" in h:
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_UNREACHABLE", "accounts.http", account=host or "the server",
                reason="that is not a server name", details=f"cannot allow host {host!r}"))
        with self._lock:
            self._servers.add(h)
        return h

    def forget_server(self, host: str) -> None:
        with self._lock:
            self._servers.discard(_norm_host(host))

    # -- the check --------------------------------------------------------

    def is_allowed(self, host: str) -> bool:
        h = _norm_host(host)
        if not h:
            return False
        if _is_loopback(h):
            return True
        if h in self._fixed:
            return True
        if self._apple and (h.endswith(_APPLE_SUFFIXES) or any(p.match(h) for p in _APPLE_PATTERNS)):
            return True
        with self._lock:
            return h in self._servers

    def check(self, url: str) -> str:
        """Raise unless `url` may be requested. Returns the host.

        Non-loopback requests must be https: a CardDAV password sent over
        plain http is a credential on the wire, and "TLS verification on"
        means little if http is accepted.
        """
        parts = urlsplit(url)
        host = _norm_host(parts.hostname or "")
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            self._refuse(host or url, f"a {scheme or 'missing'} address is not allowed")
        if not self.is_allowed(host):
            self._refuse(host or url, "this server is not one Leasha is allowed to contact")
        if scheme != "https" and not _is_loopback(host):
            self._refuse(host, "only secure (https) connections are allowed")
        return host

    @staticmethod
    def _refuse(host: str, why: str) -> None:
        raise AppErrorException(make_error(
            "ERR_ACCOUNT_UNREACHABLE", "accounts.http", account=host or "the server",
            reason=f"refused: {why}",
            details=f"host {host!r} is not on the account allow-list ({why})"))

    def describe(self) -> dict[str, list[str]]:
        """What is allowed right now, for the guard test and `accounts status`."""
        with self._lock:
            return {
                "fixed": sorted(self._fixed),
                "apple_suffixes": list(_APPLE_SUFFIXES) if self._apple else [],
                "servers": sorted(self._servers),
                "loopback": ["127.0.0.0/8", "::1"],
            }


# --------------------------------------------------------------------------
# The real transport
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return the 3xx to the caller instead of following it (see module docstring)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: ANN001
        return None


class UrllibTransport:
    """`provider.Transport` over `urllib`. TLS verified; allow-list enforced."""

    def __init__(self, allowed: AllowedHosts, *, account: str = "the account",
                 context: Optional[ssl.SSLContext] = None) -> None:
        self._allowed = allowed
        self._account = account
        # create_default_context verifies certificates and host names. It is
        # the only context ever built here; there is no switch to turn it off.
        self._ctx = context or ssl.create_default_context()
        self._remote = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=self._ctx))
        # Loopback must never go through a proxy from the environment: the
        # sign-in redirect would be sent to the proxy and fail.
        self._local = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 30.0) -> HttpResponse:
        host = self._allowed.check(url)
        opener = self._local if _is_loopback(host) else self._remote
        sent = dict(headers or {})
        if not any(k.lower() == "user-agent" for k in sent):
            sent["User-Agent"] = _user_agent()
        req = urllib.request.Request(url, data=body, headers=sent, method=method.upper())
        try:
            with opener.open(req, timeout=timeout) as resp:
                return self._wrap(resp.status, resp.headers, resp.read())
        except urllib.error.HTTPError as exc:
            # A status code is an answer, not a failure (the Transport contract).
            try:
                data = exc.read()
            except Exception:                           # noqa: BLE001
                data = b""
            return self._wrap(exc.code, exc.headers, data)
        except AppErrorException:
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, ConnectionError,
                OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_UNREACHABLE", "accounts.http", account=self._account,
                reason=redact(str(reason))[:120] or type(exc).__name__,
                details=redact(f"{method} {host}: {type(exc).__name__}: {exc}"))) from None
        except Exception as exc:                        # noqa: BLE001 - http.client errors, etc.
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_UNREACHABLE", "accounts.http", account=self._account,
                reason="the server sent something unreadable",
                details=redact(f"{method} {host}: {type(exc).__name__}: {exc}"))) from None

    @staticmethod
    def _wrap(status: int, headers, data: bytes) -> HttpResponse:   # noqa: ANN001
        return HttpResponse(status=int(status),
                            headers={k.lower(): v for k, v in (headers.items() if headers else [])},
                            body=data or b"")


class Meter:
    """What a transport did, counted (order 1t 1a). Thread-safe; `snapshot` is a plain dict for a run's plan."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[str, int] = {}
        self.bytes_out = self.bytes_in = 0
        self.seconds = self.slept_s = 0.0
        self.retries = self.connections = 0

    def count(self, method: str, sent: int, got: int, seconds: float) -> None:
        with self._lock:
            m = method.upper()
            self.requests[m] = self.requests.get(m, 0) + 1
            self.bytes_out += sent
            self.bytes_in += got
            self.seconds += seconds

    def retried(self, slept: float) -> None:
        with self._lock:
            self.retries += 1
            self.slept_s += max(0.0, slept)

    def connected(self) -> None:
        with self._lock:
            self.connections += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"requests": dict(self.requests), "total": sum(self.requests.values()),
                    "bytes_out": self.bytes_out, "bytes_in": self.bytes_in, "seconds": round(self.seconds, 2),
                    "retries": self.retries, "slept_s": round(self.slept_s, 2), "connections": self.connections}


class KeepAliveTransport:
    """`provider.Transport` over kept `http.client` connections: one per host, per thread. TLS verified, the
    allow-list checked on every request, redirects returned and not followed - the contract of
    `UrllibTransport`, which it hands a request to for a loopback host and when a proxy is set for the host.

    **A dropped connection.** A connection idle for longer than `idle_s` is closed before it is used again
    (servers drop idle connections after a minute or so), and before a write after `write_idle_s`, so a write
    rarely meets a connection the server has already let go. When a kept connection fails while the request
    is being sent, it is sent once more on a new one - a write too, since the server never had it. When it
    fails after the request went out, only a read or a delete is sent again (either is harmless twice); a
    create or an update may already have been applied, so that is an error and the engine's next sync
    re-reads before it writes."""

    def __init__(self, allowed: AllowedHosts, *, account: str = "the account",
                 context: Optional[ssl.SSLContext] = None, idle_s: float = 30.0, write_idle_s: float = 10.0,
                 meter: Optional[Meter] = None, clock: Callable[[], float] = time.monotonic,
                 connect: Optional[Callable[..., "http.client.HTTPConnection"]] = None) -> None:
        self._allowed = allowed
        self._account = account
        self._ctx = context or ssl.create_default_context()
        self._idle = idle_s
        self._write_idle = write_idle_s
        self._clock = clock
        self.meter = meter
        self._connect = connect or (lambda host, port, timeout: http.client.HTTPSConnection(
            host, port, timeout=timeout, context=self._ctx))
        self._local = threading.local()
        self._fallback = UrllibTransport(allowed, account=account, context=self._ctx)

    def _pool(self) -> dict:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        return pool

    def _drop(self, key: tuple) -> None:
        held = self._pool().pop(key, None)
        if held is not None:
            try:
                held[0].close()
            except Exception:                                 # noqa: BLE001 - closing is best effort
                pass

    def _held(self, host: str, port: int, timeout: float, fresh: bool, idle: float) -> list:
        key = (host, port)
        held = self._pool().get(key)
        if held is not None and (fresh or self._clock() - held[1] > idle):
            self._drop(key)
            held = None
        if held is None:
            held = self._pool()[key] = [self._connect(host, port, timeout), self._clock(), False]
            if self.meter is not None:
                self.meter.connected()
        conn = held[0]
        conn.timeout = timeout
        if getattr(conn, "sock", None) is not None:
            conn.sock.settimeout(timeout)
        return held

    def close(self) -> None:
        for key in list(self._pool()):
            self._drop(key)

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 30.0) -> HttpResponse:
        host = self._allowed.check(url)
        parts = urlsplit(url)
        started = time.monotonic()
        if _is_loopback(host) or parts.scheme != "https" or _proxied(host):
            resp = self._fallback.request(method, url, headers=headers, body=body, timeout=timeout)
            if self.meter is not None:
                self.meter.count(method, len(body or b""), len(resp.body), time.monotonic() - started)
            return resp
        port = parts.port or 443
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        sent = dict(headers or {})
        if not any(k.lower() == "user-agent" for k in sent):
            sent["User-Agent"] = _user_agent()
        repeatable = method.upper() in _REPEATABLE
        idle = self._idle if repeatable else min(self._idle, self._write_idle)
        for attempt in (1, 2):
            held = self._held(host, port, timeout, fresh=attempt == 2, idle=idle)
            conn, reused = held[0], held[2]
            sent_it = False
            try:
                conn.request(method.upper(), path, body=body, headers=sent)
                sent_it = True
                raw = conn.getresponse()
                data = raw.read()
                held[1], held[2] = self._clock(), True
                if raw.will_close:
                    self._drop((host, port))
                resp = HttpResponse(status=int(raw.status), headers={k.lower(): v for k, v in raw.getheaders()},
                                    body=data or b"")
                if self.meter is not None:
                    self.meter.count(method, len(body or b""), len(resp.body), time.monotonic() - started)
                return resp
            except AppErrorException:
                raise
            except (OSError, http.client.HTTPException, ValueError) as exc:
                self._drop((host, port))
                if attempt == 1 and reused and isinstance(exc, _GONE_WHILE_IDLE) and (repeatable or not sent_it):
                    _log.info("{}: a kept connection to {} had closed ({}); sending again on a new one",
                              self._account, host, type(exc).__name__)
                    continue
                reason = getattr(exc, "reason", None) or exc
                raise AppErrorException(make_error(
                    "ERR_ACCOUNT_UNREACHABLE", "accounts.http", account=self._account,
                    reason=redact(str(reason))[:120] or type(exc).__name__,
                    details=redact(f"{method} {host}: {type(exc).__name__}: {exc}"))) from None
        raise AssertionError("unreachable")                          # pragma: no cover


#: Requests that do no harm sent twice: a read, and a delete (the second finds nothing to delete).
_REPEATABLE = ("GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT", "DELETE")

#: How a connection the server closed while it sat idle shows itself on the next request.
_GONE_WHILE_IDLE = (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                    http.client.CannotSendRequest, http.client.BadStatusLine)


def _proxied(host: str) -> bool:
    """A proxy set in the environment for https and not bypassed for `host`: `http.client` would ignore it, so
    such a request goes through urllib, which honours it."""
    try:
        proxies = urllib.request.getproxies()
        return bool(proxies.get("https")) and not urllib.request.proxy_bypass(host)
    except Exception:                                         # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# Retry-After and backoff
# --------------------------------------------------------------------------

def parse_retry_after(value: str, *, now_epoch: float) -> Optional[float]:
    """Seconds to wait from a `Retry-After` header: delta-seconds or an HTTP date.

    Returns None when the header is absent or unreadable, and never a negative
    number (a date already in the past means "now").
    """
    v = (value or "").strip()
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now_epoch)


def retry_after_of(exc: AppErrorException) -> Optional[float]:
    """The `Retry-After` seconds carried by an `ERR_PROVIDER_LIMIT`, if the server gave one."""
    value = exc.error.context.get("retry_after_s")
    return float(value) if isinstance(value, (int, float)) else None


class RetryingTransport:
    """Waits and retries on 429 and 503; honest `ERR_PROVIDER_LIMIT` when it gives up.

    `sleep` and `clock` (epoch seconds, for HTTP-date `Retry-After`) are
    injected so tests run instantly.

    A `Retry-After` longer than `max_wait_s` is not slept through: a worker
    thread parked for an hour is a sync nobody can cancel. It raises
    `ERR_PROVIDER_LIMIT` at once with `retry_after_s` in the context, and the
    scheduler (`policy.next_retry_at`) waits that long instead.
    """

    RETRY_STATUSES = (429, 503)
    #: A dropped connection or a slow answer is tried again for these - they change nothing at the server, so a
    #: repeat is safe - and never for a write, which might have been applied before the connection went.
    SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT")
    NETWORK_TRIES = 3

    def __init__(self, inner: Transport, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, max_attempts: int = 5,
                 base_delay_s: float = 1.0, max_backoff_s: float = 60.0,
                 max_wait_s: float = 300.0, account: str = "the account",
                 meter: Optional[Meter] = None) -> None:
        self._inner = inner
        self.meter = meter if meter is not None else getattr(inner, "meter", None)
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max(1, int(max_attempts))
        self._base = base_delay_s
        self._max_backoff = max_backoff_s
        self._max_wait = max_wait_s
        self._account = account

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 30.0) -> HttpResponse:
        last: Optional[HttpResponse] = None
        for attempt in range(self._max_attempts):
            resp = self._send_once(method, url, headers, body, timeout)
            if resp.status not in self.RETRY_STATUSES:
                return resp
            last = resp
            told = parse_retry_after(resp.header("retry-after"), now_epoch=self._clock())
            if told is not None and told > self._max_wait:
                self._give_up(resp, told)
            if attempt == self._max_attempts - 1:
                break
            # The server's number wins even when it is shorter than ours: it
            # knows when its window reopens, we are only guessing.
            delay = told if told is not None else min(self._base * (2 ** attempt), self._max_backoff)
            _log.info("{}: HTTP {} on {} - waiting {:.1f} s before try {} (Retry-After {})", self._account,
                      resp.status, method.upper(), delay, attempt + 2, resp.header("retry-after") or "not given")
            if self.meter is not None:
                self.meter.retried(delay)
            self._sleep(delay)
        assert last is not None
        self._give_up(last, parse_retry_after(last.header("retry-after"), now_epoch=self._clock()))
        raise AssertionError("unreachable")                          # pragma: no cover

    def _send_once(self, method: str, url: str, headers: Optional[dict[str, str]], body: Optional[bytes],
                   timeout: float) -> HttpResponse:
        """One request; a lost connection or a timeout on a read is tried again (1 s, 2 s) before it is an error.
        Microsoft's first fetch of a large mailbox was ended by "connection forcibly closed" and "read operation timed
        out" - one lost connection among thousands of requests stopped the whole run (2026-10-12)."""
        tries = self.NETWORK_TRIES if method.upper() in self.SAFE_METHODS else 1
        for attempt in range(tries):
            try:
                return self._inner.request(method, url, headers=headers, body=body, timeout=timeout)
            except AppErrorException as exc:
                if (exc.error.code != "ERR_ACCOUNT_UNREACHABLE" or attempt == tries - 1
                        or not _TRANSIENT.search(str(exc.error.message))):
                    raise                    # offline, a name that does not resolve, a refused port: not repeated
                wait = min(self._base * (2 ** attempt), self._max_backoff)
                _log.info("{}: {} lost its connection ({}) - trying again in {:.0f} s", self._account,
                          method.upper(), str(exc.error.message)[:80], wait)
                if self.meter is not None:
                    self.meter.retried(wait)
                self._sleep(wait)
        raise AssertionError("unreachable")                          # pragma: no cover

    def close(self) -> None:
        """Close the kept connections of the transport inside, if it keeps any."""
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def _give_up(self, resp: HttpResponse, retry_after_s: Optional[float]) -> None:
        kind = "too many requests" if resp.status == 429 else "temporarily unavailable"
        raise AppErrorException(make_error(
            "ERR_PROVIDER_LIMIT", "accounts.http", account=self._account,
            reason=f"{kind}, HTTP {resp.status}",
            details=f"gave up after {self._max_attempts} attempts; Retry-After={retry_after_s}",
            retry_after_s=retry_after_s, status=resp.status))


#: Where the sync backoff stops when the account has no timed schedule (a manual
#: account is only ever retried by the person pressing Sync now, so this only
#: bounds a caller that asks anyway).
_DEFAULT_BACKOFF_CAP_MIN = 60


def backoff_minutes(failures: int, interval_minutes: int) -> int:
    """Minutes to wait after `failures` failed syncs in a row: 1, 2, 4, 8 ... (10e).

    Capped at the schedule's own interval - waiting longer than the person asked
    to wait between syncs would be slower than doing nothing - and 0 after a
    success (`failures` back at 0). A provider's `Retry-After` overrides this;
    see `policy.next_retry_at`.
    """
    if failures <= 0:
        return 0
    cap = interval_minutes if interval_minutes and interval_minutes > 0 else _DEFAULT_BACKOFF_CAP_MIN
    return int(min(2 ** min(failures - 1, 30), cap))
