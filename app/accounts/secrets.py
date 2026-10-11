"""Where a sign-in secret lives: the operating system's keyring, and nowhere else.

Layer: L7

Order 1k, item 1d. A refresh token or an app password is the key to a person's
mail and address book, so this module is the only place in Leasha allowed to
hold one at rest. On Windows the `keyring` library puts it in Credential
Manager (on macOS, the Keychain). It is never written to SQLite, `.env`, a log
or a crash report, and `tests/unit/test_accounts_secrets.py` greps a database
and a log after a sign-in to prove it.

**Keys.** One entry per secret under the service name `Leasha`:
`account:<id>:refresh_token`, `account:<id>:app_password`,
`oauth-client-secret:google`. Use `account_key()` rather than building the
string by hand so a typo cannot create a second, orphaned entry.

**Redaction is a second line of defence, not the first.** The first is not
logging a secret at all. But a secret turns up in places nobody planned for -
an exception message that quotes a request body, a URL with `?code=` on it, a
library that logs a header - so every value that passes through `SecretStore`
is remembered in memory (never on disk) and `redact()` scrubs it, plus anything
shaped like a token or a password parameter, from a piece of text.
`install_log_redaction()` hooks that into loguru so a line is scrubbed on its
way to every sink: the console, the file, the errors file and the debug pane.

**Windows limit worth knowing.** Credential Manager stores a credential blob of
at most 2560 bytes. Google and Microsoft refresh tokens are far shorter, but
`put` still reports a failure as `ERR_ACCOUNT_SECRET_STORE` rather than letting
a library error through (rule 2).
"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional

from app.core.errors import AppErrorException, make_error

__all__ = [
    "SERVICE", "SecretStore", "account_key", "client_secret_key",
    "register_secret", "forget_secret", "redact", "install_log_redaction",
]

#: The service name every entry is filed under in Credential Manager. Fixed:
#: a person looking for "what does Leasha keep" opens Credential Manager and
#: searches this word.
SERVICE = "Leasha"

#: A value shorter than this is not remembered for redaction. Replacing every
#: occurrence of a 3-character string would shred ordinary log text, and no
#: real token or app password is that short.
_MIN_REDACT_LEN = 6

_MASK = "[secret]"

# Known secret values, held in memory only, never persisted anywhere.
_known: set[str] = set()
_lock = threading.Lock()

# Things that look like a secret even when we were never told its value:
# name=value pairs in a URL or form body, "name": "value" in JSON, and a
# bearer/basic Authorization header. The *name* is kept so the line still says
# what was there.
_NAMES = (r"refresh_token|access_token|id_token|client_secret|code_verifier|"
          r"password|app_password|authorization_code")
# `code=` needs a long value: "code=429" in an error line is a status, not a secret.
_PAIR = re.compile(r"(?i)\b(" + _NAMES + r")=([^&\s\"']+)|\b(code)=([^&\s\"']{8,})")
_JSON_PAIR = re.compile(r'(?i)("(?:' + _NAMES + r')"\s*:\s*")([^"]*)(")')
_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")


def account_key(account_id: Any, kind: str) -> str:
    """The key for one account's secret: `kind` is `refresh_token` or `app_password`."""
    return f"account:{account_id}:{kind}"


def client_secret_key(provider: str) -> str:
    """The key for a provider's OAuth client secret (Google's desktop client has one)."""
    return f"oauth-client-secret:{provider}"


def register_secret(value: Optional[str]) -> None:
    """Remember a secret so `redact` can scrub it. Memory only."""
    if value and len(value) >= _MIN_REDACT_LEN:
        with _lock:
            _known.add(value)


def forget_secret(value: Optional[str]) -> None:
    with _lock:
        _known.discard(value or "")


def redact(text: Any) -> str:
    """`text` with every known secret, and anything token-shaped, replaced.

    Never raises: it sits on the error and logging paths, and a redactor that
    throws would take the diagnosis with it.
    """
    try:
        out = str(text)
        with _lock:
            known = sorted(_known, key=len, reverse=True)   # longest first: a prefix must not leave a tail
        for secret in known:
            if secret in out:
                out = out.replace(secret, _MASK)
        out = _PAIR.sub(lambda m: f"{m.group(1) or m.group(3)}={_MASK}", out)
        out = _JSON_PAIR.sub(lambda m: f"{m.group(1)}{_MASK}{m.group(3)}", out)
        out = _AUTH.sub(lambda m: f"{m.group(1)} {_MASK}", out)
        return out
    except Exception:                                  # noqa: BLE001
        return _MASK


_installed = False


def install_log_redaction() -> None:
    """Scrub every loguru record before any sink sees it. Idempotent.

    Uses loguru's patcher, which `logger.remove()` and `setup_logging(force=True)`
    do not reset, so it survives the logging being set up again. The message is
    rewritten after formatting arguments are applied, which is what the sinks
    print; extra fields are scrubbed too.
    """
    global _installed
    if _installed:
        return
    from loguru import logger  # noqa: PLC0415 - keeps this module importable without it

    def _patch(record: dict) -> None:
        try:
            record["message"] = redact(record["message"])
            for name, value in list(record["extra"].items()):
                if isinstance(value, str):
                    record["extra"][name] = redact(value)
            exc = record.get("exception")
            if exc is not None and exc.value is not None and exc.value.args:
                exc.value.args = tuple(redact(a) if isinstance(a, str) else a for a in exc.value.args)
        except Exception:                              # noqa: BLE001 - a patcher must never raise
            return

    logger.configure(patcher=_patch)
    _installed = True


class SecretStore:
    """put / get / delete a secret by key. Backed by the `keyring` library.

    `backend` is for tests and for a person with a non-default keyring; left
    as None it uses whichever backend `keyring` selected (Credential Manager on
    Windows).
    """

    def __init__(self, service: str = SERVICE, backend: Optional[Any] = None) -> None:
        self._service = service
        self._backend = backend
        # Wiring the redactor here means every code path that can hold a secret
        # also has the log scrubbed, with nobody having to remember to call it.
        install_log_redaction()

    # -- the keyring calls, in one place so each failure maps the same way ----

    def _call(self, action: str, key: str, fn: Any, *args: Any) -> Any:
        try:
            return fn(self._service, key, *args)
        except AppErrorException:
            raise
        except Exception as exc:                       # noqa: BLE001 - keyring backends raise anything
            # The keyring exception text can quote the key but never the value;
            # redact anyway so a backend that does is not our leak.
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_SECRET_STORE", "accounts.secrets",
                details=redact(f"{action} {key!r}: {type(exc).__name__}: {exc}"),
                reason=f"{action} failed",
            )) from None

    def _api(self) -> Any:
        if self._backend is not None:
            return self._backend
        import keyring  # noqa: PLC0415 - heavy import on some platforms; only when used
        return keyring

    def put(self, key: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_SECRET_STORE", "accounts.secrets",
                details=f"refused to store an empty value for {key!r}", reason="empty value"))
        register_secret(value)
        api = self._api()
        self._call("save", key, api.set_password, value)

    def get(self, key: str) -> Optional[str]:
        api = self._api()
        value = self._call("read", key, api.get_password)
        if value:
            register_secret(value)
        return value or None

    def delete(self, key: str) -> None:
        """Remove a secret. Deleting one that is not there is not an error."""
        api = self._api()
        old = None
        try:
            old = api.get_password(self._service, key)
        except Exception:                              # noqa: BLE001 - only used to forget the value
            old = None
        try:
            api.delete_password(self._service, key)
        except Exception as exc:                       # noqa: BLE001
            if type(exc).__name__ == "PasswordDeleteError":
                # keyring raises this when there is nothing to delete; the goal
                # (no secret under that key) is already met.
                pass
            else:
                raise AppErrorException(make_error(
                    "ERR_ACCOUNT_SECRET_STORE", "accounts.secrets",
                    details=redact(f"delete {key!r}: {type(exc).__name__}: {exc}"),
                    reason="delete failed")) from None
        forget_secret(old)
