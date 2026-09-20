"""Chat's optional web augmentation (`app/chat/web.py`): parsing, fallback, the SSRF guard,
the fetch caps and - above all - the privacy contract. Everything here is offline: a fake
transport (or a fake `requests`) stands in for the network and records what would leave.

Layer: L8b. Fixtures: `tests/fixtures/web_pages.py`.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from app.chat import web
from app.chat.testing import FakeLLM
from tests.fixtures import web_pages as fx

ROOT = Path(__file__).resolve().parents[2]

# planted strings that must never leave the machine
FILE_NAME = "ZEBRA-4471-INVOICE.pdf"
FOLDER = "C:\\Users\\alice\\Secret\\"
EMAIL = "alice@example.com"
PASSAGE = "The consignment for Halvorsen Marine was delayed at the Rotterdam depot until March."
PLANTED = [FILE_NAME, FOLDER, EMAIL, PASSAGE]
FRAGMENTS = ["ZEBRA", "4471", "alice", "Secret", "Halvorsen", "Rotterdam", "example.com", "C:\\"]


# --------------------------------------------------------------------------- doubles

class Resp:
    def __init__(self, text="", status=200, ctype="text/html; charset=utf-8", headers=None,
                 content=None):
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": ctype, **(headers or {})} if ctype else dict(headers or {})
        self.content = content if content is not None else text.encode("utf-8")


class Net:
    """A recording transport. `routes` is `[(substring of the url, Resp | callable | Exception)]`."""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, params=None, headers=None, timeout=8.0):
        self.calls.append({"method": method, "url": url, "params": params,
                           "headers": dict(headers or {}), "timeout": timeout})
        for key, answer in self.routes:
            if key in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer(url, params) if callable(answer) else answer
        raise ConnectionError(f"no route for {url}")

    def urls(self):
        return [c["url"] for c in self.calls]

    def everything_sent(self) -> str:
        return json.dumps(self.calls, default=str)


def on(**kw) -> web.WebSettings:
    kw.setdefault("enabled", True)
    return web.WebSettings(**kw)


def hit(url, title="t", snippet="s"):
    return web.WebHit(title, url, snippet, web._site(url))


# --------------------------------------------------------------------------- settings

def test_settings_default_to_off_ask_first_show_query():
    s = web.WebSettings()
    assert (s.enabled, s.provider, s.ask_first, s.show_query) == (False, "auto", True, True)
    assert (s.timeout_s, s.max_results, s.max_pages, s.max_page_chars) == (8.0, 5, 3, 6000)


def test_from_settings_reads_a_mapping_an_object_and_nothing():
    m = web.WebSettings.from_settings({
        "chat_web_enabled": True, "chat_web_provider": "SearXNG", "chat_web_searxng_url": " http://h:1 ",
        "chat_web_brave_key": "k", "chat_web_ask_first": False, "chat_web_show_query": "false"})
    assert (m.enabled, m.provider, m.searxng_url, m.brave_key, m.ask_first, m.show_query) == \
        (True, "searxng", "http://h:1", "k", False, False)

    class Obj:
        chat_web_enabled = True
        chat_web_provider = "brave"
    o = web.WebSettings.from_settings(Obj())
    assert o.enabled and o.provider == "brave" and o.ask_first

    for nothing in (None, {}, object()):
        assert web.WebSettings.from_settings(nothing) == web.WebSettings()
    assert web.WebSettings.from_settings({"chat_web_provider": "bing"}).provider == "auto"


def test_the_brave_key_is_not_in_the_repr():
    assert "SECRET-KEY" not in repr(web.WebSettings(brave_key="SECRET-KEY"))


def test_run_web_refuses_when_switched_off():
    net = Net(("", Resp(fx.WIKI_SEARCH)))
    result = web.run_web("pst file", web.WebSettings(), transport=net)
    assert not result.ok and result.error == web.MSG_OFF and net.calls == []


# --------------------------------------------------------------------------- providers

def test_wikipedia_parses_and_strips_markup():
    net = Net(("w/api.php", Resp(fx.WIKI_SEARCH, ctype="application/json")))
    hits = web.WikipediaProvider(net).search("pst file", limit=2, timeout=5)
    assert [h.title for h in hits] == ["Personal Storage Table", "Microsoft Outlook"]
    assert hits[0].url == "https://en.wikipedia.org/wiki/Personal_Storage_Table"
    assert "<" not in hits[0].snippet and "Outlook stores these items" in hits[0].snippet
    assert '"File"' in hits[1].snippet and hits[0].site == "en.wikipedia.org"
    assert net.calls[0]["params"]["srsearch"] == "pst file"


def test_duckduckgo_parses_unwraps_redirects_and_skips_ads():
    net = Net(("duckduckgo.com/html", Resp(fx.DDG_RESULTS)))
    hits = web.DuckDuckGoProvider(net).search("pst file", limit=5, timeout=5)
    assert [h.url for h in hits] == ["https://pages.example.org/pst-guide", "https://wiki.example.net/pst"]
    assert hits[0].title == "A PST file guide" and hits[0].snippet == "What a PST file is & what it holds."


@pytest.mark.parametrize("answer", [Resp(fx.DDG_CHALLENGE, status=202), Resp(fx.DDG_CHALLENGE)])
def test_duckduckgo_challenge_page_is_an_error_not_a_result(answer):
    result = web.run_web("pst file", on(provider="duckduckgo"), transport=Net(("duckduckgo", answer)))
    assert result.error == web.MSG_CHALLENGE and not result.ok


def test_searxng_uses_the_json_format_at_the_given_address():
    net = Net(("localhost:8888/search", Resp(fx.SEARX_JSON, ctype="application/json")))
    hits = web.SearxngProvider(net, "localhost:8888/").search("pst file", limit=5, timeout=5)
    assert net.calls[0]["url"] == "http://localhost:8888/search"
    assert net.calls[0]["params"] == {"q": "pst file", "format": "json"}
    assert hits[0].title == "PST file explained" and hits[0].site == "example.org"
    assert len(hits) == 2


def test_searxng_with_json_switched_off_says_so():
    net = Net(("/search", Resp("forbidden", status=403)))
    result = web.run_web("pst file", on(provider="searxng", searxng_url="http://h"), transport=net)
    assert result.error == web.MSG_SEARX_JSON


def test_brave_sends_the_key_only_as_a_header_to_its_own_host():
    net = Net(("api.search.brave.com", Resp(fx.BRAVE_JSON, ctype="application/json")))
    hits = web.BraveProvider(net, "MY-KEY").search("pst file", limit=5, timeout=5)
    call = net.calls[0]
    assert call["headers"]["X-Subscription-Token"] == "MY-KEY"
    assert "MY-KEY" not in json.dumps({k: v for k, v in call.items() if k != "headers"})
    assert hits[0].title == "What is a PST file" and hits[0].snippet == "A PST file stores Outlook data."
    assert call["params"]["count"] == 5


def test_brave_without_a_key_or_with_a_refused_one():
    assert web.run_web("pst file", on(provider="brave"), transport=Net()).error == web.MSG_NO_KEY
    net = Net(("brave", Resp("no", status=401)))
    assert web.run_web("pst file", on(provider="brave", brave_key="x"), transport=net).error == web.MSG_BAD_KEY


def test_searxng_without_an_address():
    assert web.run_web("pst file", on(provider="searxng"), transport=Net()).error == web.MSG_NO_SEARX


# --------------------------------------------------------------------------- auto

def _hosts(net):
    return [c["url"].split("/")[2] for c in net.calls]


def test_auto_prefers_the_persons_own_providers_then_keyless_in_order():
    net = Net(("api.search.brave.com", Resp("x", status=500)), ("searx.me", Resp("x", status=500)),
              ("wikipedia.org/w/api.php", Resp(fx.WIKI_EMPTY)),
              ("duckduckgo", Resp(fx.DDG_RESULTS)), ("pages.example.org", Resp(fx.ARTICLE_HTML)),
              ("wiki.example.net", Resp(fx.ARTICLE_HTML)))
    result = web.run_web("pst file", on(brave_key="k", searxng_url="https://searx.me"), transport=net)
    assert _hosts(net)[:4] == ["api.search.brave.com", "searx.me", "en.wikipedia.org", "html.duckduckgo.com"]
    assert result.provider == "duckduckgo" and result.ok and result.pages


def test_auto_stops_at_the_first_provider_that_answers():
    net = Net(("wikipedia.org/w/api.php", Resp(fx.WIKI_SEARCH)),
              ("prop", Resp("{}")))
    web.run_web("pst file", on(max_pages=0), transport=net)
    assert _hosts(net) == ["en.wikipedia.org"]


def test_auto_reports_the_first_error_when_every_provider_fails():
    net = Net(("wikipedia", TimeoutError()), ("duckduckgo", Resp(fx.DDG_CHALLENGE, status=202)))
    result = web.run_web("pst file", on(), transport=net)
    assert result.error == web.MSG_TIMEOUT and not result.ok and result.hits == []


def test_every_provider_empty_is_one_plain_sentence():
    net = Net(("wikipedia", Resp(fx.WIKI_EMPTY)), ("duckduckgo", Resp("<html></html>")))
    assert web.run_web("pst file", on(), transport=net).error == web.MSG_NOTHING


# --------------------------------------------------------------------------- failures

@pytest.mark.parametrize("boom,message", [
    (TimeoutError("slow"), web.MSG_TIMEOUT),
    (socket.timeout(), web.MSG_TIMEOUT),
    (ConnectionError("down"), web.MSG_UNREACHABLE),
    (RuntimeError("anything at all"), web.MSG_UNREACHABLE),
])
def test_transport_failures_become_a_plain_sentence(boom, message):
    result = web.run_web("pst file", on(provider="wikipedia"), transport=Net(("wikipedia", boom)))
    assert result.error == message and not result.ok


@pytest.mark.parametrize("answer,message", [
    (Resp("busy", status=429), web.MSG_BUSY), (Resp("down", status=503), web.MSG_BUSY),
    (Resp("oops", status=500), web.MSG_UNREACHABLE),
    (Resp("not json"), web.MSG_UNREADABLE), (Resp("[1, 2]"), web.MSG_UNREADABLE),
])
def test_http_errors_and_junk_answers(answer, message):
    result = web.run_web("pst file", on(provider="wikipedia"), transport=Net(("wikipedia", answer)))
    assert result.error == message


def test_run_web_never_raises_even_if_the_transport_returns_junk():
    for junk in (None, object(), 42):
        result = web.run_web("pst file", on(provider="wikipedia"), transport=lambda *a, **k: junk)
        assert isinstance(result, web.WebResult) and not result.ok and result.error


# --------------------------------------------------------------------------- SSRF guard

@pytest.mark.parametrize("url", [
    "http://localhost/x", "http://LOCALHOST./x", "http://127.0.0.1/x", "http://127.1.2.3/",
    "http://10.0.0.5/", "http://172.16.4.4/", "http://192.168.1.1/", "http://169.254.169.254/latest",
    "http://[::1]/", "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
    "http://2130706433/", "http://0x7f.0.0.1/", "http://printer.local/", "http://intranet/",
    "http://user@127.0.0.1/", "file:///etc/passwd", "ftp://example.org/x", "javascript:alert(1)",
    "https://example.org:8080/x", "http://example.org:22/", "", "not a url",
])
def test_check_url_refuses(url):
    assert web.check_url(url), url


@pytest.mark.parametrize("url", ["http://example.org/", "https://www.example.org:443/a?b=1",
                                 "http://example.org:80/", "https://93.184.216.34/"])
def test_check_url_allows_ordinary_pages(url):
    assert web.check_url(url) == ""


def test_check_url_with_resolution_refuses_a_name_that_points_inside(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **k: [(2, 1, 6, "", ("192.168.0.9", port))])
    assert web.check_url("https://looks-public.example.org/", resolve=True) == "a private or local address"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("nx")))
    assert web.check_url("https://nowhere.example.org/", resolve=True)


def test_fetch_never_contacts_a_refused_address():
    net = Net(("", Resp(fx.ARTICLE_HTML)))
    bad = [hit(u) for u in ("http://localhost/a", "http://127.0.0.1/a", "http://10.1.1.1/a",
                            "http://192.168.0.2/a", "http://169.254.1.1/a", "file:///c:/x",
                            "ftp://example.org/a", "http://example.org:8080/a")]
    assert web.fetch_pages(bad, max_pages=20, transport=net) == [] and net.calls == []


def test_a_redirect_into_the_private_network_is_not_followed():
    net = Net(("example.org/start", Resp("", status=302, headers={"Location": "http://127.0.0.1/admin"})),
              ("127.0.0.1", Resp("PRIVATE")))
    assert web.fetch_pages([hit("http://example.org/start")], transport=net) == []
    assert all("127.0.0.1" not in u for u in net.urls())


def test_at_most_three_redirects_are_followed():
    def bounce(url, params):
        n = int(url.rsplit("/", 1)[-1])
        return Resp("", status=302, headers={"Location": f"http://example.org/{n + 1}"})
    net = Net(("example.org/", bounce))
    assert web.fetch_pages([hit("http://example.org/0")], transport=net) == []
    assert len(net.calls) == web.MAX_REDIRECTS + 1

    ok = Net(("example.org/0", Resp("", status=301, headers={"location": "/1"})),
             ("example.org/1", Resp("<p>Landed here.</p>")))
    pages = web.fetch_pages([hit("http://example.org/0")], transport=ok)
    assert [p.text for p in pages] == ["Landed here."]


# --------------------------------------------------------------------------- fetch caps

def test_only_the_top_n_pages_are_fetched():
    net = Net(("", Resp("<p>Some text here.</p>")))
    hits = [hit(f"http://site{i}.example.org/p") for i in range(6)]
    pages = web.fetch_pages(hits, max_pages=2, transport=net)
    assert len(pages) == 2 and len(net.calls) == 2
    assert web.fetch_pages(hits, max_pages=0, transport=net) == []


def test_at_most_a_megabyte_is_read_and_max_chars_kept():
    page = "<p>" + "Sentence number one. " * 200_000 + "TAIL-BEYOND-THE-CAP</p>"
    assert len(page) > web.MAX_BYTES * 3
    net = Net(("", Resp(page)))
    (whole,) = web.fetch_pages([hit("http://example.org/big")], max_chars=10**7, transport=net)
    assert "TAIL-BEYOND-THE-CAP" not in whole.text and len(whole.text) <= web.MAX_BYTES
    (small,) = web.fetch_pages([hit("http://example.org/big")], max_chars=500, transport=net)
    assert len(small.text) <= 500 and small.text.endswith(".")          # ended at a sentence


def test_non_text_content_is_skipped_and_plain_text_kept():
    net = Net(("a.example.org", Resp("%PDF-1.7", ctype="application/pdf")),
              ("b.example.org", Resp("PNG", ctype="image/png")),
              ("c.example.org", Resp("Plain words. More words.", ctype="text/plain")),
              ("d.example.org", Resp("<p>Fine.</p>", ctype="application/xhtml+xml")))
    pages = web.fetch_pages([hit(f"http://{x}.example.org/") for x in "abcd"], max_pages=4, transport=net)
    assert [p.url for p in pages] == ["http://c.example.org/", "http://d.example.org/"]


def test_scripts_styles_and_page_furniture_are_not_in_the_text():
    net = Net(("", Resp(fx.ARTICLE_HTML)))
    (page,) = web.fetch_pages([hit("http://example.org/pst")], transport=net)
    assert page.title == "PST guide - Example"
    assert "Outlook keeps its mail" in page.text and "Old files are archived" in page.text
    for hidden in ("SCRIPT-ONLY-TEXT", "INLINE-SCRIPT-TEXT", "NAVIGATION-TEXT", "FOOTER-TEXT", "color: red"):
        assert hidden not in page.text


def test_a_broken_or_erroring_page_is_left_out_not_raised():
    net = Net(("a.example.org", Resp("gone", status=404)), ("b.example.org", ConnectionError()),
              ("c.example.org", Resp("<p>Kept.</p>")))
    pages = web.fetch_pages([hit(f"http://{x}.example.org/") for x in "abc"], max_pages=3, transport=net)
    assert [p.text for p in pages] == ["Kept."]


def test_wikipedia_pages_are_read_as_plain_text_through_the_api():
    net = Net(("wikipedia.org/w/api.php", Resp(fx.WIKI_EXTRACT, ctype="application/json")))
    h = hit("https://en.wikipedia.org/wiki/Personal_Storage_Table", "Personal Storage Table")
    (page,) = web.fetch_pages([h], max_chars=120, transport=net)
    assert net.calls[0]["params"]["prop"] == "extracts" and net.calls[0]["params"]["explaintext"] == 1
    assert net.calls[0]["params"]["titles"] == "Personal Storage Table"
    assert page.text.startswith("In computing") and len(page.text) <= 120


# --------------------------------------------------------------------------- sanitize_query

@pytest.mark.parametrize("dirty,keeps", [
    ("delivery terms C:\\Users\\alice\\Documents\\deal.docx", "delivery terms"),
    ("terms in \"C:\\My Docs\\Private plan\\x.pdf\" please", "terms in please"),
    ("\\\\fileserver\\share\\hr\\salaries.xlsx pay scale", "pay scale"),
    ("open D:/work/clients/acme/notes today", "open today"),
    ("look in /home/alice/mail/inbox now", "look in now"),
    ("~/secrets/keys and ssh", "and ssh"),
    ("mail alice@example.com about rent", "mail about rent"),
    ("see https://intranet.corp/wiki/page?id=4 and more", "see and more"),
    ("see www.corp.example/wiki for more", "see for more"),
    ("delivery in ZEBRA-4471-INVOICE.pdf then", "delivery in then"),
    ("summary of budget_2024_final.XLSX", "summary of"),
    ("account 12345678 balance", "account balance"),
    ("phone 020-7946-0958 reception", "phone reception"),
    ("token a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6 leaked", "token leaked"),
])
def test_sanitize_removes_what_could_be_local_data(dirty, keeps):
    assert web.sanitize_query(dirty) == keeps


def test_sanitize_keeps_ordinary_search_words():
    assert web.sanitize_query("what is a .pst file in Outlook 2019") == "what is a pst file in Outlook 2019"
    assert web.sanitize_query("TCP/IP and node.js in 2024") == "TCP/IP and node.js in 2024"


def test_sanitize_removes_every_avoid_string_however_it_is_spelled():
    avoid = [FILE_NAME, FOLDER, EMAIL, PASSAGE]
    for text in ("zebra 4471 invoice terms", "the Secret folder of alice", "Rotterdam depot delay",
                 "consignment for Halvorsen Marine was delayed at", "example.com mail"):
        out = web.sanitize_query(text, avoid=avoid).lower()
        for frag in ("zebra", "4471", "alice", "secret", "halvorsen", "example.com"):
            assert frag not in out, (text, out)
    # ...but unrelated words survive
    assert web.sanitize_query("zebra stripes", avoid=avoid) == "zebra stripes"


def test_sanitize_caps_words_and_characters():
    words = " ".join(f"word{i}x" for i in range(30))
    out = web.sanitize_query(words)
    assert len(out.split()) == web.MAX_QUERY_WORDS
    long = " ".join(["extraordinarily"] * 9)
    assert len(web.sanitize_query(long)) <= web.MAX_QUERY_CHARS


def test_sanitize_returns_nothing_when_nothing_safe_is_left():
    assert web.sanitize_query("") == "" and web.sanitize_query(None) == ""
    assert web.sanitize_query("C:\\Users\\alice\\x.pdf alice@example.com 12345678") == ""
    assert web.sanitize_query("the of and", avoid=()) == ""
    assert web.sanitize_query("ZEBRA-4471-INVOICE", avoid=[FILE_NAME]) == ""


# --------------------------------------------------------------------------- compose_query

def test_compose_uses_the_local_model_and_never_sends_it_more_than_the_question():
    llm = FakeLLM(script=["PST file format Outlook"])
    out = web.compose_query("What is a PST file? My mail is in C:\\Mail\\me.pst", llm, avoid=["me.pst"])
    assert out == "PST file format Outlook"
    (kind, prompt), = llm.calls
    assert "PST file" in prompt and "\n\n" in prompt


@pytest.mark.parametrize("reply", ["", "Sure! Here is a search query for you", "What is a PST file?",
                                   "I cannot help with that request because it is unclear", "\n\n"])
def test_compose_falls_back_to_the_questions_own_keywords_on_junk(reply):
    llm = FakeLLM(script=[reply])
    assert web.compose_query("Tell me what a PST file is", llm) == "PST file"


def test_compose_falls_back_when_the_model_is_down_or_absent():
    assert web.compose_query("Tell me what a PST file is", FakeLLM(up=False)) == "PST file"
    assert web.compose_query("Tell me what a PST file is", None) == "PST file"

    class Broken:
        def generate(self, *a, **k):
            raise ValueError("weird")
    assert web.compose_query("how does Outlook archive mail", Broken()) == "Outlook archive mail"


def test_compose_strips_a_path_or_file_name_the_model_smuggles_in():
    for reply in (f"delivery delay {FOLDER}{FILE_NAME}", f"Keywords: delivery delay {EMAIL} {FILE_NAME}",
                  "delivery delay ZEBRA-4471-INVOICE"):
        out = web.compose_query("what does the delivery delay say", FakeLLM(script=[reply]),
                                avoid=[FILE_NAME, FOLDER, EMAIL])
        assert not any(p.lower() in out.lower() for p in PLANTED + FRAGMENTS), out
        assert "delay" in out


def test_compose_sanitises_the_fallback_too_and_can_return_nothing():
    q = f"summarise {FILE_NAME} in {FOLDER} for {EMAIL}"
    out = web.compose_query(q, None, avoid=[FILE_NAME, FOLDER, EMAIL])
    assert out == "summarise"       # the one safe word the question left
    assert web.compose_query(f"{FILE_NAME} {EMAIL}", None, avoid=[FILE_NAME, EMAIL]) == ""
    assert web.compose_query("", FakeLLM()) == ""


# --------------------------------------------------------------------------- context shaping

def test_web_context_numbers_pages_first_then_snippet_only_hits():
    result = web.WebResult("q", "wikipedia", hits=[
        web.WebHit("A", "https://a.example.org/", "Snippet of A."),
        web.WebHit("B", "https://b.example.org/", "Snippet of B."),
        web.WebHit("C", "https://c.example.org/", "")],
        pages=[web.WebPage("https://b.example.org", "Page B", "First sentence. Second sentence. Third one here.")])
    assert [n for n, _ in result.sources()] == [1, 2]
    ctx = web.web_context(result, max_chars=30)
    assert [(n, t, u) for n, t, u, _ in ctx] == [(1, "Page B", "https://b.example.org"),
                                                (2, "A", "https://a.example.org/")]
    assert ctx[0][3] == "First sentence."          # clipped at a sentence, not mid-word
    assert web.web_context(web.WebResult("q", error="x"), max_chars=100) == []


# --------------------------------------------------------------------------- privacy guard

QUESTION = (f"What does {FILE_NAME} in {FOLDER} say about the delivery to {EMAIL}? "
            f"{PASSAGE} Explain the Incoterms please")


def _full_run(net, *, llm_reply):
    llm = FakeLLM(script=[llm_reply])
    avoid = [FILE_NAME, FOLDER, EMAIL, PASSAGE]
    query = web.compose_query(QUESTION, llm, avoid=avoid)
    assert query, "the fixture question still has safe words"
    result = web.run_web(query, on(provider="wikipedia"), transport=net, avoid=avoid)
    return query, result


def _assert_clean(net):
    sent = net.everything_sent().lower()
    for secret in PLANTED + FRAGMENTS:
        assert secret.lower() not in sent, secret
        assert secret.lower().replace("\\", "\\\\") not in sent, secret


def _wiki_net():
    return Net(("w/api.php", lambda url, p: Resp(fx.WIKI_EXTRACT if p.get("prop") else fx.WIKI_SEARCH,
                                                 ctype="application/json")))


@pytest.mark.parametrize("reply", [
    "Incoterms delivery terms",                                             # a well-behaved model
    f"Incoterms delivery {FILE_NAME} {FOLDER} {EMAIL}",                     # one that smuggles
    "zebra 4471 invoice Halvorsen Rotterdam delayed Incoterms",             # partial leakage
    "",                                                                     # no model output at all
])
def test_no_planted_string_is_in_any_outgoing_request(reply):
    net = _wiki_net()
    query, result = _full_run(net, llm_reply=reply)
    assert result.ok and net.calls
    _assert_clean(net)
    # the request carried exactly the query, and only a User-Agent as a header
    assert net.calls[0]["params"]["srsearch"] == query
    for call in net.calls:
        assert set(call["headers"]) == {"User-Agent"} and call["headers"]["User-Agent"] == web.USER_AGENT


def test_a_raw_question_handed_to_run_web_is_sanitised_before_it_leaves():
    net = _wiki_net()
    result = web.run_web(QUESTION, on(provider="wikipedia"), transport=net,
                         avoid=[FILE_NAME, FOLDER, EMAIL, PASSAGE])
    assert result.ok
    _assert_clean(net)


def test_nothing_safe_means_no_request_at_all():
    net = _wiki_net()
    result = web.run_web(f"{FILE_NAME} {EMAIL}", on(provider="wikipedia"), transport=net, avoid=[FILE_NAME, EMAIL])
    assert result.error == web.MSG_NOTHING_SAFE and net.calls == [] and not result.ok


def test_result_pages_are_fetched_without_the_query_in_the_request():
    net = Net(("duckduckgo.com/html", Resp(fx.DDG_RESULTS)), ("pages.example.org", Resp(fx.ARTICLE_HTML)),
              ("wiki.example.net", Resp(fx.ARTICLE_HTML)))
    result = web.run_web("Incoterms delivery terms", on(provider="duckduckgo"), transport=net)
    fetches = [c for c in net.calls if "duckduckgo" not in c["url"]]
    assert len(fetches) == 2 and all(c["params"] is None and "Incoterms" not in c["url"] for c in fetches)
    assert result.provider == "duckduckgo" and len(result.pages) == 2


# --------------------------------------------------------------------------- socket-level guard

class _RequestsResponse:
    def __init__(self, body: str, ctype="text/html; charset=utf-8", status=200):
        self.status_code, self.headers, self._body = status, {"Content-Type": ctype}, body.encode()

    def iter_content(self, size):
        yield self._body

    def close(self):
        pass


def test_a_full_run_touches_only_the_provider_and_the_result_page_hosts(monkeypatch):
    import requests

    allowed = {"html.duckduckgo.com", "pages.example.org", "wiki.example.net"}
    asked_dns, requested, connected = [], [], []

    def fake_getaddrinfo(host, port, *a, **k):
        asked_dns.append(host)
        if host not in allowed:
            raise OSError(f"blocked lookup of {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def fake_request(method, url, **kw):
        requested.append((method, url, kw))
        assert kw.get("allow_redirects") is False           # redirects are followed (guarded) by Leasha
        host = url.split("/")[2]
        assert host in allowed, f"unexpected request to {host}"
        body = fx.DDG_RESULTS if host == "html.duckduckgo.com" else fx.ARTICLE_HTML
        return _RequestsResponse(body)

    def no_connect(*a, **k):
        connected.append(a)
        raise OSError("no real connections in this test")

    monkeypatch.setattr(requests, "request", fake_request)
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", no_connect)

    result = web.run_web("Incoterms delivery terms", on(provider="duckduckgo"))     # the REAL transport
    assert result.ok and result.provider == "duckduckgo" and len(result.pages) == 2
    assert {u.split("/")[2] for _, u, _ in requested} == allowed
    assert set(asked_dns) <= allowed and connected == []
    assert all(kw["headers"]["User-Agent"] == web.USER_AGENT and set(kw["headers"]) <= {"User-Agent", "Accept"}
               for _, _, kw in requested)
    assert all(kw["timeout"] <= 8.0 for _, _, kw in requested)


def test_the_real_transport_turns_a_dead_network_into_a_plain_sentence(monkeypatch):
    import requests

    def dead(method, url, **kw):
        raise requests.ConnectionError("no route")

    def slow(method, url, **kw):
        raise requests.Timeout("slow")
    monkeypatch.setattr(requests, "request", dead)
    assert web.run_web("pst file", on(provider="wikipedia")).error == web.MSG_UNREACHABLE
    monkeypatch.setattr(requests, "request", slow)
    assert web.run_web("pst file", on(provider="wikipedia")).error == web.MSG_TIMEOUT


# --------------------------------------------------------------------------- layering

def test_the_module_keeps_the_chat_layering_rules():
    source = (ROOT / "app" / "chat" / "web.py").read_text(encoding="utf-8")
    assert "import requests" not in source and "urllib" not in source
    assert "PyQt" not in source and "app.ui" not in source


# --------------------------------------------------------------------------- the person's own request

@pytest.mark.parametrize("asked, kept", [
    ("How much notice must the tenant give? Search the web too.", "How much notice must the tenant give?"),
    ("google what is a PST file", "what is a PST file"),
    ("what is a PST file online?", "what is a PST file"),
    ("Look it up online: latest outlook version", "latest outlook version"),
])
def test_the_instruction_to_use_the_web_is_not_itself_searched_for(asked, kept):
    stripped = web.strip_web_request(asked).replace(" ?", "?").replace("? .", "?").strip(" .:")
    assert stripped.rstrip("?") == kept.rstrip("?")
    query = web.compose_query(asked, None)
    assert "web" not in query.lower().split() and "google" not in query.lower().split()
    assert "online" not in query.lower().split()
