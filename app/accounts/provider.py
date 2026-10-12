"""The contracts between the sync engine and the providers.

Layer: L7

Order 1k. Everything a connector needs from, and gives to, the rest of Leasha
is in this file, so connectors, the engine, the fake providers in the tests and
the window can all be built at the same time.

**Transport.** A connector never opens a socket. It is handed a `Transport`
and calls `request`; the real one (`app/accounts/http.py`) checks the host
against the allow-list, retries with `Retry-After`, and is the only code that
touches the network. Tests hand a connector a recorded or hand-written
transport, so no test in the suite opens a connection (order 1k 4d).

**Contacts.** `ContactProvider.pull` yields pages of `RemoteContact`; each
page carries the token to resume from. A connector converts its own records
to and from `app.contacts.card.Card` and nothing else leaves the connector.
Groups travel as names in `Card.groups`; the connector maps them to its own
labels, folders or groups (and creates one that does not exist yet).

**Mail.** `MailProvider.pull` yields pages of `RemoteMessage` (the raw
RFC 822 bytes plus where it lives) or `RemoteMessageGone`. Read only: no mail
connector has a method that writes to a mailbox, and a test fails if one does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Protocol, runtime_checkable

from app.contacts.card import Capabilities, Card

__all__ = [
    "HttpResponse", "Transport", "TokenSource", "VersionConflict", "TokenExpired",
    "RemoteContact", "ContactPage", "ContactProvider", "WriteResult",
    "RemoteMessage", "RemoteMessageGone", "MailPage", "MailProvider",
]


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)   # lower-case names
    body: bytes = b""

    def json(self) -> Any:
        import json  # noqa: PLC0415
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


@runtime_checkable
class Transport(Protocol):
    """One HTTP request in, one response out. Never raises for a status code:
    a 4xx or 5xx is a returned `HttpResponse`. Raises `AppErrorException`
    (`ERR_ACCOUNT_UNREACHABLE`) when there is no answer at all."""

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 30.0) -> HttpResponse: ...


#: Returns a valid access token (refreshing it if needed) or, for an app
#: password, the secret itself. Called per request, so a token refreshed in the
#: middle of a sync is picked up.
TokenSource = Callable[[], str]


class VersionConflict(Exception):
    """A write lost a race: the record changed on the server since it was read."""


class TokenExpired(Exception):
    """The saved change token is no longer valid; the engine does a full pull."""


# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------

@dataclass
class RemoteContact:
    remote_id: str
    version: str = ""                  # etag / change key
    card: Optional[Card] = None        # None: deleted at the account
    edited_at: float = 0.0             # the account's own edit time, 0 if unknown


@dataclass
class ContactPage:
    changes: list[RemoteContact]
    next_token: Optional[str]          # resume here; on the last page, the token for next time
    done: bool = False


@dataclass
class WriteResult:
    """One contact's outcome inside a batch write (order 1t 5b). Exactly one of: written (`ok`), lost a race
    (`conflict`: the engine re-reads, re-merges and tries that one again on its own), or refused (`error`, the
    account's reason; that one stays waiting). `card` is what the account now holds, when its answer says so -
    the merge base - and None when it does not (the engine then reads it back)."""

    ok: bool = False
    remote_id: str = ""
    version: str = ""
    card: Optional[Card] = None
    conflict: bool = False
    error: Optional[Any] = None                 # an `AppError`


@runtime_checkable
class ContactProvider(Protocol):
    """A connector. **Optional, for speed (order 1t 5b, 3b):** `batch_size` (int) and `create_many(cards)`,
    `update_many([(remote_id, version, card)])`, `delete_many([(remote_id, version)])`, each returning one
    `WriteResult` per item in order and raising only for a failure of the account as a whole; `get_many(ids)`
    returning {id: RemoteContact or None}; and `list_ids()` returning {remote_id: version} for every contact the
    account holds - the cheapest read it offers, for the check after a sync. The engine uses them when present
    and the one-by-one methods when not."""

    caps: Capabilities

    def pull(self, token: Optional[str]) -> Iterator[ContactPage]:
        """Changes since `token` (None: everything). Raises `TokenExpired`."""

    def get(self, remote_id: str) -> Optional[RemoteContact]: ...

    def create(self, card: Card) -> tuple[str, str]:
        """Returns (remote_id, version)."""

    def update(self, remote_id: str, version: str, card: Card) -> str:
        """Returns the new version. Raises `VersionConflict`."""

    def delete(self, remote_id: str, version: str) -> None: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------
# Mail (Phase B)
# --------------------------------------------------------------------------

@dataclass
class RemoteMessage:
    remote_id: str                     # stable id inside the account
    raw: bytes                         # the whole RFC 822 message
    folder: str = ""                   # folder path, or "" for label-style providers
    labels: list[str] = field(default_factory=list)   # Gmail labels, Graph categories
    received_at: float = 0.0
    seen: bool = False
    version: str = ""


@dataclass
class RemoteMessageGone:
    """The message was deleted or moved out of the chosen folders at the server."""
    remote_id: str


@dataclass
class MailPage:
    changes: list[Any]                 # RemoteMessage | RemoteMessageGone
    next_token: Optional[str]
    done: bool = False
    total_hint: Optional[int] = None   # for first-fetch progress, when the server says


@runtime_checkable
class MailProvider(Protocol):
    def list_folders(self) -> list[str]: ...

    def pull(self, token: Optional[str], *, folders: Optional[list[str]] = None,
             since: Optional[float] = None, include_spam_trash: bool = False) -> Iterator[MailPage]:
        """Changes since `token`. `since` bounds a first fetch (D12)."""

    def close(self) -> None: ...
