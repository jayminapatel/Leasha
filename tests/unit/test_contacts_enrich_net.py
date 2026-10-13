"""Order 1k section 7f-7i: enrichment from the internet, and running it.

Layer: L7

**No network, ever.** Every test replaces the socket layer with one that fails
the test on any connection, and passes a fake `Transport`. The guard that only the
allow-listed hosts could be reached is the real one: `run` puts the fake behind a
`GuardedTransport` over `AllowedHosts`, so a request to any host that is not on
`ENRICHMENT_HOSTS` (or a company's own domain, registered for one call) raises
before the fake sees it.

Pinned: the internet sources are off by default and send nothing when off;
Gravatar sends only a hash; Wikidata is never asked about a person; a logo comes
only from the contact's own domain; the same lookup is not repeated within
90 days; a proposal is not applied until accepted.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
from urllib.parse import parse_qs, urlsplit

import pytest

from app.accounts.http import AllowedHosts
from app.accounts.provider import HttpResponse
from app.accounts.store import AccountSettings, AccountStore
from app.contacts.card import Card, Field
from app.contacts.enrich import apply_proposals, enrich_selection, proposals_for
from app.contacts.enrich import gravatar, logo, wikidata
from app.contacts.enrich.base import GuardedTransport
from app.contacts.hub import ContactHub
from app.core.errors import AppErrorException
from app.storage.sqlite_store import SqliteStore

DAY = 86400


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test opened a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "net.db").connect()
    yield ContactHub(store)
    store.close()


def png(color=(10, 120, 200), size=(32, 32)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


class Fake:
    """A Transport that answers from a list of (url prefix, response or callable)."""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.calls: list[tuple[str, str, dict, object]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append((method, url, dict(headers or {}), body))
        for prefix, resp in self.routes:
            if url.startswith(prefix):
                return resp(url) if callable(resp) else resp
        return HttpResponse(404)

    @property
    def urls(self):
        return [c[1] for c in self.calls]

    @property
    def hosts(self):
        return {urlsplit(u).hostname for u in self.urls}


class Boom:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append(url)
        raise AssertionError(f"a switched-off source made a request: {url}")


class Down:
    def request(self, method, url, **kw):
        raise AppErrorException(__import__("app.core.errors", fromlist=["make_error"]).make_error(
            "ERR_ACCOUNT_UNREACHABLE", "test", account="x", reason="offline", details="offline"))


ON = dict(enrich_gravatar=True, enrich_logo=True, enrich_wikidata=True)
LOCAL_OFF = dict(enrich_signatures=False, enrich_company=False, enrich_faces=False)


def person(hub, email="Ann@Acme-Widgets.co.uk", **scalars):
    card = Card(scalars={"given": "Ann", "family": "Lee", **scalars},
                fields=[Field("email", email, "work", primary=True)] if email else [])
    return hub.create(card)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- off by default -----------------------------------------------------------------

def test_the_internet_switches_are_off_by_default_and_the_local_ones_on():
    s = AccountSettings()
    assert (s.enrich_gravatar, s.enrich_logo, s.enrich_wikidata) == (False, False, False)
    assert all([s.enrich_signatures, s.enrich_company, s.enrich_faces, s.enrich_history, s.enrich_suggest])


def test_with_the_defaults_nothing_is_sent_anywhere(hub):
    cid = person(hub, company="Acme Widgets")
    hub.update(cid, _with_url(hub.get_card(cid), "https://acme-widgets.co.uk"))
    boom = Boom()
    out = enrich_selection(hub, AccountSettings(), [cid], transport=boom)
    assert boom.calls == []
    assert all(p.source in ("signature", "company", "face") for p in out)
    # and through the UI entry point, with the settings read from the store
    assert proposals_for(hub, AccountStore(hub.store), [cid], transport=boom) is not None and boom.calls == []


def _with_url(card, url):
    card.fields.append(Field("url", url, "work"))
    return card


def test_a_source_that_is_off_is_not_called_while_the_others_are(hub):
    cid = person(hub, company="Acme Widgets")
    fake = Fake(("https://www.gravatar.com/avatar/", HttpResponse(200, body=png())))
    s = AccountSettings(enrich_gravatar=True, **LOCAL_OFF)
    out = enrich_selection(hub, s, [cid], transport=fake)
    assert fake.hosts == {"www.gravatar.com"} and [p.source for p in out] == ["gravatar"]


# ---- the guard -----------------------------------------------------------------------

def test_only_listed_hosts_can_be_reached_through_the_guard():
    fake = Fake()
    g = GuardedTransport(fake, AllowedHosts(("gravatar", "wikidata")))
    g.request("GET", "https://www.gravatar.com/x")
    g.request("GET", "https://www.wikidata.org/w/api.php")
    for bad in ("https://evil.example.com/", "https://www.googleapis.com/", "http://www.gravatar.com/x",
                "https://gravatar.com.evil.example/"):
        with pytest.raises(AppErrorException):
            g.request("GET", bad)
    assert fake.hosts == {"www.gravatar.com", "www.wikidata.org"}


# ---- 7f Gravatar --------------------------------------------------------------------------

def test_gravatar_sends_only_the_hash_of_the_lowercased_trimmed_address(hub):
    cid = person(hub, email="  Ann@Acme-Widgets.co.uk ")
    want = hashlib.sha256(b"ann@acme-widgets.co.uk").hexdigest()
    profile = {"entry": [{"urls": [{"value": "https://ann.example/", "title": "Blog"},
                                   {"value": "http://insecure.example/", "title": "Old"}]}]}
    fake = Fake((f"https://www.gravatar.com/avatar/{want}", HttpResponse(200, body=png())),
                (f"https://www.gravatar.com/{want}.json", HttpResponse(200, body=json.dumps(profile).encode())))
    out = enrich_selection(hub, AccountSettings(**LOCAL_OFF, enrich_gravatar=True), [cid], transport=fake)
    assert len(fake.calls) == 2
    for method, url, headers, body in fake.calls:
        assert "@" not in url and want in url and urlsplit(url).hostname == "www.gravatar.com"
        assert method == "GET" and body is None
        assert not any("@" in str(v) or "ann" in str(v).lower().replace("annotation", "") for v in headers.values())
    assert "d=404" in fake.urls[0]
    photo = [p for p in out if p.field_kind == "photo"][0]
    assert photo.source == "gravatar" and photo.receipt == f"https://www.gravatar.com/{want}"
    assert [(p.field_kind, p.field_label, p.new) for p in out if p.field_kind == "url"] == [
        ("url", "Blog", "https://ann.example/")]                                 # https links only
    assert hub.get_card(cid).photo is None and hub.sources(cid) == {}            # proposed, not applied
    apply_proposals(hub, out, range(len(out)))
    assert hub.get_card(cid).photo is not None
    assert hub.sources(cid)["photo"][0][0] == "enrichment:gravatar"
    assert hub.sources(cid)["f:url:ann.example"][0] == ("enrichment:gravatar", photo.receipt)


def test_gravatar_with_no_picture_and_a_bad_picture_gives_no_photo(hub):
    cid = person(hub)
    assert enrich_selection(hub, AccountSettings(**LOCAL_OFF, enrich_gravatar=True), [cid], transport=Fake()) == []
    junk = Fake(("https://www.gravatar.com/avatar/", HttpResponse(200, body=b"<html>not an image</html>")))
    assert enrich_selection(hub, AccountSettings(**LOCAL_OFF, enrich_gravatar=True), [cid], transport=junk,
                            force=True) == []


def test_gravatar_hash_is_not_the_address():
    assert gravatar.email_hash("A@b.co ") == hashlib.sha256(b"a@b.co").hexdigest()
    assert "@" not in gravatar.avatar_url("a@b.co") and "@" not in gravatar.profile_url("a@b.co")


def test_a_contact_with_no_email_sends_nothing_to_gravatar(hub):
    cid = person(hub, email="")
    boom = Boom()
    assert enrich_selection(hub, AccountSettings(**LOCAL_OFF, enrich_gravatar=True), [cid], transport=boom) == []
    assert boom.calls == []


# ---- 7g logo -----------------------------------------------------------------------------------

def _logo_settings():
    return AccountSettings(**LOCAL_OFF, enrich_logo=True)


def test_the_logo_comes_from_the_contacts_own_domain_and_the_host_is_forgotten(hub):
    cid = person(hub, email="ann@acme-widgets.co.uk")
    fake = Fake(("https://acme-widgets.co.uk/favicon.ico", HttpResponse(200, body=png())))
    out = enrich_selection(hub, _logo_settings(), [cid], transport=fake)
    assert fake.urls == ["https://acme-widgets.co.uk/favicon.ico"]
    assert len(out) == 1 and out[0].field_kind == "photo" and out[0].source == "logo"
    assert out[0].receipt == "https://acme-widgets.co.uk/favicon.ico"
    assert hub.get_card(cid).photo is None


def test_the_contacts_web_address_beats_the_email_domain_when_it_is_the_companys(hub):
    """Order 1s 6d: the web address is still asked first, but only when it is the company's own site -
    its registrable name is the work email's (acme-widgets.com for acme-widgets.co.uk)."""
    cid = person(hub, email="ann@acme-widgets.co.uk")
    hub.update(cid, _with_url(hub.get_card(cid), "www.acme-widgets.com/about"))
    fake = Fake(("https://www.acme-widgets.com/favicon.ico", HttpResponse(200, body=png())))
    enrich_selection(hub, _logo_settings(), [cid], transport=fake)
    assert fake.hosts == {"www.acme-widgets.com"}


def test_the_home_page_icon_is_used_when_there_is_no_favicon_file_and_never_from_another_host(hub):
    cid = person(hub, email="ann@acme-widgets.co.uk")
    page = (b'<html><head><link rel="stylesheet" href="/x.css">'
            b'<link rel="icon" href="https://cdn.evil.example/i.png">'
            b'<link rel="shortcut icon" href="/static/i.png"></head></html>')
    fake = Fake(("https://acme-widgets.co.uk/static/i.png", HttpResponse(200, body=png())),
                ("https://acme-widgets.co.uk/", HttpResponse(200, body=page)))
    out = enrich_selection(hub, _logo_settings(), [cid], transport=fake)
    assert fake.urls == ["https://acme-widgets.co.uk/favicon.ico", "https://acme-widgets.co.uk/",
                         "https://acme-widgets.co.uk/static/i.png"]
    assert out[0].receipt == "https://acme-widgets.co.uk/static/i.png" and fake.hosts == {"acme-widgets.co.uk"}


def test_a_redirect_is_followed_only_to_the_same_site(hub):
    cid = person(hub, email="ann@acme-widgets.co.uk")
    fake = Fake(("https://acme-widgets.co.uk/favicon.ico",
                 HttpResponse(301, {"location": "https://www.acme-widgets.co.uk/favicon.ico"})),
                ("https://www.acme-widgets.co.uk/favicon.ico", HttpResponse(200, body=png())))
    assert len(enrich_selection(hub, _logo_settings(), [cid], transport=fake)) == 1
    away = Fake(("https://acme-widgets.co.uk/", HttpResponse(302, {"location": "https://evil.example/x.ico"})),
                ("https://acme-widgets.co.uk/favicon.ico", HttpResponse(302, {"location": "https://evil.example/x.ico"})))
    assert enrich_selection(hub, _logo_settings(), [cid], transport=away, force=True) == []
    assert "evil.example" not in away.hosts


@pytest.mark.parametrize("email,url", [
    ("ann@gmail.com", ""),                                      # free mail: no company, no domain
    ("", "http://127.0.0.1/admin"), ("", "http://192.168.0.1/"), ("", "https://localhost/"),
    ("", "https://printer.local/"), ("", "https://intranet/"), ("", "https://nas.lan/"),
])
def test_no_request_for_free_mail_an_ip_address_or_a_private_name(hub, email, url):
    cid = person(hub, email=email)
    if url:
        hub.update(cid, _with_url(hub.get_card(cid), url))
    boom = Boom()
    assert enrich_selection(hub, _logo_settings(), [cid], transport=boom) == []
    assert boom.calls == []


def test_a_logo_is_not_fetched_for_a_card_that_already_has_a_photo(hub):
    from app.contacts.card import Photo
    cid = person(hub, email="ann@acme-widgets.co.uk")
    card = hub.get_card(cid)
    card.photo = Photo("image/png", png())
    hub.update(cid, card)
    boom = Boom()
    assert enrich_selection(hub, _logo_settings(), [cid], transport=boom) == [] and boom.calls == []


def test_the_registered_company_host_is_forgotten_after_the_call():
    allowed = AllowedHosts(("gravatar", "wikidata"))
    fake = Fake(("https://acme-corp.com/favicon.ico", HttpResponse(200, body=png())))
    card = Card(fields=[Field("email", "a@acme-corp.com")])
    out = logo.logo_proposals(GuardedTransport(fake, allowed), allowed, 1, card)
    assert out and allowed.describe()["servers"] == []
    with pytest.raises(AppErrorException):
        allowed.check("https://acme-corp.com/favicon.ico")


# ---- 7h Wikidata ---------------------------------------------------------------------------------

def wd_transport(*, human=False, extra_hits=()):
    def api(url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if q["action"] == "wbsearchentities":
            hits = [{"id": "Q1", "label": q["search"]}, *extra_hits]
            return HttpResponse(200, body=json.dumps({"search": hits}).encode())
        ids = q["ids"].split("|")
        ents = {}
        for i in ids:
            if i == "Q1":
                claims = {"P856": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "https://acme.example"}}}],
                          "P159": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q84"}}}}],
                          "P452": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q11"}}}},
                                   {"rank": "deprecated", "mainsnak": {"datavalue": {"value": {"id": "Q99"}}}}]}
                if human:
                    claims["P31"] = [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]
                ents[i] = {"claims": claims, "labels": {"en": {"value": "Acme"}}}
            else:
                ents[i] = {"labels": {"en": {"value": {"Q84": "London", "Q11": "Manufacturing"}.get(i, i)}}}
        return HttpResponse(200, body=json.dumps({"entities": ents}).encode())
    return Fake(("https://www.wikidata.org/w/api.php", api))


def _wd_settings():
    return AccountSettings(**LOCAL_OFF, enrich_wikidata=True)


def test_wikidata_offers_a_companys_website_headquarters_and_industry(hub):
    cid = hub.create(Card(scalars={"company": "Acme Ltd"}))                  # an organisation card
    fake = wd_transport()
    out = enrich_selection(hub, _wd_settings(), [cid], transport=fake)
    assert fake.hosts == {"www.wikidata.org"}
    got = {(p.field_kind, p.field_label): p for p in out}
    assert got[("url", "company")].new == "https://acme.example"
    assert got[("address", "headquarters")].new == "London"
    assert got[("custom", "industry")].new == "Manufacturing"                # the deprecated claim is ignored
    assert all(p.source == "wikidata" and p.receipt.startswith("https://www.wikidata.org/wiki/Q1")
               and "CC0" in p.receipt for p in out)
    assert hub.get_card(cid).values("url") == []
    apply_proposals(hub, out, range(len(out)))
    assert hub.sources(cid)["f:url:acme.example"][0][0] == "enrichment:wikidata"


def test_what_is_sent_to_wikidata_is_the_company_name_and_nothing_about_a_person(hub):
    cid = person(hub, email="ann@acme.example", company="Acme Ltd")
    fake = wd_transport()
    enrich_selection(hub, _wd_settings(), [cid], transport=fake)
    searches = [parse_qs(urlsplit(u).query).get("search") for u in fake.urls]
    assert [x for x in searches if x] == [["Acme Ltd"]]
    sent = " ".join(fake.urls).lower()
    for secret in ("ann", "lee", "@"):
        assert secret not in sent, secret


def test_wikidata_is_never_asked_about_a_person(hub):
    no_company = person(hub)                                                  # a person with no company
    own_name = person(hub, email="b@x.example", company="Ann Lee")           # the company field holds the name
    given_only = hub.create(Card(scalars={"given": "Ann", "company": "Ann"}))
    boom = Boom()
    assert enrich_selection(hub, _wd_settings(), [no_company, own_name, given_only], transport=boom) == []
    assert boom.calls == []


def test_the_code_asserts_wikidata_is_for_companies_only(hub, monkeypatch):
    cid = person(hub, company="Acme")
    boom = Boom()
    monkeypatch.setattr(wikidata, "company_term", lambda card: "Ann Lee")    # a bug that tried to send a name
    with pytest.raises(AssertionError, match="never people"):
        wikidata.wikidata_proposals(boom, cid, hub.get_card(cid))
    assert boom.calls == []


def test_an_entity_wikidata_says_is_a_human_is_rejected():
    assert wikidata.lookup_company(wd_transport(human=True), "Acme") is None


def test_only_an_exact_name_match_is_accepted():
    fake = Fake(("https://www.wikidata.org/w/api.php",
                 HttpResponse(200, body=json.dumps({"search": [{"id": "Q5", "label": "Acme Corporation Of Mars"}]}).encode())))
    assert wikidata.lookup_company(fake, "Acme") is None and len(fake.calls) == 1


def test_colleagues_at_one_company_share_one_wikidata_lookup(hub):
    a = person(hub, email="a@acme-corp.com", company="Acme Ltd")
    b = person(hub, email="b@acme-corp.com", company="ACME")
    fake = wd_transport()
    out = enrich_selection(hub, _wd_settings(), [a, b], transport=fake)
    assert len(fake.calls) == 3 and {p.contact_id for p in out} == {a, b}


# ---- 7i on demand, and the 90-day memory ---------------------------------------------------------------------

def test_the_same_lookup_is_not_repeated_within_90_days(hub):
    cid = person(hub, email="ann@acme-widgets.co.uk", company="Acme Ltd")
    s = AccountSettings(**LOCAL_OFF, **ON)
    clock = Clock()
    fake = Fake(("https://acme-widgets.co.uk/favicon.ico", HttpResponse(200, body=png())),
                ("https://www.gravatar.com/avatar/", HttpResponse(404)),
                ("https://www.wikidata.org/w/api.php", wd_transport().routes[0][1]))
    first = enrich_selection(hub, s, [cid], transport=fake, clock=clock)
    n = len(fake.calls)
    assert n >= 4 and {p.source for p in first} >= {"logo", "wikidata"}
    clock.t += 89 * DAY
    # not asked again - and, since order 1s 6a, the remembered answer is offered again rather than lost
    again = enrich_selection(hub, s, [cid], transport=fake, clock=clock)
    assert len(fake.calls) == n and {p.source for p in again} == {p.source for p in first}
    clock.t += 2 * DAY                                                       # 91 days after the first
    assert enrich_selection(hub, s, [cid], transport=fake, clock=clock) and len(fake.calls) == 2 * n
    # "Look again" ignores the memory
    assert enrich_selection(hub, s, [cid], transport=fake, clock=clock, force=True)
    assert len(fake.calls) == 3 * n


def test_what_is_remembered_holds_a_time_and_never_an_address(hub):
    cid = person(hub, email="ann@acme-widgets.co.uk", company="Acme Ltd")
    clock = Clock(1_800_000_000.0)
    enrich_selection(hub, AccountSettings(**LOCAL_OFF, **ON), [cid], transport=Fake(), clock=clock)
    rows = hub.conn.execute("SELECT key, value FROM index_state WHERE key LIKE 'enrich:seen:%'").fetchall()
    kinds = {r["key"].split(":")[2] for r in rows}
    assert kinds == {"gravatar", "logo", "wikidata"}
    assert all("@" not in r["key"] and r["value"] == "1800000000" for r in rows)


def test_a_lookup_with_no_answer_is_not_remembered(hub):
    cid = person(hub, email="ann@acme-widgets.co.uk", company="Acme Ltd")
    s = AccountSettings(**LOCAL_OFF, **ON)
    notes: list[str] = []
    assert enrich_selection(hub, s, [cid], transport=Down(), notes=notes) == []
    assert len(notes) == 3
    assert hub.conn.execute("SELECT COUNT(*) FROM index_state WHERE key LIKE 'enrich:seen:%'").fetchone()[0] == 0


def test_a_lookup_that_found_nothing_is_remembered_too(hub):
    cid = person(hub)
    s = AccountSettings(**LOCAL_OFF, enrich_gravatar=True)
    fake = Fake()
    enrich_selection(hub, s, [cid], transport=fake)
    n = len(fake.calls)
    assert n and enrich_selection(hub, s, [cid], transport=fake) == [] and len(fake.calls) == n


def test_proposals_for_reads_the_switches_from_the_account_store(hub):
    cid = person(hub)
    store = AccountStore(hub.store)
    fake = Fake(("https://www.gravatar.com/avatar/", HttpResponse(200, body=png())))
    assert [p for p in proposals_for(hub, store, [cid], transport=fake) if p.source == "gravatar"] == []
    assert fake.calls == []
    store.update_settings(enrich_gravatar=True)
    got = proposals_for(hub, store, [cid], transport=fake)
    assert [p.source for p in got if p.source == "gravatar"] == ["gravatar"]


def test_nothing_in_the_package_runs_by_itself():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "contacts" / "enrich"
    text = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    assert not re.search(r"\b(?:Thread|QTimer|threading|sched|Timer\(|asyncio|ThreadPool)", text.replace(
        "no timer, no thread", ""))


def test_a_second_run_says_which_internet_sources_were_not_asked_again(hub):
    """The owner, 2026-10-12: "it does not seem to work". After the first run every lookup was remembered, so
    the next ninety days of runs asked the internet nothing - and said nothing about it. Now each run says so."""
    cid = person(hub, email="ann@acme-widgets.co.uk", company="Acme Ltd")
    s = AccountSettings(**LOCAL_OFF, **ON)
    clock = Clock()
    enrich_selection(hub, s, [cid], transport=Fake(), clock=clock)
    notes: list[str] = []
    clock.t += DAY
    enrich_selection(hub, s, [cid], transport=Fake(), clock=clock, notes=notes)
    assert len(notes) == 3 and all("not asked again for 1 contact:" in n and "Look again" in n for n in notes)
    assert {n.split(" was ")[0] for n in notes} == {"Gravatar", "Company logos", "Wikidata"}
    forced: list[str] = []
    enrich_selection(hub, s, [cid], transport=Fake(), clock=clock, notes=forced, force=True)
    assert forced == []                                                    # Look again skips nothing


def test_a_social_profile_is_not_taken_for_the_company_site(hub):
    """Found on the owner's index, 2026-10-12: a LinkedIn profile on a card sent the logo lookup to LinkedIn."""
    cid = person(hub, email="ann@acme-widgets.co.uk")
    hub.update(cid, _with_url(hub.get_card(cid), "https://ae.linkedin.com/in/ann-lee"))
    assert logo.company_domain(hub.get_card(cid)) == "acme-widgets.co.uk"
    lonely = person(hub, email="bo@gmail.com")
    hub.update(lonely, _with_url(hub.get_card(lonely), "https://www.facebook.com/bo"))
    assert logo.company_domain(hub.get_card(lonely)) == ""
    assert logo.company_domain(Card(fields=[Field("url", "https://boxcom.co.uk", "work")])) == "boxcom.co.uk"


# ---- order 1s section 6: internet sources, made sensible --------------------------------------------------------

def _all_three():
    """Gravatar (a photo and a link), the logo and Wikidata, all answering."""
    want = hashlib.sha256(b"ann@acme-widgets.co.uk").hexdigest()
    profile = {"entry": [{"urls": [{"value": "https://ann.example/", "title": "Blog"}]}]}
    return Fake((f"https://www.gravatar.com/avatar/{want}", HttpResponse(200, body=png((200, 10, 10)))),
                (f"https://www.gravatar.com/{want}.json", HttpResponse(200, body=json.dumps(profile).encode())),
                ("https://acme-widgets.co.uk/favicon.ico", HttpResponse(200, body=png())),
                ("https://www.wikidata.org/w/api.php", wd_transport().routes[0][1]))


def _offered(props):
    return sorted((p.source, p.field_kind, p.field_label, p.new.sha if p.is_photo else str(p.new), p.receipt)
                  for p in props)


def test_a_remembered_lookup_offers_its_answer_again_without_a_request(hub):
    """6a. Before, a result not accepted in the run that found it was lost for ninety days: the lookup was
    remembered, its answer was not. Now the answer is kept and offered again, and nothing is asked."""
    cid = person(hub, email="ann@acme-widgets.co.uk", company="Acme Ltd")
    s = AccountSettings(**LOCAL_OFF, **ON)
    clock = Clock()
    fake = _all_three()
    first = enrich_selection(hub, s, [cid], transport=fake, clock=clock)
    assert {p.source for p in first} == {"gravatar", "logo", "wikidata"}
    clock.t += 30 * DAY
    boom = Boom()
    again = enrich_selection(hub, s, [cid], transport=boom, clock=clock)
    assert boom.calls == [] and _offered(again) == _offered(first)
    assert all(p.contact_id == cid for p in again)
    gphoto = [p for p in again if p.source == "gravatar" and p.is_photo][0]
    assert gphoto.new.data == [p for p in first if p.source == "gravatar" and p.is_photo][0].new.data


def test_a_remembered_answer_is_offered_to_a_colleague_and_not_again_once_on_the_card(hub):
    a = person(hub, email="a@acme-widgets.co.uk", company="Acme Ltd")
    s = AccountSettings(**LOCAL_OFF, enrich_wikidata=True)
    clock = Clock()
    first = enrich_selection(hub, s, [a], transport=wd_transport(), clock=clock)
    apply_proposals(hub, first, [i for i, p in enumerate(first) if p.field_kind == "url"])
    b = person(hub, email="b@acme-widgets.co.uk", company="ACME LTD")
    clock.t += DAY
    again = enrich_selection(hub, s, [a, b], transport=Boom(), clock=clock)
    assert {(p.contact_id, p.field_kind) for p in again} == {
        (a, "address"), (a, "custom"), (b, "url"), (b, "address"), (b, "custom")}   # a's url is on its card


def test_what_is_kept_of_an_answer_is_small_and_never_an_address(hub, tmp_path):
    cid = person(hub, email="ann@acme-widgets.co.uk", company="Acme Ltd")
    enrich_selection(hub, AccountSettings(**LOCAL_OFF, **ON), [cid], transport=_all_three(), clock=Clock())
    rows = hub.conn.execute("SELECT key, value FROM index_state WHERE key LIKE 'enrich:result:%'").fetchall()
    assert {r["key"].split(":")[2] for r in rows} == {"gravatar", "logo", "wikidata"}
    for r in rows:
        assert "@" not in r["key"] and "@" not in r["value"]
        assert len(r["value"]) < 1024, r["key"]                       # a small JSON: no picture in index_state
        json.loads(r["value"])
    keys = {r["key"] for r in rows}
    assert f"enrich:result:gravatar:{gravatar.email_hash('ann@acme-widgets.co.uk')}" in keys
    assert "enrich:result:logo:acme-widgets.co.uk" in keys and "enrich:result:wikidata:acme" in keys
    pictures = list((tmp_path / "cache" / "enrich").glob("*.png"))
    assert len(pictures) == 2                                           # the Gravatar photo and the logo


def test_a_remembered_photo_whose_file_was_cleared_is_not_offered(hub, tmp_path):
    cid = person(hub, email="ann@acme-widgets.co.uk")
    s = AccountSettings(**LOCAL_OFF, enrich_logo=True)
    clock = Clock()
    assert enrich_selection(hub, s, [cid], transport=_all_three(), clock=clock)
    for f in (tmp_path / "cache" / "enrich").glob("*"):
        f.unlink()
    clock.t += DAY
    assert enrich_selection(hub, s, [cid], transport=Boom(), clock=clock) == []


def test_a_lookup_that_failed_keeps_no_answer(hub):
    cid = person(hub, email="ann@acme-widgets.co.uk", company="Acme Ltd")
    enrich_selection(hub, AccountSettings(**LOCAL_OFF, **ON), [cid], transport=Down(), notes=[])
    assert hub.conn.execute("SELECT COUNT(*) FROM index_state WHERE key LIKE 'enrich:result:%'").fetchone()[0] == 0


def test_look_again_replaces_the_remembered_answer(hub):
    cid = hub.create(Card(scalars={"company": "Acme Ltd"}))
    s = AccountSettings(**LOCAL_OFF, enrich_wikidata=True)
    clock = Clock()
    assert enrich_selection(hub, s, [cid], transport=wd_transport(), clock=clock)
    assert enrich_selection(hub, s, [cid], transport=Fake(), clock=clock, force=True) == []   # now nothing found
    assert enrich_selection(hub, s, [cid], transport=Boom(), clock=clock) == []


def test_the_repeat_interval_is_the_setting(hub):
    """6b: "Ask the internet again after" (AccountSettings.enrich_repeat_days) replaces the fixed 90 days."""
    cid = person(hub)
    s = AccountSettings(**LOCAL_OFF, enrich_gravatar=True, enrich_repeat_days=30)
    clock = Clock()
    fake = Fake()
    enrich_selection(hub, s, [cid], transport=fake, clock=clock)
    n = len(fake.calls)
    clock.t += 29 * DAY
    notes: list[str] = []
    enrich_selection(hub, s, [cid], transport=fake, clock=clock, notes=notes)
    assert len(fake.calls) == n and notes and "in the last 30 days" in notes[0]
    clock.t += 2 * DAY
    enrich_selection(hub, s, [cid], transport=fake, clock=clock)
    assert len(fake.calls) == 2 * n


def test_a_repeat_interval_of_never_asks_once_and_look_again_still_asks(hub):
    cid = person(hub)
    s = AccountSettings(**LOCAL_OFF, enrich_gravatar=True, enrich_repeat_days=0)
    clock = Clock()
    fake = Fake()
    enrich_selection(hub, s, [cid], transport=fake, clock=clock)
    n = len(fake.calls)
    clock.t += 3650 * DAY
    notes: list[str] = []
    enrich_selection(hub, s, [cid], transport=fake, clock=clock, notes=notes)
    assert len(fake.calls) == n
    assert len(notes) == 1 and "Never" in notes[0] and "Look again" in notes[0] and "90" not in notes[0]
    enrich_selection(hub, s, [cid], transport=fake, clock=clock, force=True)
    assert len(fake.calls) == 2 * n


@pytest.mark.parametrize("company,email,term", [
    ("Zentrix Technologies Account Energy", "ann@zentrix.com", "Zentrix Technologies"),   # the owner's shape
    ("Quorvale Ltd - Procurement", "ann@gmail.com", "Quorvale Ltd"),
    ("Brightmoor Systems | Senior Engineer", "ann@brightmoor.co.uk", "Brightmoor Systems"),
    ("Brightmoor Systems, Sales Department", "ann@brightmoor.co.uk", "Brightmoor Systems"),
    ("Halvern Partners", "ann@halvern-partners.com", "Halvern Partners"),                  # agrees with the domain
    ("Halvern Ltd", "ann@gmail.com", "Halvern Ltd"),                                        # a legal suffix
    ("Halvern Ltd", "", "Halvern Ltd"),                                                     # an organisation card
    ("Orlesk Trading LLC", "ann@othercorp.com", "Orlesk Trading LLC"),
    ("Orlesk Sales Ltd", "ann@othercorp.com", "Orlesk Sales Ltd"),                          # a legal name is whole
    ("Halvern Partners", "ann@othercorp.com", ""),                                         # disagrees, no suffix
    ("Halvern Partners", "ann@gmail.com", ""),                                             # free mail is no domain
    ("Halvern", "", ""),
    ("Account Management", "ann@zentrix.com", ""),                                          # only department words
])
def test_the_wikidata_term_is_cleaned_and_sent_only_when_it_is_the_company(company, email, term):
    """6c. Found on the owner's index: "Wipro Technologies Account Energy" (a company, its account team and
    its sector) was sent as one name, matched nothing, and was remembered for ninety days."""
    card = Card(scalars={"given": "Ann", "family": "Lee", "company": company},
                fields=[Field("email", email, "work")] if email else [])
    assert wikidata.company_term(card) == term


def test_only_the_cleaned_company_name_reaches_wikidata(hub):
    cid = person(hub, email="ann@zentrix.com", company="Zentrix Technologies Account Energy")
    fake = wd_transport()
    enrich_selection(hub, _wd_settings(), [cid], transport=fake)
    searches = [parse_qs(urlsplit(u).query).get("search") for u in fake.urls]
    assert [x for x in searches if x] == [["Zentrix Technologies"]]
    other = person(hub, email="bo@othercorp.com", company="Halvern Partners")
    boom = Boom()
    assert enrich_selection(hub, _wd_settings(), [other], transport=boom) == [] and boom.calls == []


@pytest.mark.parametrize("email,url,domain", [
    ("ann@acme-widgets.co.uk", "https://www.other-site.net/about", "acme-widgets.co.uk"),   # not the company's
    ("ann@acme-widgets.co.uk", "https://shop.acme-widgets.com/", "shop.acme-widgets.com"),  # the same name
    ("ann@mail.acme-widgets.co.uk", "acme-widgets.co.uk", "acme-widgets.co.uk"),
    ("ann@gmail.com", "https://www.other-site.net/", "www.other-site.net"),                 # free mail: no work email
    ("", "https://www.other-site.net/", "www.other-site.net"),
])
def test_a_logo_comes_only_from_a_domain_that_is_the_companys(email, url, domain):
    """6d: a web address on the card is used for the logo only when it shares its registrable name with a
    work email's domain, or the card has no work email."""
    card = Card(fields=[*([Field("email", email, "work")] if email else []), Field("url", url, "work")])
    assert logo.company_domain(card) == domain


def test_a_web_address_that_is_not_the_companys_is_not_asked(hub):
    cid = person(hub, email="ann@acme-widgets.co.uk")
    hub.update(cid, _with_url(hub.get_card(cid), "https://www.other-site.net/about"))
    fake = Fake(("https://acme-widgets.co.uk/favicon.ico", HttpResponse(200, body=png())))
    out = enrich_selection(hub, _logo_settings(), [cid], transport=fake)
    assert fake.hosts == {"acme-widgets.co.uk"} and out[0].receipt == "https://acme-widgets.co.uk/favicon.ico"


def test_kept_pictures_are_capped_and_the_oldest_go_first(tmp_path, monkeypatch):
    """Order 1s 6a kept pictures with no limit, so the cache only grew (found merging, 2026-10-12)."""
    import os

    from app.contacts.enrich import remember

    monkeypatch.setattr(remember, "PHOTO_CACHE_BYTES", 2500)
    folder = tmp_path / "cache" / "enrich"
    folder.mkdir(parents=True)
    for n in range(5):
        f = folder / f"{n:040x}.png"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (1_000_000 + n, 1_000_000 + n))
    newest = folder / f"{4:040x}.png"
    remember._prune(folder, newest)
    left = sorted(p.name for p in folder.iterdir())
    assert len(left) == 2 and newest.name in left and f"{3:040x}.png" in left


@pytest.mark.parametrize("domain, social", [
    ("linkedin.com", True), ("ae.linkedin.com", True), ("linkedin.co.uk", True), ("x.com", True),
    ("youtu.be", True), ("www.github.com", True),
    ("linkedin.com.example.org", False), ("notlinkedin.com", False), ("box.com", False),
    ("acme-widgets.co.uk", False), ("xcom.example", False),
])
def test_a_social_site_is_matched_at_its_own_name_not_inside_another(domain, social):
    """CodeRabbit on #68 (2026-10-12): the check was a substring match, so `linkedin.com.example.org` -
    somebody's own site - counted as LinkedIn. A social site now matches only as itself or a subdomain."""
    from app.contacts.enrich import logo

    assert logo._social(domain) is social
