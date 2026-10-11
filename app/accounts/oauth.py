"""Signing in to Google and Microsoft the way a desktop app does, and staying signed in.

Layer: L7

Order 1k, items 1c and 1e. The authorization-code flow with PKCE (S256) and a
loopback redirect (`http://127.0.0.1:<free port>/`), built from small pure
pieces that take the `Transport` as an argument so the tests use a fake and no
test opens a connection:

* `generate_pkce`, `build_auth_url` - what the browser is sent to.
* `LoopbackServer` - waits on 127.0.0.1 only, checks the `state`, times out.
* `exchange_code`, `refresh` - the two token-endpoint calls.
* `TokenManager` - a `TokenSource` that refreshes a minute early, keeps a
  rotated refresh token, and turns the failures into the errors the person can
  act on.
* `AppPasswordSource` - the same shape for Apple and any IMAP/CardDAV server,
  which have no OAuth.

**Leasha ships no client ID (D2).** Each person registers their own Google
Cloud and Microsoft Entra app and pastes the client ID (and, for Google, the
client secret) into Settings > Accounts. A missing one is
`ERR_ACCOUNT_NOT_CONFIGURED`, which says how to make one.

**Google's "client secret" is not a secret in the usual sense** - Google issues
one for desktop clients and requires it at the token endpoint, but says it
cannot be kept confidential in an installed app. We still keep it in the
`SecretStore` like any other, never in a file (rule 11).

**What is never stored.** The access token lives in memory only; it is cheap to
get again. Only the refresh token is persisted, and only in the `SecretStore`.

UNVERIFIED on Windows / against the live providers: every call here was tested
against fakes. Real sign-ins, Google's "unverified app" screen and a work
tenant's consent prompt are the owner's to check (acceptance A-checks).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets as _stdlib_secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

from app.accounts.provider import HttpResponse, TokenSource, Transport
from app.accounts.secrets import SecretStore, account_key, client_secret_key, redact, register_secret
from app.core.errors import AppErrorException, make_error

__all__ = [
    "GOOGLE", "MICROSOFT", "REFRESH_SKEW_S", "OAuthClientConfig", "TokenSet",
    "generate_pkce", "pkce_challenge", "scopes_for", "build_auth_url", "LoopbackServer",
    "exchange_code", "refresh", "classify_token_error", "TokenManager", "AppPasswordSource",
    "sign_in",
]

GOOGLE = "google"
MICROSOFT = "microsoft"

#: A token this close to expiry is refreshed first. A sync request that starts
#: with 30 s left can finish after the token has died; a minute is long enough
#: for one page and short enough not to refresh needlessly.
REFRESH_SKEW_S = 60

_ENDPOINTS = {
    GOOGLE: {
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
    },
    MICROSOFT: {
        # 'common' accepts personal and work/school accounts in one registration.
        "auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    },
}

#: Markers of "your administrator has not approved this app", matched
#: case-insensitively in `error` and `error_description`. Microsoft reports
#: AADSTS65001 under the error code `invalid_grant`, so this check must run
#: before the invalid_grant (signed out) mapping.
_ADMIN_CONSENT_MARKERS = ("aadsts65001", "aadsts90094", "admin_consent_required", "consent_required")


# --------------------------------------------------------------------------
# Configuration, PKCE, URLs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OAuthClientConfig:
    """One person's app registration for one provider."""

    provider: str
    client_id: str = ""
    client_secret: str = ""            # Google only

    def validate(self) -> "OAuthClientConfig":
        """Raise `ERR_ACCOUNT_NOT_CONFIGURED` unless this can sign anyone in."""
        if self.provider not in _ENDPOINTS:
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_NOT_CONFIGURED", "accounts.oauth", provider=self.provider,
                details=f"{self.provider!r} does not use browser sign-in"))
        missing = []
        if not self.client_id.strip():
            missing.append("client ID")
        if self.provider == GOOGLE and not self.client_secret.strip():
            missing.append("client secret")
        if missing:
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_NOT_CONFIGURED", "accounts.oauth", provider=self.provider.capitalize(),
                details="missing: " + ", ".join(missing)))
        return self

    @classmethod
    def from_store(cls, provider: str, client_id: str, secrets: SecretStore) -> "OAuthClientConfig":
        """Build from the client ID (a setting) and the client secret (from the SecretStore)."""
        return cls(provider, client_id, secrets.get(client_secret_key(provider)) or "")


@dataclass
class TokenSet:
    access_token: str
    expires_at: float                  # epoch seconds
    refresh_token: str = ""            # empty when the server did not send a (new) one
    scope: str = ""
    email: str = ""                    # from a Google id_token, when asked for (openid email)


def pkce_challenge(verifier: str) -> str:
    """S256: base64url(SHA-256(verifier)) without padding (RFC 7636 4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """A fresh (verifier, challenge). 32 random bytes -> a 43-character verifier."""
    verifier = base64.urlsafe_b64encode(_stdlib_secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return verifier, pkce_challenge(verifier)


def scopes_for(provider: str, *, mail: bool = False, contacts: bool = True) -> list[str]:
    """The permissions to ask for. Mail is asked for only when mail is switched on."""
    if provider == GOOGLE:
        scopes = ["openid", "email"]
        if contacts:
            scopes.append("https://www.googleapis.com/auth/contacts")
        if mail:
            scopes.append("https://www.googleapis.com/auth/gmail.readonly")
        return scopes
    if provider == MICROSOFT:
        scopes = ["offline_access", "User.Read"]
        if contacts:
            scopes.append("Contacts.ReadWrite")
        if mail:
            scopes.append("Mail.Read")
        return scopes
    raise AppErrorException(make_error(
        "ERR_ACCOUNT_NOT_CONFIGURED", "accounts.oauth", provider=provider,
        details=f"{provider!r} does not use browser sign-in"))


def build_auth_url(config: OAuthClientConfig, *, redirect_uri: str, state: str, challenge: str,
                   scopes: list[str], login_hint: str = "") -> str:
    config.validate()
    params = {
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if config.provider == GOOGLE:
        # offline + consent: without both, Google may omit the refresh token on
        # a second sign-in and the account would silently stop syncing in an hour.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    else:
        params["prompt"] = "select_account"
    if login_hint:
        params["login_hint"] = login_hint
    return _ENDPOINTS[config.provider]["auth"] + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


# --------------------------------------------------------------------------
# Error mapping (1e)
# --------------------------------------------------------------------------

def classify_token_error(payload: object, *, account: str, provider: str, status: int = 400,
                         component: str = "accounts.oauth") -> AppErrorException:
    """Turn a token-endpoint (or redirect) error into the `AppError` the person can act on."""
    err = desc = ""
    if isinstance(payload, dict):
        raw = payload.get("error")
        # Microsoft puts a string, but Google's other APIs nest an object.
        err = str(raw.get("status") or raw.get("message") or "") if isinstance(raw, dict) else str(raw or "")
        desc = str(payload.get("error_description") or "")
    blob = f"{err} {desc}".lower()
    safe = redact(f"{err}: {desc}".strip(": "))[:300]
    if any(m in blob for m in _ADMIN_CONSENT_MARKERS):
        return AppErrorException(make_error(
            "ERR_ACCOUNT_ADMIN_CONSENT", component, account=account, details=safe, provider=provider))
    if err == "invalid_grant" or "invalid_grant" in blob:
        return AppErrorException(make_error(
            "ERR_ACCOUNT_SIGNIN_EXPIRED", component, account=account, details=safe, provider=provider))
    if status in (429, 503):
        return AppErrorException(make_error(
            "ERR_PROVIDER_LIMIT", component, account=account, reason=f"HTTP {status}", details=safe))
    return AppErrorException(make_error(
        "ERR_ACCOUNT_REJECTED", component, account=account,
        reason=(err or f"HTTP {status}")[:80], details=safe, provider=provider))


# --------------------------------------------------------------------------
# The loopback redirect
# --------------------------------------------------------------------------

_DONE_PAGE = (b"<!doctype html><html><head><meta charset='utf-8'><title>Leasha</title></head>"
              b"<body style='font-family:sans-serif;margin:3em'><h2>You are signed in.</h2>"
              b"<p>You can close this window and go back to Leasha.</p></body></html>")
_FAIL_PAGE = (b"<!doctype html><html><head><meta charset='utf-8'><title>Leasha</title></head>"
              b"<body style='font-family:sans-serif;margin:3em'><h2>Sign-in did not finish.</h2>"
              b"<p>Go back to Leasha for what to do next.</p></body></html>")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:                      # noqa: N802 - http.server's name
        query = urllib.parse.urlsplit(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        # A browser also asks for /favicon.ico; that is not the redirect.
        if not ({"code", "error"} & params.keys()):
            self.send_response(404)
            self.end_headers()
            return
        ok = "code" in params and "error" not in params and params.get("state") == self.server.expected_state
        page = _DONE_PAGE if ok else _FAIL_PAGE
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)
        self.server.captured = params

    def log_message(self, format, *args) -> None:   # noqa: A002, ANN001
        # The default writes the request line - which carries the one-time code -
        # to stderr. Silence it entirely.
        return


class _Server(http.server.HTTPServer):
    expected_state = ""
    captured: Optional[dict] = None


class LoopbackServer:
    """Waits for the browser's redirect on 127.0.0.1 and nowhere else.

    Binds port 0 so the OS picks a free port; `redirect_uri` is known before the
    browser is opened. `wait()` blocks, so call it from a worker thread (rule 5).
    """

    def __init__(self, state: str, *, clock: Callable[[], float] = time.monotonic,
                 redirect_host: str = "127.0.0.1") -> None:
        self._state = state
        self._clock = clock
        # What the browser is told to come back to. The socket below is bound to 127.0.0.1
        # whatever this says: `localhost` is a name for the same machine, not another listener.
        self._redirect_host = redirect_host
        # 127.0.0.1, never 0.0.0.0: nothing else on the network may answer or
        # inject a code.
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.expected_state = state
        self._httpd.timeout = 0.2
        self._cancel = threading.Event()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://{self._redirect_host}:{self.port}/"

    def cancel(self) -> None:
        """Stop waiting (the person closed the dialog). Safe from any thread."""
        self._cancel.set()

    def close(self) -> None:
        try:
            self._httpd.server_close()
        except Exception:                           # noqa: BLE001
            pass

    def __enter__(self) -> "LoopbackServer":
        return self

    def __exit__(self, *exc) -> None:               # noqa: ANN002
        self.close()

    def wait(self, timeout_s: float = 300.0, *, account: str = "the account",
             provider: str = "") -> str:
        """The authorization code, once the browser comes back with a matching state."""
        deadline = self._clock() + timeout_s
        while self._httpd.captured is None:
            if self._cancel.is_set():
                self._fail(account, "sign-in was cancelled")
            if self._clock() >= deadline:
                self._fail(account, "the browser sign-in was not finished in time")
            self._httpd.handle_request()            # returns after .timeout at the latest
        params = self._httpd.captured
        if "error" in params:
            raise classify_token_error(params, account=account, provider=provider, status=400)
        # compare_digest: the state is not a secret but there is no reason to leak its length of match.
        if not _stdlib_secrets.compare_digest(params.get("state", ""), self._state):
            self._fail(account, "the sign-in answer did not match this request")
        register_secret(params["code"])
        return params["code"]

    @staticmethod
    def _fail(account: str, why: str) -> None:
        raise AppErrorException(make_error(
            "ERR_ACCOUNT_REJECTED", "accounts.oauth", account=account, reason=why, details=why))


# --------------------------------------------------------------------------
# Token endpoint
# --------------------------------------------------------------------------

def _post_token(transport: Transport, config: OAuthClientConfig, form: dict[str, str], *,
                account: str, clock: Callable[[], float]) -> TokenSet:
    config.validate()
    register_secret(config.client_secret)
    form = dict(form, client_id=config.client_id)
    if config.client_secret:
        form["client_secret"] = config.client_secret
    resp: HttpResponse = transport.request(
        "POST", _ENDPOINTS[config.provider]["token"],
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body=urllib.parse.urlencode(form).encode("ascii"))
    try:
        data = resp.json()
    except (ValueError, UnicodeDecodeError):
        data = None
    if resp.status != 200 or not isinstance(data, dict) or "access_token" not in data:
        raise classify_token_error(data, account=account, provider=config.provider, status=resp.status)
    # Access tokens and codes never pass through the SecretStore (they are not
    # kept), so tell the redactor about them here or a stray log line leaks one.
    register_secret(str(data["access_token"]))
    register_secret(str(data.get("refresh_token") or ""))
    return TokenSet(
        access_token=str(data["access_token"]),
        expires_at=clock() + float(data.get("expires_in", 3600)),
        refresh_token=str(data.get("refresh_token") or ""),
        scope=str(data.get("scope") or ""),
        email=_email_from_id_token(data.get("id_token")),
    )


def _email_from_id_token(token: object) -> str:
    """The address in a Google id_token. Unverified decode, which is fine here:
    the token came straight from Google over TLS in answer to our own request,
    and it is used to label the account, never to authorise anything."""
    if not isinstance(token, str) or token.count(".") != 2:
        return ""
    try:
        seg = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        return str(payload.get("email") or "")
    except Exception:                               # noqa: BLE001
        return ""


def exchange_code(transport: Transport, config: OAuthClientConfig, *, code: str, verifier: str,
                  redirect_uri: str, account: str = "the account",
                  clock: Callable[[], float] = time.time) -> TokenSet:
    return _post_token(transport, config, {
        "grant_type": "authorization_code", "code": code,
        "code_verifier": verifier, "redirect_uri": redirect_uri,
    }, account=account, clock=clock)


def refresh(transport: Transport, config: OAuthClientConfig, refresh_token: str, *,
            account: str = "the account", clock: Callable[[], float] = time.time) -> TokenSet:
    return _post_token(transport, config, {
        "grant_type": "refresh_token", "refresh_token": refresh_token,
    }, account=account, clock=clock)


# --------------------------------------------------------------------------
# Staying signed in
# --------------------------------------------------------------------------

class TokenManager:
    """A `TokenSource` for one OAuth account.

    Call the instance (or pass it where a `TokenSource` is wanted) for an access
    token valid for at least `REFRESH_SKEW_S` more seconds. Thread-safe: the
    worker and a "Test connection" can ask at once and only one refresh runs.
    """

    def __init__(self, account_id: object, secrets: SecretStore, transport: Transport,
                 config: OAuthClientConfig, *, account_label: str = "",
                 clock: Callable[[], float] = time.time) -> None:
        self._key = account_key(account_id, "refresh_token")
        self._secrets = secrets
        self._transport = transport
        self._config = config
        self._label = account_label or str(account_id)
        self._clock = clock
        self._access = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def expires_at(self) -> float:
        """When the current access token dies (0 if none yet). For the account row."""
        return self._expires_at

    def adopt(self, tokens: TokenSet) -> None:
        """Take the result of a sign-in: keep the refresh token, hold the access token."""
        with self._lock:
            self._remember(tokens)

    def invalidate(self) -> None:
        """Forget the cached access token (a connector got a 401): the next call refreshes."""
        with self._lock:
            self._access, self._expires_at = "", 0.0

    def _remember(self, tokens: TokenSet) -> None:
        if tokens.refresh_token:
            # Providers may rotate the refresh token on every refresh (Microsoft
            # does). Persisting the new one is what keeps the account signed in;
            # the old one may already be dead.
            self._secrets.put(self._key, tokens.refresh_token)
        self._access, self._expires_at = tokens.access_token, tokens.expires_at

    def __call__(self) -> str:
        with self._lock:
            if self._access and self._expires_at - self._clock() > REFRESH_SKEW_S:
                return self._access
            stored = self._secrets.get(self._key)
            if not stored:
                raise AppErrorException(make_error(
                    "ERR_ACCOUNT_SIGNIN_EXPIRED", "accounts.oauth", account=self._label,
                    details="no refresh token is stored for this account"))
            tokens = refresh(self._transport, self._config, stored, account=self._label, clock=self._clock)
            self._remember(tokens)
            return self._access

    def as_source(self) -> TokenSource:
        return self.__call__


class AppPasswordSource:
    """A `TokenSource` that returns an app password (Apple, any IMAP/CardDAV server)."""

    def __init__(self, secrets: SecretStore, key: str, *, account: str = "the account") -> None:
        self._secrets = secrets
        self._key = key
        self._account = account

    def __call__(self) -> str:
        value = self._secrets.get(self._key)
        if not value:
            # Same fix as an expired OAuth sign-in: Reconnect, i.e. type it again.
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_SIGNIN_EXPIRED", "accounts.oauth", account=self._account,
                details="no app password is stored for this account"))
        return value


# --------------------------------------------------------------------------
# The whole sign-in, for the CLI and the window
# --------------------------------------------------------------------------

def sign_in(config: OAuthClientConfig, transport: Transport, secrets: SecretStore, *,
            account_id: object, mail: bool = False, contacts: bool = True, login_hint: str = "",
            account: str = "the account", timeout_s: float = 300.0,
            open_browser: Callable[[str], object] = webbrowser.open,
            on_url: Optional[Callable[[str], None]] = None,
            clock: Callable[[], float] = time.time) -> tuple[TokenSet, "TokenManager"]:
    """Run the browser sign-in end to end; keep the refresh token; return a ready `TokenManager`.

    Blocks until the person finishes or `timeout_s` passes: call it from a worker.
    `on_url` is given the sign-in address so the window can show it when the
    browser cannot be opened.
    """
    config.validate()                                   # before a port is bound or a browser opened
    verifier, challenge = generate_pkce()
    register_secret(verifier)
    state = _stdlib_secrets.token_urlsafe(24)
    # Microsoft ignores the port only when the host is `localhost` (Microsoft Learn, "Redirect URI
    # best practices", Localhost exceptions): a registered `http://localhost` never matches
    # `http://127.0.0.1:<port>/` and sign-in fails with invalid_request on redirect_uri. Google
    # takes either, so it keeps the literal address.
    host = "localhost" if config.provider == MICROSOFT else "127.0.0.1"
    with LoopbackServer(state, redirect_host=host) as server:
        url = build_auth_url(config, redirect_uri=server.redirect_uri, state=state, challenge=challenge,
                             scopes=scopes_for(config.provider, mail=mail, contacts=contacts),
                             login_hint=login_hint)
        if on_url:
            on_url(url)
        open_browser(url)
        code = server.wait(timeout_s, account=account, provider=config.provider)
        redirect_uri = server.redirect_uri
    tokens = exchange_code(transport, config, code=code, verifier=verifier,
                           redirect_uri=redirect_uri, account=account, clock=clock)
    manager = TokenManager(account_id, secrets, transport, config, account_label=account, clock=clock)
    manager.adopt(tokens)
    return tokens, manager
