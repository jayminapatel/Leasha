"""Order 1k, items 1c and 1e: PKCE sign-in, the loopback redirect, token refresh, consent errors.

Layer: L7

The Transport is a fake; the loopback server is real but on 127.0.0.1 only.
Nothing here opens a non-loopback socket.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

import keyring
import keyring.backend
import keyring.errors
import pytest

from app.accounts import oauth
from app.accounts.oauth import (
    AppPasswordSource, LoopbackServer, OAuthClientConfig, TokenManager, TokenSet, build_auth_url,
    classify_token_error, exchange_code, generate_pkce, pkce_challenge, refresh, scopes_for,
)
from app.accounts.provider import HttpResponse
from app.accounts.secrets import SecretStore, account_key
from app.core.errors import AppErrorException


class _Vault(keyring.backend.KeyringBackend):
    priority = 1                                              # type: ignore[assignment]

    def __init__(self):
        super().__init__()
        self.items = {}

    def set_password(self, s, u, p):
        self.items[(s, u)] = p

    def get_password(self, s, u):
        return self.items.get((s, u))

    def delete_password(self, s, u):
        if (s, u) not in self.items:
            raise keyring.errors.PasswordDeleteError("x")
        del self.items[(s, u)]


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host in ("127.0.0.1", "::1"), f"non-loopback connect to {host!r}"
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture()
def store():
    before = keyring.get_keyring()
    keyring.set_keyring(_Vault())
    yield SecretStore()
    keyring.set_keyring(before)


class FakeTransport:
    """Answers token-endpoint calls from a script; records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.sent.append((method, url, dict(urllib.parse.parse_qsl((body or b"").decode()))))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return r


def _ok(access="AT1", refresh_token=None, expires_in=3600, **extra):
    d = {"access_token": access, "expires_in": expires_in, **extra}
    if refresh_token:
        d["refresh_token"] = refresh_token
    return HttpResponse(200, {}, json.dumps(d).encode())


def _err(status, error, desc=""):
    return HttpResponse(status, {}, json.dumps({"error": error, "error_description": desc}).encode())


GOOGLE = OAuthClientConfig("google", "gid.apps.example", "gsecret-value")
MS = OAuthClientConfig("microsoft", "ms-client-id")


# ---- PKCE and the sign-in URL ----------------------------------------------

def test_pkce_matches_the_rfc7636_vector():
    # RFC 7636 appendix B.
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_pkce_is_valid_and_fresh():
    v1, c1 = generate_pkce()
    v2, _ = generate_pkce()
    assert 43 <= len(v1) <= 128 and set(v1) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert c1 == pkce_challenge(v1) and v1 != v2 and "=" not in c1


def test_scopes():
    assert scopes_for("google") == ["openid", "email", "https://www.googleapis.com/auth/contacts"]
    assert "https://www.googleapis.com/auth/gmail.readonly" in scopes_for("google", mail=True)
    assert "https://www.googleapis.com/auth/gmail.readonly" not in scopes_for("google")
    ms = scopes_for("microsoft", mail=True)
    assert set(ms) == {"Contacts.ReadWrite", "Mail.Read", "offline_access", "User.Read"}
    assert "Mail.Read" not in scopes_for("microsoft")


def test_auth_url_carries_pkce_state_and_the_loopback_redirect():
    url = build_auth_url(GOOGLE, redirect_uri="http://127.0.0.1:5555/", state="st8", challenge="CHAL",
                         scopes=scopes_for("google", mail=True), login_hint="me@x.com")
    p = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    assert (p.scheme, p.netloc, p.path) == ("https", "accounts.google.com", "/o/oauth2/v2/auth")
    assert q["code_challenge"] == "CHAL" and q["code_challenge_method"] == "S256" and q["state"] == "st8"
    assert q["redirect_uri"] == "http://127.0.0.1:5555/" and q["response_type"] == "code"
    assert q["access_type"] == "offline" and q["client_id"] == "gid.apps.example" and q["login_hint"] == "me@x.com"
    assert "client_secret" not in q                                 # never in a URL
    mq = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
        build_auth_url(MS, redirect_uri="http://127.0.0.1:1/", state="s", challenge="c", scopes=["User.Read"])).query))
    assert mq["client_id"] == "ms-client-id"


def test_microsoft_uses_the_common_tenant():
    url = build_auth_url(MS, redirect_uri="http://127.0.0.1:1/", state="s", challenge="c", scopes=["User.Read"])
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")


def test_missing_client_id_or_google_secret_is_not_configured():
    for cfg in (OAuthClientConfig("google"), OAuthClientConfig("google", "id", ""), OAuthClientConfig("microsoft", " ")):
        with pytest.raises(AppErrorException) as err:
            build_auth_url(cfg, redirect_uri="http://127.0.0.1:1/", state="s", challenge="c", scopes=[])
        assert err.value.error.code == "ERR_ACCOUNT_NOT_CONFIGURED"
        assert "Settings > Accounts" in err.value.error.suggestion
    t = FakeTransport(_ok())
    with pytest.raises(AppErrorException):
        refresh(t, OAuthClientConfig("google"), "rt")
    assert t.sent == []                                             # nothing sent without a client ID


# ---- the loopback server ----------------------------------------------------

def _get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0                     # the server already closed: a late favicon request, harmless


def _later(url):
    t = threading.Thread(target=_get, args=(url,), daemon=True)
    t.start()
    return t


def test_loopback_server_binds_127_0_0_1_only():
    with LoopbackServer("s") as srv:
        assert srv._httpd.server_address[0] == "127.0.0.1"
        assert srv.redirect_uri == f"http://127.0.0.1:{srv.port}/" and srv.port > 0


def test_loopback_accepts_a_good_state_and_ignores_a_favicon():
    with LoopbackServer("good-state") as srv:
        _later(srv.redirect_uri + "favicon.ico")
        _later(srv.redirect_uri + "?" + urllib.parse.urlencode({"code": "the-code", "state": "good-state"}))
        assert srv.wait(5) == "the-code"


def test_loopback_rejects_a_bad_state():
    with LoopbackServer("good-state") as srv:
        t = _later(srv.redirect_uri + "?code=stolen&state=forged")
        with pytest.raises(AppErrorException) as err:
            srv.wait(5, account="Home")
        t.join(5)
        assert err.value.error.code == "ERR_ACCOUNT_REJECTED" and "stolen" not in str(err.value.error.details)


def test_loopback_times_out_and_can_be_cancelled():
    ticks = iter(range(0, 10_000))
    with LoopbackServer("s", clock=lambda: float(next(ticks))) as srv:
        with pytest.raises(AppErrorException) as err:
            srv.wait(3)
        assert err.value.error.code == "ERR_ACCOUNT_REJECTED" and "in time" in err.value.error.details
    with LoopbackServer("s") as srv:
        srv.cancel()
        with pytest.raises(AppErrorException):
            srv.wait(30)


def test_loopback_provider_error_in_the_redirect_is_classified():
    with LoopbackServer("s") as srv:
        _later(srv.redirect_uri + "?" + urllib.parse.urlencode(
            {"error": "access_denied", "error_description": "AADSTS65001: The user has not consented", "state": "s"}))
        with pytest.raises(AppErrorException) as err:
            srv.wait(5, account="Work", provider="microsoft")
    assert err.value.error.code == "ERR_ACCOUNT_ADMIN_CONSENT" and err.value.error.context["account"] == "Work"


# ---- code exchange and refresh ---------------------------------------------

def test_exchange_code_sends_the_verifier_and_parses_the_answer():
    t = FakeTransport(_ok("AT", "RT", scope="openid email", id_token=_jwt({"email": "me@gmail.com"})))
    tok = exchange_code(t, GOOGLE, code="C", verifier="V", redirect_uri="http://127.0.0.1:9/",
                        clock=lambda: 1000.0)
    method, url, form = t.sent[0]
    assert (method, url) == ("POST", "https://oauth2.googleapis.com/token")
    assert form["grant_type"] == "authorization_code" and form["code_verifier"] == "V" and form["code"] == "C"
    assert form["client_id"] == "gid.apps.example" and form["client_secret"] == "gsecret-value"
    assert (tok.access_token, tok.refresh_token, tok.email, tok.expires_at) == ("AT", "RT", "me@gmail.com", 4600.0)


def _jwt(payload):
    import base64
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"h.{seg}.s"


def test_microsoft_exchange_has_no_client_secret():
    t = FakeTransport(_ok("AT", "RT"))
    exchange_code(t, MS, code="C", verifier="V", redirect_uri="http://127.0.0.1:9/")
    assert t.sent[0][1] == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert "client_secret" not in t.sent[0][2]


def test_token_manager_refreshes_when_within_60_seconds_of_expiry(store):
    now = [1000.0]
    t = FakeTransport(_ok("AT2", expires_in=3600))
    store.put(account_key("a1", "refresh_token"), "RT-stored-1")
    tm = TokenManager("a1", store, t, GOOGLE, clock=lambda: now[0])
    tm.adopt(TokenSet("AT1", expires_at=1000.0 + 3600, refresh_token="RT-stored-1"))
    assert tm() == "AT1" and t.sent == []                          # fresh: no call
    now[0] = 1000.0 + 3600 - 61
    assert tm() == "AT1" and t.sent == []                          # 61 s left: still no call
    now[0] = 1000.0 + 3600 - 59
    assert tm() == "AT2" and len(t.sent) == 1                      # inside 60 s: refreshed
    assert t.sent[0][2]["grant_type"] == "refresh_token" and t.sent[0][2]["refresh_token"] == "RT-stored-1"
    assert tm() == "AT2" and len(t.sent) == 1                      # and cached again


def test_a_rotated_refresh_token_is_persisted(store):
    t = FakeTransport(_ok("AT2", "RT-rotated"), _ok("AT3", None))
    store.put(account_key("m", "refresh_token"), "RT-old-value")
    now = [0.0]
    tm = TokenManager("m", store, t, MS, clock=lambda: now[0])
    assert tm() == "AT2"
    assert store.get(account_key("m", "refresh_token")) == "RT-rotated"
    now[0] = 10_000
    assert tm() == "AT3"
    assert t.sent[1][2]["refresh_token"] == "RT-rotated"            # the new one is used next time
    assert store.get(account_key("m", "refresh_token")) == "RT-rotated"   # none sent -> keep the old


def test_invalid_grant_means_the_sign_in_expired(store):
    store.put(account_key("g", "refresh_token"), "RT-dead-value")
    tm = TokenManager("g", store, FakeTransport(_err(400, "invalid_grant", "Token has been expired or revoked.")),
                      GOOGLE, account_label="Home Gmail")
    with pytest.raises(AppErrorException) as err:
        tm()
    e = err.value.error
    assert e.code == "ERR_ACCOUNT_SIGNIN_EXPIRED" and "Home Gmail" in e.message and "Reconnect" in e.suggestion


def test_no_stored_refresh_token_means_signed_out(store):
    t = FakeTransport(_ok())
    with pytest.raises(AppErrorException) as err:
        TokenManager("nobody", store, t, GOOGLE)()
    assert err.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED" and t.sent == []


@pytest.mark.parametrize("error,desc", [
    ("invalid_grant", "AADSTS65001: The user or administrator has not consented to use the application"),
    ("access_denied", "AADSTS90094: The grant requires admin permission."),
    ("admin_consent_required", ""),
    ("interaction_required", "AADSTS90094 admin must approve"),
    ("invalid_request", "consent_required for this tenant"),
    ("ACCESS_DENIED", "aadsts65001 lower case"),
])
def test_admin_consent_refusals_become_the_admin_consent_error(store, error, desc):
    store.put(account_key("w", "refresh_token"), "RT-work-value")
    tm = TokenManager("w", store, FakeTransport(_err(400, error, desc)), MS, account_label="Work")
    with pytest.raises(AppErrorException) as err:
        tm()
    e = err.value.error
    assert e.code == "ERR_ACCOUNT_ADMIN_CONSENT" and e.context["account"] == "Work"
    assert "Work" in e.message and "IT administrator" in e.suggestion       # says what to ask IT for (rule 2)


def test_other_failures_are_rejected_or_rate_limited_never_raw():
    e = classify_token_error({"error": "invalid_client"}, account="A", provider="google", status=401).error
    assert e.code == "ERR_ACCOUNT_REJECTED"
    e = classify_token_error(None, account="A", provider="google", status=503).error
    assert e.code == "ERR_PROVIDER_LIMIT"
    e = classify_token_error("<html>oops</html>", account="A", provider="google", status=500).error
    assert e.code == "ERR_ACCOUNT_REJECTED"


def test_a_non_json_success_body_is_rejected_not_crashed():
    with pytest.raises(AppErrorException) as err:
        refresh(FakeTransport(HttpResponse(200, {}, b"<html>captive portal</html>")), GOOGLE, "rt")
    assert err.value.error.code == "ERR_ACCOUNT_REJECTED"


def test_invalidate_forces_a_refresh(store):
    t = FakeTransport(_ok("AT-new"))
    store.put(account_key("i", "refresh_token"), "RT-i-value")
    tm = TokenManager("i", store, t, GOOGLE, clock=lambda: 0.0)
    tm.adopt(TokenSet("AT-old", 99999.0))
    assert tm() == "AT-old"
    tm.invalidate()
    assert tm() == "AT-new"


def test_app_password_source(store):
    store.put("account:ic:app_password", "abcd-efgh-ijkl-mnop")
    assert AppPasswordSource(store, "account:ic:app_password")() == "abcd-efgh-ijkl-mnop"
    with pytest.raises(AppErrorException) as err:
        AppPasswordSource(store, "account:none:app_password", account="iCloud")()
    assert err.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"


def test_token_manager_is_a_token_source(store):
    from app.accounts.provider import TokenSource  # noqa: F401 - a Callable alias; calling it is the contract
    tm = TokenManager("x", store, FakeTransport(_ok("A")), GOOGLE)
    assert callable(tm) and callable(tm.as_source())
