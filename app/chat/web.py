"""Optional web augmentation for chat - off by default, local first, private by construction.

Layer: L8b - no Qt, importable without a network. Nothing else in Leasha uses the web;
this module is reached only when the person switched "use the web" on in Chat.

**The contract, in the order it matters**

1. *Nothing runs unless `WebSettings.enabled`.* `run_web` refuses otherwise, so a caller
   that forgets the toggle cannot leak by accident.
2. *Local first.* The engine runs local retrieval before asking for the web; this module
   only ever supplies extra passages (`web_context`). It never sees the local answer.
3. *What leaves the machine is one short keyword query.* `compose_query` asks the LOCAL
   model to turn the question into <= 10 keywords, and `sanitize_query` then strips
   anything that could be the person's data: paths, drive letters, e-mail addresses,
   URLs, file names, long digit runs, and every string in `avoid` (the caller passes the
   names and distinctive passages of the local sources it retrieved). `run_web`
   sanitises again, so a raw question handed to it cannot go out either. If nothing safe
   is left the query is "" and the caller must not search.
4. *Requests carry nothing else.* Only a polite `User-Agent` (and, for Brave, the key
   the person typed, in the header of that one call). No cookies, no referrer, no
   account, no identifiers. Result pages are fetched text-only: scripts are never run,
   at most 3 redirects are followed, only `text/html` / `text/plain` is read, at most
   1 MB is kept, and a page is refused when it points at a loopback, private or
   link-local address, a non-http(s) scheme or a port other than 80/443 (the SSRF guard;
   see `check_url`). The person's own SearXNG address is exempt - it is *meant* to be
   on the LAN - but is only ever asked for search results.
5. *It never raises.* Every failure becomes `WebResult.error`, one plain sentence.

**Providers** (`search(query, *, limit, timeout) -> list[WebHit]`)

* `wikipedia` - keyless, `en.wikipedia.org/w/api.php?action=query&list=search`.
* `duckduckgo` - keyless HTML endpoint, kept for machines it accepts.
* `searxng` - the person's own instance, `/search?q=...&format=json`.
* `brave` - Brave Search API with the person's own key (`X-Subscription-Token`).
* `auto` - the person's own SearXNG / Brave first when configured, then the keyless
  ones in order; falls to the next on an error **or** an empty answer.

**Live probe, 2026-09-20, from the development machine (Windows 11, home broadband,
plain `requests`, harmless query "what is a PST file"):**

    Wikipedia Action API (list=search)     200 in 0.9 s  - WORKS. JSON, `snippet` is HTML.
    Wikipedia Action API (prop=extracts)   200 in 0.9-1.1 s - WORKS, plain-text article.
        (`exchars` is capped at 1,200 by the API, so the full text is asked for and
        clipped here.) Latency VARIES: 0.7-1.1 s in the first probes, then 4-22 s per call
        a few minutes later on the same machine (a shared, busy network) - so a
        slow answer is a normal case, and the default 8 s timeout will sometimes fire.
    Wikipedia REST search / page summary   200 in 0.7-0.9 s - work, not used.
    html.duckduckgo.com/html (GET and POST) 202 in 0.4 s - NOT USABLE: an anti-bot
        "anomaly" challenge page, with a polite or a browser User-Agent. Not bypassed.
    lite.duckduckgo.com/lite, api.duckduckgo.com   202 in 0.4 s - same challenge.
    mojeek.com/search  - a JavaScript captcha.  search.marginalia.nu - a "wait a moment"
        bot page.  search.brave.com/search (HTML) - 429.  startpage.com - 200 but a
        JavaScript application, no results in the HTML.

So the working keyless provider on that machine is Wikipedia, which answers
encyclopedic questions and little else. That is the honest reach of "no key, no
account": for general web questions the person supplies a SearXNG address or a Brave
key. The DuckDuckGo parser below was written from the endpoint's documented markup
(`a.result__a`, `a.result__snippet`, `uddg=` redirect links) and could not be checked
against a live result page from that machine; a challenge page is detected and reported
so `auto` moves on. SearXNG and Brave were built from their documented JSON shapes and
not called (no instance, and no key may be entered here).

`transport` is the one seam: `(method, url, *, params=None, headers=None, timeout=...)`
returning an object with `.status_code`, `.text`, `.headers`, `.content`. Every network
call goes through it, so tests need no network and can record every outgoing request.
The default transport imports `requests` lazily and never follows a redirect itself
(`_send` does, with the guard applied to every hop).
"""

from __future__ import annotations

import importlib
import ipaddress
import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

__all__ = [
    "WebHit", "WebPage", "WebResult", "WebSettings", "USER_AGENT",
    "check_url", "sanitize_query", "compose_query", "fetch_pages", "run_web", "web_context",
]

USER_AGENT = "Leasha-Chat/1 (local desktop app; contact: none)"
MAX_BYTES = 1_000_000            #: most of any one response that is read
MAX_REDIRECTS = 3
MAX_QUERY_WORDS = 10
MAX_QUERY_CHARS = 100
PROVIDERS = ("auto", "duckduckgo", "wikipedia", "searxng", "brave")

MSG_OFF = "Web search is switched off."
MSG_TIMEOUT = "The web search did not answer in time."
MSG_UNREACHABLE = "The web search could not be reached."
MSG_BUSY = "The web search is busy right now - try again in a minute."
MSG_CHALLENGE = "The web search asked for proof that a person was searching, so it was skipped."
MSG_NOTHING = "The web search found nothing for that."
MSG_UNREADABLE = "The web search answered in a way that could not be read."
MSG_NOTHING_SAFE = "There was nothing safe to search for."
MSG_BAD_KEY = "The web search did not accept the Brave Search key."
MSG_NO_KEY = "No Brave Search key has been entered."
MSG_NO_SEARX = "No SearXNG address has been entered."
MSG_SEARX_JSON = "That SearXNG server does not give answers in JSON, which Leasha needs."
#: 2026-10-04, code review: the question was stopped while the web was being searched.
MSG_STOPPED = "The web search was stopped."


# --------------------------------------------------------------------------- the values

@dataclass(frozen=True)
class WebHit:
    """One search result: what the provider said, not yet the page."""
    title: str
    url: str
    snippet: str = ""
    site: str = ""


@dataclass(frozen=True)
class WebPage:
    """The readable text of one result page."""
    url: str
    title: str
    text: str


@dataclass(frozen=True)
class WebResult:
    query: str
    provider: str = ""
    hits: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.hits or self.pages)

    def sources(self) -> list:
        """`[(n, WebPage | WebHit), ...]`, n from 1: fetched pages first, then the hits
        that have a snippet and no page. Items with no text are left out, so the
        numbers are exactly those `web_context` prints."""
        out: list = []
        seen: set[str] = set()
        for page in self.pages:
            if page.text.strip():
                out.append(page)
                seen.add(_same(page.url))
        for hit in self.hits:
            if hit.snippet.strip() and _same(hit.url) not in seen:
                out.append(hit)
        return list(enumerate(out, start=1))


def _same(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _pick(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, Mapping):
            if name in source and source[name] is not None:
                return source[name]
        elif source is not None and getattr(source, name, None) is not None:
            return getattr(source, name)
    return default


def _flag(value: Any, default: bool) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default if value is None else bool(value)


@dataclass(frozen=True)
class WebSettings:
    enabled: bool = False
    provider: str = "auto"
    searxng_url: str = ""
    brave_key: str = field(default="", repr=False)      # never printed or logged
    ask_first: bool = True
    show_query: bool = True
    timeout_s: float = 8.0
    max_results: int = 5
    max_pages: int = 3
    max_page_chars: int = 6000

    @classmethod
    def from_settings(cls, settings: Any = None, **overrides: Any) -> "WebSettings":
        """From a `Settings`, any object with the same attribute names, or a mapping;
        every missing value takes the default (off, ask first, show the query)."""
        provider = str(_pick(settings, "chat_web_provider", "CHAT_WEB_PROVIDER",
                             default="auto")).strip().lower()
        values: dict[str, Any] = {
            "enabled": _flag(_pick(settings, "chat_web_enabled", "CHAT_WEB_ENABLED"), False),
            "provider": provider if provider in PROVIDERS else "auto",
            "searxng_url": str(_pick(settings, "chat_web_searxng_url", "CHAT_WEB_SEARXNG_URL",
                                     default="")).strip(),
            "brave_key": str(_pick(settings, "chat_web_brave_key", "CHAT_WEB_BRAVE_KEY",
                                   default="")).strip(),
            "ask_first": _flag(_pick(settings, "chat_web_ask_first", "CHAT_WEB_ASK_FIRST"), True),
            "show_query": _flag(_pick(settings, "chat_web_show_query", "CHAT_WEB_SHOW_QUERY"), True),
        }
        values.update(overrides)
        return cls(**values)


# --------------------------------------------------------------------------- the network seam

class _WebError(Exception):
    """A failure with its plain-words sentence; never escapes this module."""


class _Reply:
    def __init__(self, status_code: int, headers: Any, content: bytes, text: str) -> None:
        self.status_code, self.headers, self.content, self.text = status_code, headers, content, text


def _decode(content: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I) \
        or re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", content[:2048], re.I)
    charset = match.group(1) if match else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii", "ignore")
    try:
        return content.decode(charset, "replace")
    except LookupError:
        return content.decode("utf-8", "replace")


def _default_transport(method: str, url: str, *, params: Any = None, headers: Any = None,
                       timeout: float = 8.0) -> Any:
    """`requests`, imported here so the module is importable offline; no redirects (the
    caller follows them, guarded), the body read in pieces and stopped at `MAX_BYTES`.

    2026-10-04, code review: `timeout` was only `requests`' per-read limit, so a server
    that sent a few bytes every few seconds held the answer for as long as it liked.
    The body now has a wall clock too - `timeout` from the first byte of the reply -
    and a reply still arriving then is cut off: the connection is closed under the
    read that is waiting (`_close_late`), which is what ends a read that never returns."""
    requests = importlib.import_module("requests")
    started = time.monotonic()
    try:
        response = requests.request(method, url, params=params, headers=headers,
                                    timeout=timeout, allow_redirects=False, stream=True)
    except requests.Timeout as exc:
        raise _WebError(MSG_TIMEOUT) from exc
    except requests.RequestException as exc:
        raise _WebError(MSG_UNREACHABLE) from exc
    late = {"cut": False}

    def _close_late() -> None:
        late["cut"] = True
        # Closing the response alone does not end a read waiting in another thread
        # (the socket outlives its file object); shutting the socket down does.
        sock = _socket_of(response)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            response.close()
        except Exception:                                  # noqa: BLE001
            pass

    watchdog = threading.Timer(max(0.05, started + float(timeout) - time.monotonic()), _close_late)
    watchdog.daemon = True
    watchdog.start()
    try:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(16384):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BYTES:
                break
            if late["cut"] or time.monotonic() - started > float(timeout):
                raise _WebError(MSG_TIMEOUT)
        if late["cut"]:
            raise _WebError(MSG_TIMEOUT)
        content = b"".join(chunks)[:MAX_BYTES]
    except _WebError:
        raise
    except Exception as exc:                               # noqa: BLE001 - a closed read too
        if late["cut"] or isinstance(exc, requests.Timeout):
            raise _WebError(MSG_TIMEOUT) from exc
        raise _WebError(MSG_UNREACHABLE) from exc
    finally:
        watchdog.cancel()
        response.close()
    return _Reply(response.status_code, response.headers, content,
                  _decode(content, str(response.headers.get("content-type", ""))))


def _socket_of(response: Any) -> Any:
    """The socket under a streamed `requests` reply, or `None` (the connection pool's
    connection, else http.client's file object)."""
    raw = getattr(response, "raw", None)
    for path in (("_connection", "sock"), ("_fp", "fp", "raw", "_sock")):
        node = raw
        for name in path:
            node = getattr(node, name, None)
            if node is None:
                break
        if node is not None and hasattr(node, "shutdown"):
            return node
    return None


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None) or {}
    try:
        for key, value in dict(headers).items():
            if str(key).lower() == name:
                return str(value)
    except Exception:                                    # noqa: BLE001
        pass
    return ""


def _classify(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return MSG_TIMEOUT
    return MSG_UNREACHABLE


def _urls() -> Any:
    """URL helpers (`urlparse`, `urljoin`, `unquote`) - `requests` re-exports them."""
    return importlib.import_module("requests.compat")


def _body(response: Any) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)) and content:
        return _decode(bytes(content[:MAX_BYTES]), _header(response, "content-type"))
    return str(getattr(response, "text", "") or "")[:MAX_BYTES]


# --------------------------------------------------------------------------- SSRF guard

_HOST_ONLY_NUMERIC = re.compile(r"(?:0x[0-9a-f]+|\d+)", re.I)
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")


def check_url(url: str, *, resolve: bool = False) -> str:
    """"" when `url` may be fetched, otherwise the reason it may not.

    http/https only, port 80/443 only, no loopback / private / link-local / reserved
    address, whether written as a literal or (with `resolve`) reached by name. Only
    the *result pages* pass through here - never the provider or the person's SearXNG.
    """
    try:
        parts = _urls().urlparse(str(url or "").strip())
        host = (parts.hostname or "").strip(".").lower()
        port = parts.port
    except ValueError:
        return "not a web address"
    if parts.scheme not in ("http", "https"):
        return "only http and https pages are read"
    if not host:
        return "no host"
    if port not in (None, 80, 443):
        return "only the standard web ports are used"
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return "a local address"
    try:
        literals = [ipaddress.ip_address(host)]
    except ValueError:
        literals = []
        if _HOST_ONLY_NUMERIC.fullmatch(host.rsplit(".", 1)[-1]):
            return "an address written in an unusual way"      # 2130706433, 0x7f.1
        if "." not in host:
            return "a local address"                           # a bare machine name
        if resolve:
            try:
                literals = [ipaddress.ip_address(info[4][0].split("%")[0])
                            for info in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)]
            except (OSError, ValueError):
                return "the address could not be looked up"
    for ip in literals:
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            return "a private or local address"
    return ""


def _send(transport: Callable, method: str, url: str, *, params: Any = None,
          headers: Optional[dict] = None, timeout: float = 8.0, guard: bool = False,
          resolve: bool = False, ends: Optional[float] = None) -> Any:
    """One request, following at most `MAX_REDIRECTS` redirects, the guard on every hop.

    2026-10-04, code review: every hop had the whole `timeout`, so four redirects were
    four times it. The redirects now share it, and `ends` (a `time.monotonic()`) can
    hold the request to less - the page's or the whole search's time left."""
    sent = {"User-Agent": USER_AGENT, **(headers or {})}
    limit = time.monotonic() + float(timeout)
    ends = limit if ends is None else min(ends, limit)
    for _hop in range(MAX_REDIRECTS + 1):
        reason = check_url(url, resolve=resolve) if guard else (
            "" if str(url).lower().startswith(("http://", "https://")) else "not a web address")
        if reason:
            raise _WebError(f"That address was not opened ({reason}).")
        left = ends - time.monotonic()
        if left <= 0:
            raise _WebError(MSG_TIMEOUT)
        try:
            response = transport(method, url, params=params, headers=sent, timeout=left)
        except _WebError:
            raise
        except Exception as exc:                          # noqa: BLE001 - never escape
            raise _WebError(_classify(exc)) from exc
        if getattr(response, "status_code", 0) in (301, 302, 303, 307, 308):
            where = _header(response, "location")
            if not where:
                raise _WebError(MSG_UNREADABLE)
            url, params, method = _urls().urljoin(url, where), None, "GET"
            continue
        return response
    raise _WebError(MSG_UNREACHABLE)


def _check(response: Any) -> None:
    code = int(getattr(response, "status_code", 0) or 0)
    if 200 <= code < 300:
        return
    raise _WebError(MSG_BUSY if code in (429, 503) else MSG_UNREACHABLE)


def _json(response: Any) -> Any:
    try:
        data = json.loads(_body(response))
    except ValueError as exc:
        raise _WebError(MSG_UNREADABLE) from exc
    if not isinstance(data, dict):
        raise _WebError(MSG_UNREADABLE)
    return data


_TAGS = re.compile(r"<[^>]+>")


def _plain(markup: Any) -> str:
    return " ".join(unescape(_TAGS.sub("", str(markup or ""))).split())


def _site(url: str) -> str:
    try:
        host = (_urls().urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------- providers

class Provider(Protocol):
    name: str

    def search(self, query: str, *, limit: int, timeout: float) -> list[WebHit]: ...


class WikipediaProvider:
    name = "wikipedia"
    api = "https://en.wikipedia.org/w/api.php"

    def __init__(self, transport: Callable) -> None:
        self._t = transport

    def search(self, query: str, *, limit: int, timeout: float) -> list[WebHit]:
        response = _send(self._t, "GET", self.api, timeout=timeout, params={
            "action": "query", "list": "search", "srsearch": query, "srlimit": limit,
            "srprop": "snippet", "format": "json", "formatversion": 2, "utf8": 1})
        _check(response)
        rows = ((_json(response).get("query") or {}).get("search") or [])
        quote = _urls().quote
        return [WebHit(title=str(row.get("title") or ""),
                       url="https://en.wikipedia.org/wiki/" + quote(str(row["title"]).replace(" ", "_"), safe="_()"),
                       snippet=_plain(row.get("snippet")), site="en.wikipedia.org")
                for row in rows if isinstance(row, dict) and row.get("title")][:limit]


class _DdgParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._mode = ""
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag != "a":
            return
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if "result__a" in classes:
            self._mode, self._href, self._buf = "title", a.get("href") or "", []
        elif "result__snippet" in classes:
            self._mode, self._buf = "snippet", []

    def handle_data(self, data: str) -> None:
        if self._mode:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._mode:
            return
        text = " ".join("".join(self._buf).split())
        if self._mode == "title":
            self.rows.append({"title": text, "href": self._href, "snippet": ""})
        elif self.rows and not self.rows[-1]["snippet"]:
            self.rows[-1]["snippet"] = text
        self._mode = ""


class DuckDuckGoProvider:
    name = "duckduckgo"
    endpoint = "https://html.duckduckgo.com/html/"

    def __init__(self, transport: Callable) -> None:
        self._t = transport

    def search(self, query: str, *, limit: int, timeout: float) -> list[WebHit]:
        response = _send(self._t, "GET", self.endpoint, params={"q": query}, timeout=timeout)
        markup = _body(response)
        if response.status_code == 202 or "anomaly" in markup[:20000].lower():
            raise _WebError(MSG_CHALLENGE)
        _check(response)
        parser = _DdgParser()
        try:
            parser.feed(markup)
            parser.close()
        except Exception:                                 # noqa: BLE001 - keep what parsed
            pass
        hits: list[WebHit] = []
        for row in parser.rows:
            href = str(row["href"])
            found = re.search(r"[?&]uddg=([^&]+)", href)
            if found:
                href = _urls().unquote(found.group(1))
            elif href.startswith("//"):
                href = "https:" + href
            if not href.startswith(("http://", "https://")) or _site(href).endswith("duckduckgo.com"):
                continue                                   # an ad or an internal link
            hits.append(WebHit(row["title"], href, row["snippet"], _site(href)))
        return hits[:limit]


class SearxngProvider:
    name = "searxng"

    def __init__(self, transport: Callable, base_url: str) -> None:
        base = base_url.strip().rstrip("/")
        if base and "://" not in base:
            base = "http://" + base                       # "localhost:8888" as typed
        self._t, self._base = transport, base

    def search(self, query: str, *, limit: int, timeout: float) -> list[WebHit]:
        if not self._base:
            raise _WebError(MSG_NO_SEARX)
        response = _send(self._t, "GET", self._base + "/search", timeout=timeout,
                         params={"q": query, "format": "json"})
        if response.status_code in (400, 403, 404):
            raise _WebError(MSG_SEARX_JSON)
        _check(response)
        rows = _json(response).get("results") or []
        return [WebHit(_plain(row.get("title")), str(row["url"]), _plain(row.get("content")),
                       _site(str(row["url"])))
                for row in rows if isinstance(row, dict) and row.get("url")][:limit]


class BraveProvider:
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, transport: Callable, key: str) -> None:
        self._t, self._key = transport, key.strip()

    def search(self, query: str, *, limit: int, timeout: float) -> list[WebHit]:
        if not self._key:
            raise _WebError(MSG_NO_KEY)
        response = _send(self._t, "GET", self.endpoint, timeout=timeout,
                         params={"q": query, "count": limit},
                         headers={"Accept": "application/json", "X-Subscription-Token": self._key})
        if response.status_code in (401, 403, 422):
            raise _WebError(MSG_BAD_KEY)
        _check(response)
        rows = ((_json(response).get("web") or {}).get("results") or [])
        return [WebHit(_plain(row.get("title")), str(row["url"]), _plain(row.get("description")),
                       _site(str(row["url"])))
                for row in rows if isinstance(row, dict) and row.get("url")][:limit]


def _providers(settings: WebSettings, transport: Callable) -> list:
    every = {"wikipedia": lambda: WikipediaProvider(transport),
             "duckduckgo": lambda: DuckDuckGoProvider(transport),
             "searxng": lambda: SearxngProvider(transport, settings.searxng_url),
             "brave": lambda: BraveProvider(transport, settings.brave_key)}
    if settings.provider != "auto":
        return [every[settings.provider]()]
    order = (["brave"] if settings.brave_key else []) + (["searxng"] if settings.searxng_url else []) \
        + ["wikipedia", "duckduckgo"]
    return [every[name]() for name in order]


# --------------------------------------------------------------------------- reading pages

_DROPPED = re.compile(
    r"<!--.*?-->|<(script|style|noscript|nav|footer|aside|svg|template|iframe|select|button)\b.*?</\1\s*>",
    re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


def _clip(text: str, limit: int) -> str:
    """At most `limit` characters, ended at a sentence boundary when there is one."""
    text = str(text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text if limit > 0 else ""
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?][\"')\]]?(?=\s)", cut)]
    if ends and ends[-1] >= limit * 0.4:
        return cut[:ends[-1]].strip()
    return cut[:cut.rfind(" ")].strip() if " " in cut else cut


def _text_of(markup: str) -> tuple[str, str]:
    """`(title, text)` of an HTML page, scripts and page furniture dropped, using the
    same HTML-to-text reader the indexer uses for e-books."""
    from app.extract.ebook import text_from_xhtml
    found = _TITLE.search(markup)
    title = _plain(found.group(1)) if found else ""
    return title, text_from_xhtml(_DROPPED.sub(" ", markup))


def _wikipedia_text(transport: Callable, hit: WebHit, timeout: float, resolve: bool,
                    ends: Optional[float] = None) -> Optional[WebPage]:
    parts = _urls().urlparse(hit.url)
    if not (parts.hostname or "").endswith(".wikipedia.org") or not parts.path.startswith("/wiki/"):
        return None
    response = _send(transport, "GET", f"https://{parts.hostname}/w/api.php", timeout=timeout,
                     guard=True, resolve=resolve, ends=ends, params={
                         "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1,
                         "titles": _urls().unquote(parts.path[len("/wiki/"):]).replace("_", " "),
                         "format": "json", "formatversion": 2})
    _check(response)
    pages = (_json(response).get("query") or {}).get("pages") or []
    text = str(pages[0].get("extract") or "") if pages and isinstance(pages[0], dict) else ""
    return WebPage(hit.url, hit.title, text) if text.strip() else None


def _fetch_one(transport: Callable, hit: WebHit, timeout: float, resolve: bool,
               ends: Optional[float] = None) -> Optional[WebPage]:
    # 2026-10-04, code review: one page, every request for it, within `timeout`.
    page_ends = time.monotonic() + float(timeout)
    ends = page_ends if ends is None else min(ends, page_ends)
    special = _wikipedia_text(transport, hit, timeout, resolve, ends)   # plain text, no furniture
    if special is not None:
        return special
    response = _send(transport, "GET", hit.url, timeout=timeout, guard=True, resolve=resolve,
                     headers={"Accept": "text/html,text/plain;q=0.9"}, ends=ends)
    _check(response)
    kind = _header(response, "content-type").split(";")[0].strip().lower()
    if kind and kind not in _TEXT_TYPES:
        return None
    body = _body(response)
    if kind == "text/plain":
        title, text = hit.title, body
    else:
        title, text = _text_of(body)
    return WebPage(hit.url, title or hit.title, text) if text.strip() else None


def fetch_pages(hits: Sequence[WebHit], *, max_pages: int = 3, timeout: float = 8.0,
                max_chars: int = 6000, transport: Optional[Callable] = None,
                deadline: Optional[float] = None,
                should_stop: Optional[Callable[[], bool]] = None) -> list[WebPage]:
    """Text of the top `max_pages` result pages, each kept to `max_chars`. A page that
    is refused, not text, unreachable or empty is simply left out. Never raises.

    2026-10-04, code review: each page is held to `timeout` in all (redirects
    included), the pages together to `deadline` (a `time.monotonic()`), and
    `should_stop` - the question's Stop - is asked before each page."""
    resolve = transport is None                # a real connection: look the name up first
    send = transport or _default_transport
    pages: list[WebPage] = []
    for hit in list(hits)[:max(0, int(max_pages))]:
        if should_stop is not None and _asks_stop(should_stop):
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            page = _fetch_one(send, hit, timeout, resolve, deadline)
        except Exception:                                  # noqa: BLE001
            page = None
        if page is not None:
            pages.append(WebPage(page.url, page.title[:200], _clip(page.text, max_chars)))
    return pages


# --------------------------------------------------------------------------- the query

_QUOTED_PATH = re.compile(r"[\"'`]\s*(?:[A-Za-z]:[\\/]|\\\\)[^\"'`]*[\"'`]")
_URL = re.compile(r"(?:https?|ftp|file)://\S+|\bwww\.\S+", re.I)
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_FILE_EXT = frozenset("""pdf doc docx xls xlsx xlsm ppt pptx txt csv rtf odt ods odp msg eml pst ost
jpg jpeg png gif bmp tif tiff heic zip rar 7z md json xml html htm log ini bak tmp mp3 mp4 mov wav
epub one pub vsd mdb accdb sql""".split())
_GENERIC_SEGMENTS = frozenset("""users user documents desktop downloads program files windows home
appdata local roaming temp data c d e f my""".split())
_TRIM = " \t.,;:!?()[]{}<>\"'`*_"
_WORD = re.compile(r"[a-z0-9]+")
_REQUEST_WORDS = frozenset("tell show give explain describe please find search look".split())


#: The person's own request to use the web ("search the web too", "google it", "... online"):
#: it is an instruction to Leasha, not something to search for, so it is not sent.
_WEB_REQUEST = re.compile(
    r"\b(?:(?:please\s+)?(?:search|look(?:\s+it|\s+that|\s+this)?\s+up|check|find|browse)"
    r"(?:\s+(?:it|that|this))?\s+(?:on\s+|in\s+)?(?:the\s+)?(?:web|internet|online)(?:\s+(?:for|too|also|as\s+well))?"
    r"|(?:google|bing|duckduckgo)(?:\s+(?:it|that|this|for))?|(?:on|from|using)\s+the\s+(?:web|internet)"
    r"|online\s+search|search\s+online|online(?=\W*$))", re.I)


def strip_web_request(text: str) -> str:
    """`text` without the person's instruction to use the web."""
    return " ".join(_WEB_REQUEST.sub(" ", str(text or "")).split())


def _is_private_token(token: str) -> bool:
    if "\\" in token or re.match(r"^[A-Za-z]:", token) or "@" in token:
        return True
    if token.startswith(("/", "~/", "./", "../")) and len(token) > 1 or token.count("/") >= 2:
        return True
    if "/" in token and "." in token:
        return True                                        # example.com/path
    bare = token.strip(_TRIM)
    if "." in bare and bare.rsplit(".", 1)[-1].lower() in _FILE_EXT and bare.rsplit(".", 1)[0]:
        return True                                        # a file name; ".pst" alone stays
    if sum(ch.isdigit() for ch in token) >= 6:
        return True                                        # an account or phone number
    return len(re.sub(r"\W", "", token)) >= 24             # a key, hash or id


def _avoid_phrases(avoid: Iterable[str]) -> tuple[list[list[str]], list[list[str]], set[str]]:
    """(phrases to remove wherever they appear, long passages for the n-gram rule,
    digit-bearing words to remove anywhere)."""
    phrases: list[list[str]] = []
    passages: list[list[str]] = []
    digits: set[str] = set()

    def add(text: str) -> None:
        words = _WORD.findall(text.lower())
        if words and sum(map(len, words)) >= 3:
            phrases.append(words)

    for item in avoid or ():
        text = str(item or "").strip()
        if not text:
            continue
        add(text)
        stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", text)
        if stem != text:
            add(stem)
        if "@" in text and " " not in text:
            local, _, domain = text.partition("@")
            if len(local) >= 4:
                add(local)
            add(domain)
        if "\\" in text or text.count("/") >= 2:
            for segment in re.split(r"[\\/]+", text):
                segment = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", segment.strip(": "))
                if len(segment) >= 3 and segment.lower() not in _GENERIC_SEGMENTS:
                    add(segment)
        words = _WORD.findall(text.lower())
        if len(words) >= 5:
            passages.append(words)
            spoken = text.split()                 # names and places in a passage are its identity
            for at, word in enumerate(spoken):
                bare = word.strip(_TRIM)
                if at and bare[:1].isupper() and bare.isalpha() and len(bare) >= 4                         and not spoken[at - 1].endswith((".", "!", "?")):
                    add(bare)
        digits |= {w for w in words if len(w) >= 3 and any(c.isdigit() for c in w)}
    return phrases, passages, digits


def _drop_avoid(tokens: list[str], avoid: Iterable[str]) -> list[str]:
    phrases, passages, digits = _avoid_phrases(avoid)
    if not (phrases or passages or digits):
        return tokens
    flat = [(word, at) for at, tok in enumerate(tokens) for word in _WORD.findall(tok.lower())]
    words = [w for w, _ in flat]
    dropped: set[int] = set()
    for at, word in enumerate(words):
        if word in digits:
            dropped.add(flat[at][1])
    for phrase in phrases:
        size = len(phrase)
        for at in range(len(words) - size + 1):
            if words[at:at + size] == phrase:
                dropped.update(flat[i][1] for i in range(at, at + size))
    for passage in passages:
        grams = {tuple(passage[i:i + 4]) for i in range(len(passage) - 3)}
        for at in range(len(words) - 3):
            if tuple(words[at:at + 4]) in grams:
                dropped.update(flat[i][1] for i in range(at, at + 4))
    return [tok for at, tok in enumerate(tokens) if at not in dropped]


def _scrub(query: str, avoid: Iterable[str]) -> list[str]:
    """The query's words with everything private removed, in order, not yet capped."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(query or ""))
    for pattern in (_QUOTED_PATH, _URL, _EMAIL):
        text = pattern.sub(" ", text)
    tokens = _drop_avoid([t for t in text.split() if not _is_private_token(t)], avoid)
    tokens = [t.strip(_TRIM) for t in tokens]
    return [t for t in tokens if re.search(r"[A-Za-z0-9]", t)]


def _finish(tokens: list[str]) -> str:
    """At most 10 words / 100 characters; "" unless a real content word is left."""
    from app.chat.text import content_tokens
    out = ""
    for token in tokens[:MAX_QUERY_WORDS]:
        if len(out) + len(token) + (1 if out else 0) > MAX_QUERY_CHARS:
            break
        out = f"{out} {token}".strip()
    return out if content_tokens(out) else ""


def sanitize_query(query: str, *, avoid: Iterable[str] = ()) -> str:
    """The query with anything that could be the person's data taken out, capped at
    10 words / 100 characters; "" when nothing safe (no real content word) is left."""
    return _finish(_scrub(query, avoid))


_PREFIX = re.compile(r"^\s*(?:search\s+query|keywords?|query|search)\s*[:\-]\s*", re.I)
_CHATTY = re.compile(r"^\s*(?:sure|here|okay|ok|certainly|i\b|the\b.*\bquery\b)", re.I)


def _from_model(text: str) -> str:
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    line = _PREFIX.sub("", line).strip(" \t\"'`*")
    if not line or _CHATTY.match(line) or len(line.split()) > 16 or line.endswith("?"):
        return ""
    return line


def _keywords(question: str) -> str:
    """The deterministic fallback: the question's own words minus filler, in order."""
    from app.chat.text import STOPWORDS
    filler = STOPWORDS | _REQUEST_WORDS
    seen: set[str] = set()
    out: list[str] = []
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-.]*[A-Za-z0-9]|[A-Za-z0-9]", str(question or "")):
        low = word.lower()
        if low in filler or low in seen:
            continue
        seen.add(low)
        out.append(word)
    return " ".join(out)


def compose_query(question: str, llm: Any, *, avoid: Iterable[str] = (),
                  should_stop: Optional[Callable[[], bool]] = None) -> str:
    """The web query for `question`: written by the LOCAL model from the question only,
    then sanitised against `avoid`; a deterministic keyword query when the model is
    unavailable or talks nonsense. "" means nothing safe - do not search.
    `should_stop` (2026-10-04, code review) is the question's Stop, for the model."""
    avoid = list(avoid or ())
    asked = strip_web_request(" ".join(str(question or "").split()))[:500] or         " ".join(str(question or "").split())[:500]
    prompt = ("Write a short web search query (at most 8 keywords) for the question below.\n"
              "Use only words from the question. Do not add names, file names, folder paths, "
              "e-mail addresses or numbers that are not in it. Reply with the keywords only, "
              "on one line.\n\n"
              f"Question: {asked}\nKeywords:")
    candidate = ""
    if llm is not None:
        from app.chat.llm import stop_kwargs

        try:
            reply = llm.generate(prompt, temperature=0.0, max_tokens=40, timeout=30.0, stop=["\n"],
                                 **stop_kwargs(llm, should_stop))
            candidate = sanitize_query(_from_model(getattr(reply, "text", reply)), avoid=avoid)
        except Exception:                                  # noqa: BLE001 - any failure means fall back
            candidate = ""
    if candidate:
        return candidate
    # Scrub the question first: filler removal would break the consecutive-word matches
    # the passage rule depends on. Then keywords, then the same clean-up again.
    return sanitize_query(_keywords(" ".join(_scrub(asked, avoid))), avoid=avoid)


# --------------------------------------------------------------------------- the whole thing

def run_web(query: str, settings: Optional[WebSettings] = None, *,
            transport: Optional[Callable] = None, avoid: Iterable[str] = (),
            should_stop: Optional[Callable[[], bool]] = None) -> WebResult:
    """Search, then read the top pages. Never raises; a failure is `WebResult.error`.

    The query is sanitised again here, so a caller handing over a raw question cannot
    leak a path. The whole call is held to about three times `timeout_s`.
    Dated note, 2026-10-04, code review: that sentence was not true - only each
    socket read had a limit, so a slow server, its redirects and three pages could
    hold an answer for minutes. It is now (`_send`, `_default_transport`), and
    `should_stop` - the question's Stop - is asked between providers and pages.
    """
    settings = settings or WebSettings()
    safe = ""
    try:
        if not settings.enabled:
            return WebResult(query="", error=MSG_OFF)
        safe = sanitize_query(query, avoid=avoid)
        if not safe:
            return WebResult(query="", error=MSG_NOTHING_SAFE)
        return _run(safe, settings, transport, should_stop)
    except Exception:                                      # noqa: BLE001 - the contract
        return WebResult(query=safe, error=MSG_UNREACHABLE)


def _asks_stop(should_stop: Optional[Callable[[], bool]]) -> bool:
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception:                                      # noqa: BLE001
        return False


def _run(query: str, settings: WebSettings, transport: Optional[Callable],
         should_stop: Optional[Callable[[], bool]] = None) -> WebResult:
    send = transport or _default_transport
    deadline = time.monotonic() + max(1.0, settings.timeout_s) * 3
    left = lambda: max(0.1, min(settings.timeout_s, deadline - time.monotonic()))   # noqa: E731
    first_error = ""
    for provider in _providers(settings, send):
        if _asks_stop(should_stop):
            return WebResult(query, settings.provider, error=MSG_STOPPED)
        if time.monotonic() > deadline:
            first_error = first_error or MSG_TIMEOUT
            break
        try:
            hits = provider.search(query, limit=max(1, settings.max_results), timeout=left())
        except _WebError as exc:
            first_error = first_error or str(exc)
            continue
        except Exception as exc:                           # noqa: BLE001
            first_error = first_error or _classify(exc)
            continue
        if not hits:
            continue
        pages = [] if time.monotonic() > deadline else fetch_pages(
            hits, max_pages=settings.max_pages, timeout=left(), max_chars=settings.max_page_chars,
            transport=transport, deadline=deadline, should_stop=should_stop)
        return WebResult(query, provider.name, list(hits), pages)
    return WebResult(query, settings.provider, error=first_error or MSG_NOTHING)


def web_context(result: WebResult, *, max_chars: int = 1500) -> list[tuple[int, str, str, str]]:
    """`[(n, title, url, text), ...]`, n from 1 (the caller renumbers), each text clipped
    at a sentence boundary to `max_chars`. Pages first, then snippet-only hits."""
    out: list[tuple[int, str, str, str]] = []
    for n, source in result.sources():
        text = _clip(source.text if isinstance(source, WebPage) else source.snippet, max_chars)
        if text:
            out.append((n, source.title or _site(source.url) or source.url, source.url, text))
    return out
