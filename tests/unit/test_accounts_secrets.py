"""Order 1k, item 1d: a sign-in secret lives in the keyring and nowhere else.

Layer: L7

No real keyring is touched (a fake backend is installed) and nothing leaves
loopback. The headline test runs a whole sign-in and then greps a database, an
`.env` and a log file for the secrets.
"""

from __future__ import annotations

import socket
import sqlite3
import threading
import urllib.parse
import urllib.request

import keyring
import keyring.backend
import keyring.errors
import pytest
from loguru import logger

from app.accounts import oauth
from app.accounts.provider import HttpResponse
from app.accounts.secrets import (
    SecretStore, account_key, client_secret_key, forget_secret, redact, register_secret,
)
from app.core.errors import AppErrorException


class FakeKeyring(keyring.backend.KeyringBackend):
    priority = 1                                          # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def get_password(self, service, username):
        return self.items.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.items[(service, username)]


class BrokenKeyring(FakeKeyring):
    def set_password(self, service, username, password):
        raise keyring.errors.KeyringError("vault is locked")

    def get_password(self, service, username):
        raise keyring.errors.KeyringError("vault is locked")


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """Nothing in this file may open a non-loopback socket."""
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host in ("127.0.0.1", "::1"), f"non-loopback connect to {host!r}"
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture()
def vault():
    before = keyring.get_keyring()
    fake = FakeKeyring()
    keyring.set_keyring(fake)
    yield fake
    keyring.set_keyring(before)


def test_put_get_delete_round_trip(vault):
    store = SecretStore()
    key = account_key("7", "refresh_token")
    assert key == "account:7:refresh_token"
    assert client_secret_key("google") == "oauth-client-secret:google"
    assert store.get(key) is None
    store.put(key, "rt-abcdefgh-12345")
    assert store.get(key) == "rt-abcdefgh-12345"
    assert vault.items[("Leasha", key)] == "rt-abcdefgh-12345"      # service name is Leasha
    store.delete(key)
    assert store.get(key) is None
    store.delete(key)                                               # deleting twice is not an error


def test_keyring_failure_is_the_registered_error(vault):
    keyring.set_keyring(BrokenKeyring())
    store = SecretStore()
    with pytest.raises(AppErrorException) as err:
        store.put("account:1:refresh_token", "whatever-secret-1")
    assert err.value.error.code == "ERR_ACCOUNT_SECRET_STORE"
    assert err.value.error.suggestion
    with pytest.raises(AppErrorException) as err:
        store.get("account:1:refresh_token")
    assert err.value.error.code == "ERR_ACCOUNT_SECRET_STORE"


def test_empty_secret_is_refused(vault):
    with pytest.raises(AppErrorException) as err:
        SecretStore().put("account:1:app_password", "")
    assert err.value.error.code == "ERR_ACCOUNT_SECRET_STORE"


def test_redact_scrubs_known_and_token_shaped_text():
    register_secret("S3cretValue-XYZ")
    try:
        assert "S3cretValue-XYZ" not in redact("failed with S3cretValue-XYZ in body")
    finally:
        forget_secret("S3cretValue-XYZ")
    out = redact("GET /cb?code=4%2F0AbCdEfGhIjK&state=abc refresh_token=1//0gAAAA access_token=ya29.xyz123")
    assert "4%2F0AbCdEfGhIjK" not in out and "1//0gAAAA" not in out and "ya29.xyz123" not in out
    assert "state=abc" in out                                       # not a secret, kept for diagnosis
    assert "code=429" in redact("server said code=429")             # a status is not a secret
    assert "abcdefgh12345678" not in redact('{"access_token": "abcdefgh12345678", "expires_in": 3599}')
    assert "3599" in redact('{"access_token": "abcdefgh12345678", "expires_in": 3599}')
    assert "abc.def-123456" not in redact("Authorization: Bearer abc.def-123456")


def test_redact_never_raises_on_odd_input():
    assert isinstance(redact(None), str)
    assert isinstance(redact(b"bytes \xff"), str)


class _Fake:
    """A token endpoint that answers like Google."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append((method, url, body))
        return HttpResponse(200, {}, (
            b'{"access_token":"ya29.ACCESS-TOKEN-VALUE","expires_in":3599,'
            b'"refresh_token":"1//REFRESH-TOKEN-VALUE","scope":"openid email"}'))


def test_no_secret_reaches_sqlite_env_or_log_after_a_sign_in(vault, tmp_path):
    """The proof item 1d asks for: run a sign-in, then grep every place a secret must not be."""
    db = tmp_path / "leasha.db"
    env = tmp_path / ".env"
    log = tmp_path / "app.log"
    conn = sqlite3.connect(db)
    conn.execute("create table accounts (id integer primary key, provider text, address text, last_error text)")
    conn.execute("insert into accounts values (1, 'google', 'me@example.com', null)")
    conn.commit()
    env.write_text("DATA_PATH=D:/x\nOAUTH_GOOGLE_CLIENT_ID=client-id-123\n")

    store = SecretStore()
    store.put(client_secret_key("google"), "GOCSPX-client-secret-value")
    config = oauth.OAuthClientConfig.from_store("google", "client-id-123", store)
    sink = logger.add(str(log), level="DEBUG", format="{message}")
    seen = {}

    def browser(url: str) -> None:                       # plays the person's browser, over loopback
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        seen["code_challenge"] = q["code_challenge"][0]

        def go():
            target = q["redirect_uri"][0] + "?" + urllib.parse.urlencode(
                {"code": "4/0AUTH-CODE-VALUE-123", "state": q["state"][0]})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            opener.open(target, timeout=5).read()

        threading.Thread(target=go, daemon=True).start()

    try:
        tokens, manager = oauth.sign_in(config, _Fake(), store, account_id=1, account="me@example.com",
                                        timeout_s=10, open_browser=browser)
        # Code that leaks on purpose, the way a careless future change would.
        logger.info(f"got tokens {tokens.access_token} / {tokens.refresh_token} / "
                    f"secret {config.client_secret} / code 4/0AUTH-CODE-VALUE-123")
        logger.error("request body was refresh_token=1//REFRESH-TOKEN-VALUE&client_secret=GOCSPX-client-secret-value")
        logger.complete()
    finally:
        logger.remove(sink)
        conn.close()

    assert store.get(account_key(1, "refresh_token")) == "1//REFRESH-TOKEN-VALUE"   # it is in the keyring
    secrets = ["1//REFRESH-TOKEN-VALUE", "ya29.ACCESS-TOKEN-VALUE", "GOCSPX-client-secret-value",
               "4/0AUTH-CODE-VALUE-123"]
    for path in (db, env, log):
        raw = path.read_bytes().decode("utf-8", "replace")
        for s in secrets:
            assert s not in raw, f"{s!r} found in {path.name}"
    assert "[secret]" in log.read_text()                 # the log did get the line, scrubbed
    for f in tmp_path.rglob("*"):                        # and nowhere else under the data folder
        if f.is_file():
            data = f.read_bytes().decode("utf-8", "replace")
            assert not any(s in data for s in secrets), f.name
