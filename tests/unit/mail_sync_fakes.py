"""A fake mailbox and provider for the mail-sync and mail-index tests.

Layer: L7 (test support). Not a test module.

`FakeMailbox` is the account: RFC 822 messages with a change log. `FakeMailProvider` is a
`MailProvider` over it that pages like a real connector (token = the change number reached)
and has the faults a syncer must survive: a crash after some pages, an expired token. It
records every call it is given, so a test can assert that nothing but reads was asked of it.
No network, no sleeping.
"""

from __future__ import annotations

import time
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from typing import Iterator, Optional

import keyring
from keyring.backend import KeyringBackend

from app.accounts.provider import MailPage, RemoteMessage, RemoteMessageGone, TokenExpired
from app.accounts.secrets import SecretStore


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def get_password(self, service, username):
        return self.data.get((service, username))

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


def secret_store() -> SecretStore:
    return SecretStore(backend=MemoryKeyring())


def raw_message(subject: str, body: str, *, sender: str = "priya@example.com", to: str = "me@example.com",
                message_id: str = "", sent: Optional[datetime] = None,
                attachments: tuple = (), inline_logo: bool = False) -> bytes:
    """An RFC 822 message. `attachments` is `((name, bytes, "type/subtype"), ...)`."""
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, to, subject
    if message_id:
        m["Message-ID"] = message_id
    m["Date"] = format_datetime(sent or datetime(2024, 3, 5, 12, 0, tzinfo=timezone.utc))
    m.set_content(body)
    for name, data, ctype in attachments:
        main, _, sub = ctype.partition("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    if inline_logo:
        m.add_attachment(b"\x89PNG\r\n\x1a\n" + b"0" * 64, maintype="image", subtype="png",
                         filename="logo.png", disposition="inline")
    return m.as_bytes()


class FakeMailbox:
    def __init__(self) -> None:
        self.messages: dict[str, RemoteMessage] = {}
        self.log: list[tuple[int, str, str]] = []            # (seq, "add" | "gone", remote id)
        self.seq = 0
        self.n = 0
        self.total_hint = True

    def add(self, subject: str, body: str, *, folder: str = "INBOX", labels: tuple = (), remote_id: str = "",
            received_at: float = 0.0, **kw) -> str:
        self.n += 1
        rid = remote_id or f"m{self.n}"
        self.seq += 1
        self.messages[rid] = RemoteMessage(rid, raw_message(subject, body, **kw), folder=folder,
                                           labels=list(labels), received_at=received_at or time.time(),
                                           version=str(self.seq))
        self.log.append((self.seq, "add", rid))
        return rid

    def remove(self, rid: str) -> None:
        self.messages.pop(rid)
        self.seq += 1
        self.log.append((self.seq, "gone", rid))


class FakeMailProvider:
    def __init__(self, box: FakeMailbox, *, page_size: int = 2) -> None:
        self.box = box
        self.page_size = page_size
        self.crash_after_pages: Optional[int] = None
        self.expire_tokens = False
        self.calls: list[str] = []
        self.pull_args: list[dict] = []
        self.closed = False

    def list_folders(self) -> list[str]:
        self.calls.append("list_folders")
        return sorted({m.folder for m in self.box.messages.values()})

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True

    def pull(self, token: Optional[str], *, folders: Optional[list[str]] = None, since: Optional[float] = None,
             include_spam_trash: bool = False) -> Iterator[MailPage]:
        self.calls.append("pull")
        self.pull_args.append({"token": token, "folders": folders, "since": since,
                               "include_spam_trash": include_spam_trash})
        if token and self.expire_tokens:
            self.expire_tokens = False
            raise TokenExpired("history dropped")
        resume_page, head_at = 0, None
        if token and token.startswith("first:"):              # a first fetch resumed part-way
            _tag, k, head_s = token.split(":")
            resume_page, head_at, token = int(k), int(head_s), None
        if token is None:                                     # a first fetch: everything now, newest-first
            head = head_at if head_at is not None else self.box.seq
            changes = [m for m in self.box.messages.values()
                       if (folders is None or m.folder in folders or set(m.labels) & set(folders))
                       and (since is None or m.received_at >= since)]
            changes.sort(key=lambda m: -m.received_at)
        else:
            head = self.box.seq
            start = int(token)
            changes = []
            for seq, kind, rid in self.box.log:
                if seq <= start:
                    continue
                if kind == "gone":
                    changes.append(RemoteMessageGone(rid))
                elif rid in self.box.messages:
                    changes.append(self.box.messages[rid])
        pages = [changes[i:i + self.page_size] for i in range(0, len(changes), self.page_size)] or [[]]
        served = 0
        for i, chunk in enumerate(pages):
            if i < resume_page:
                continue
            if self.crash_after_pages is not None and served >= self.crash_after_pages:
                self.crash_after_pages = None
                raise ConnectionError("the network went away")
            last = i == len(pages) - 1
            # Resume token: the head once the last page is served; before that, the first-fetch
            # cursor ("first:<pages served>") so a crash resumes after the pages already saved.
            yield MailPage(chunk, str(head) if last else f"first:{i + 1}:{head}", done=last,
                           total_hint=len(changes) if self.box.total_hint else None)
            served += 1
