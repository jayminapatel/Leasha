"""Mail from any IMAP server, read-only: iCloud, Fastmail, Yahoo, a company server, Gmail.

Layer: L7

Order 1k 5c, D3 route 2 and D17. Implements `app.accounts.provider.MailProvider`
over `imaplib`, and **never writes to a mailbox** (rule 10). The whole of what this
module may say to a server is: CAPABILITY, LOGIN or AUTHENTICATE, ENABLE
CONDSTORE, LIST, EXAMINE, UID SEARCH, UID FETCH, IDLE and LOGOUT.

* Folders are opened with **EXAMINE** (imaplib's `select(readonly=True)`), never
  SELECT, so the server itself refuses a change.
* Bodies are fetched with **BODY.PEEK[]**, never BODY[] or RFC822, because those
  mark the message \\Seen on the server.

**Connection.** `connection_factory` returns a connected, *unauthenticated*
`imaplib.IMAP4` (tests hand it one aimed at a fake server on 127.0.0.1); without it
the connector builds IMAP4_SSL (`security="ssl"`, the default), IMAP4 followed by
STARTTLS (`"starttls"`) or, only when the account is explicitly set to `"none"`,
plain IMAP4. Sign-in is LOGIN with the app password from `token_source`
(`auth="login"`), or AUTHENTICATE XOAUTH2 when that source returns an OAuth
access token (`auth="xoauth2"`). `ImapMailProvider.icloud(...)` is the iCloud preset.

**Ids and tokens.** `remote_id` is `"<folder>:<uidvalidity>:<uid>"` (split from the
right: a folder name may hold a colon). The change token is a JSON map, sorted keys:

    {folder: {"uidvalidity": V, "last_uid": N, "modseq": M, "uids": "1:40,42",
              "pending": {"to": H|null, "since": epoch|null, "modseq": M0}}}

`last_uid` is the highest UID already taken; `modseq` is the folder's HIGHESTMODSEQ
(0 where CONDSTORE is not offered); `uids` is the set of UIDs delivered so far as
IMAP-style ranges - it is how a delete is noticed, because IMAP has no "what was
deleted" answer without QRESYNC: `UID SEARCH ALL` is diffed against it. `pending`
exists only while a folder's first fetch is part-way, and keeps the window and the
ceiling so a resumed fetch (rule 4) neither widens nor runs past them. Every page's
token is the whole map, so a page that carries nothing can be skipped.

**UIDVALIDITY.** If it changes, every UID the server gave before is meaningless: the
ids delivered under the old value are retired (`RemoteMessageGone`) and the folder
is fetched again from scratch under the new one.

**CONDSTORE.** Where offered (ENABLE CONDSTORE), a folder whose HIGHESTMODSEQ moved
is asked `UID FETCH 1:N (FLAGS) (CHANGEDSINCE m)` and each changed message is
fetched again, so a read flag (or, on Gmail, a label) change reaches the index.
Without it flag changes are not noticed; deletes still are, by the UID diff.

**Gmail over IMAP** (host imap.gmail.com, capability X-GM-EXT-1). Every message is in
"All Mail" as well as under its labels, so reading each folder would index it once
per label. Only All Mail is read, and `X-GM-LABELS` supplies the labels, which stay
labels (`folder` is ""). Spam and Trash are not in All Mail; they are read as well
only when asked, and carry the label "Spam" or "Trash". Choosing folders means
choosing labels: All Mail is still read once and a message is kept only if it has
one of them.

**IDLE** is a separate method, `idle(...)`, on its own connection. `pull` never uses
it: the schedule and Sync now are the only things that start a sync (D11.2, 10d).
"""

from __future__ import annotations

import base64
import imaplib
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Optional

from app.accounts.provider import MailPage, RemoteMessage, RemoteMessageGone, TokenExpired, TokenSource
from app.accounts.providers._mailcommon import dump_token, load_token
from app.core.errors import raise_error
from app.core.logging import logger

__all__ = ["ImapMailProvider", "imap_utf7_decode", "imap_utf7_encode", "encode_uids", "decode_uids",
           "remote_id"]

_log = logger.bind(component="accounts.imap_mail")
_COMPONENT = "accounts.imap_mail"

_SECURITY = ("ssl", "starttls", "none")
_GMAIL_HOSTS = ("imap.gmail.com", "imap.googlemail.com")
_BIN_NAMES = frozenset({"junk", "spam", "trash", "deleted items", "deleted messages", "bulk mail",
                        "junk email", "junk e-mail"})
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
#: Retiring a big folder is paged so one page is never a hundred thousand ids.
_RETIRE_CHUNK = 1000

#: Gmail's backslash labels, as the index should show them.
_GMAIL_SYSTEM = {"inbox": "Inbox", "sent": "Sent", "draft": "Drafts", "important": "Important",
                 "starred": "Starred", "spam": "Spam", "trash": "Trash"}
#: A folder choice names a label; these are the labels behind Gmail's folder names.
_GMAIL_FOLDER_LABEL = {"sent mail": "sent", "drafts": "drafts", "inbox": "inbox", "starred": "starred",
                       "important": "important", "spam": "spam", "trash": "trash", "all mail": "all mail"}


def remote_id(folder: str, uidvalidity: int, uid: int) -> str:
    return f"{folder}:{uidvalidity}:{uid}"


# --------------------------------------------------------------------------
# Small pure helpers (unit-tested directly)
# --------------------------------------------------------------------------

def imap_utf7_decode(text: str) -> str:
    """IMAP's modified UTF-7 (RFC 3501 5.1.3) as text: folder and label names."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "&":
            out.append(text[i])
            i += 1
            continue
        end = text.find("-", i)
        if end == -1:
            out.append(text[i:])
            break
        chunk = text[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            out.append(base64.b64decode(b64).decode("utf-16-be"))
        i = end + 1
    return "".join(out)


def imap_utf7_encode(text: str) -> str:
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            data = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(data).decode("ascii").rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def encode_uids(uids: Iterable[int]) -> str:
    """UIDs as IMAP ranges ("1:40,42"): a mailbox with a few gaps stays a few bytes."""
    ordered = sorted(set(uids))
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else f"{ordered[i]}:{ordered[j]}")
        i = j + 1
    return ",".join(parts)


def decode_uids(text: str) -> set[int]:
    out: set[int] = set()
    for part in filter(None, text.split(",")):
        if ":" in part:
            a, b = part.split(":")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _quote(name: str) -> str:
    """A mailbox name as an IMAP quoted string (imaplib does not quote it for us)."""
    return '"' + imap_utf7_encode(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(epoch: float) -> str:
    # UTC, English month names: SEARCH SINCE is date-only and locale-free by spec.
    d = datetime.fromtimestamp(int(epoch), timezone.utc)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


_INTERNALDATE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})")


def _internaldate(text: str) -> float:
    m = _INTERNALDATE.search(text or "")
    if not m or m.group(2).title() not in _MONTHS:
        return 0.0
    day, mon, year, hh, mm, ss, sign, oh, om = m.groups()
    offset = (int(oh) * 3600 + int(om) * 60) * (1 if sign == "+" else -1)
    stamp = datetime(int(year), _MONTHS.index(mon.title()) + 1, int(day), int(hh), int(mm), int(ss), tzinfo=timezone.utc)
    return stamp.timestamp() - offset


_LIST_LINE = re.compile(r'\((?P<attrs>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$', re.S)


def _parse_list(line: bytes) -> Optional[tuple[str, frozenset[str]]]:
    m = _LIST_LINE.search(line.decode("latin-1").strip())
    if not m:
        return None
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = re.sub(r"\\(.)", r"\1", name[1:-1])
    attrs = frozenset(a.lower() for a in m.group("attrs").split())
    return imap_utf7_decode(name), attrs


def _parse_paren_list(text: str, pos: int) -> list[str]:
    """The strings of the parenthesised list starting at `text[pos] == "("`.

    Quote-aware: a Gmail label may contain a space, a parenthesis or a backslash.
    """
    out: list[str] = []
    i = pos + 1
    while i < len(text):
        c = text[i]
        if c == ")":
            break
        if c == " ":
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < len(text) and text[j] != '"':
                if text[j] == "\\" and j + 1 < len(text):
                    j += 1
                buf.append(text[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] not in " )":
                j += 1
            out.append(text[i:j])
            i = j
    return out


_UID = re.compile(r"\bUID (\d+)")
_FLAGS = re.compile(r"FLAGS \(([^)]*)\)")
_IDATE = re.compile(r'INTERNALDATE "([^"]+)"')
_MODSEQ = re.compile(r"MODSEQ \((\d+)\)")


def _parse_fetch(data: list[Any]) -> dict[int, dict[str, Any]]:
    """imaplib's FETCH answer as {uid: {raw, flags, date, modseq, labels}}.

    imaplib hands back a tuple (text up to the literal, literal bytes) followed by
    the rest of that line as bytes, and a plain bytes line for a response with no
    literal. The server chooses where each attribute falls around the literal, so
    the text before and after it is searched together.
    """
    entries: list[list[Any]] = []
    for part in data:
        if part is None:
            continue
        if isinstance(part, tuple):
            entries.append([bytes(part[0]).decode("latin-1"), part[1], ""])
        else:
            text = bytes(part).decode("latin-1")
            if entries and entries[-1][1] is not None and not entries[-1][2] and not re.match(r"\d+ \(", text):
                entries[-1][2] = text
            else:
                entries.append([text, None, ""])
    out: dict[int, dict[str, Any]] = {}
    for head, literal, tail in entries:
        text = f"{head} {tail}"
        m = _UID.search(text)
        if not m:
            continue
        info: dict[str, Any] = {"raw": bytes(literal) if literal is not None else None, "flags": set(),
                                "date": "", "modseq": 0, "labels": None}
        f = _FLAGS.search(text)
        if f:
            info["flags"] = set(f.group(1).split())
        d = _IDATE.search(text)
        if d:
            info["date"] = d.group(1)
        q = _MODSEQ.search(text)
        if q:
            info["modseq"] = int(q.group(1))
        at = text.find("X-GM-LABELS (")
        if at != -1:
            info["labels"] = [imap_utf7_decode(x) for x in _parse_paren_list(text, at + len("X-GM-LABELS "))]
        out[int(m.group(1))] = info
    return out


def _chunks(items: list[int], size: int) -> Iterator[list[int]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _gmail_label(label: str) -> str:
    if label.startswith("\\"):
        base = label[1:]
        return _GMAIL_SYSTEM.get(base.lower(), base)
    return label


def _friendly_folder_label(folder: str) -> str:
    for prefix in ("[Gmail]/", "[Google Mail]/"):
        if folder.startswith(prefix):
            folder = folder[len(prefix):]
    return _GMAIL_FOLDER_LABEL.get(folder.lower(), folder.lower())


class _Session:
    """One authenticated connection and what the server said it can do."""

    def __init__(self, conn: imaplib.IMAP4, caps: set[str], gmail: bool):
        self.conn = conn
        self.caps = caps
        self.gm = gmail and "X-GM-EXT-1" in caps
        # CONDSTORE is used only where the server also offers ENABLE (RFC 7162): that
        # is what makes it report HIGHESTMODSEQ on EXAMINE. A pre-ENABLE server falls
        # back to the UID diff, which is correct, just blind to flag changes.
        self.condstore = "CONDSTORE" in caps and "ENABLE" in caps


# --------------------------------------------------------------------------
# The connector
# --------------------------------------------------------------------------

class ImapMailProvider:
    """Read-only mail from one IMAP account."""

    def __init__(self, host: str, *, port: int = 993, security: str = "ssl", username: str,
                 token_source: TokenSource, auth: str = "login", account_name: str = "IMAP",
                 connection_factory: Optional[Callable[[], imaplib.IMAP4]] = None,
                 batch_size: int = 25, timeout: float = 60.0, gmail: Optional[bool] = None):
        if security not in _SECURITY:
            raise ValueError(f"security must be one of {_SECURITY}, not {security!r}")
        if auth not in ("login", "xoauth2"):
            raise ValueError("auth must be 'login' or 'xoauth2'")
        self.host = host
        self.port = port
        self.security = security
        self._username = username
        self._token_source = token_source
        self._auth = auth
        self._account = account_name
        self._factory = connection_factory
        # Fetching a batch holds that many whole messages in memory at once; 25
        # keeps a folder of large attachments from becoming a gigabyte.
        self._batch = max(1, int(batch_size))
        self._timeout = timeout
        self._gmail = host.lower() in _GMAIL_HOSTS if gmail is None else bool(gmail)
        self._session: Optional[_Session] = None

    @classmethod
    def icloud(cls, *, username: str, token_source: TokenSource, **kw: Any) -> "ImapMailProvider":
        """iCloud Mail: its server, with the app-specific password as the secret."""
        return cls("imap.mail.me.com", port=993, security="ssl", username=username,
                   token_source=token_source, account_name=kw.pop("account_name", "iCloud"), **kw)

    # -- public --------------------------------------------------------------

    def list_folders(self) -> list[str]:
        return self._guarded(lambda: [n for n, a in self._list(self._ensure()) if self._usable(a)])

    def pull(self, token: Optional[str], *, folders: Optional[list[str]] = None,
             since: Optional[float] = None, include_spam_trash: bool = False) -> Iterator[MailPage]:
        states = self._load_states(token)
        try:
            session = self._ensure()
            selected, scope = self._select_folders(session, self._list(session), folders, include_spam_trash)
            yield from self._paged(self._all_pages(session, states, selected, scope, since))
        except (imaplib.IMAP4.abort, OSError, EOFError) as exc:
            self._drop()
            raise_error("ERR_ACCOUNT_UNREACHABLE", _COMPONENT, account=self._account,
                        reason=str(exc) or type(exc).__name__, details=repr(exc))
        except imaplib.IMAP4.error as exc:
            self._drop()
            raise_error("ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account,
                        reason=str(exc), details=repr(exc))

    def idle(self, folder: str, on_new: Callable[[str], None], stop_event: threading.Event,
             max_seconds: float) -> bool:
        """Wait on `folder` and call `on_new(folder)` when the server announces new mail.

        Returns False at once if the server does not offer IDLE, else True when it
        ends (stopped, or `max_seconds` passed). It uses its own connection and
        EXAMINE, never changes anything, and is only ever called when the person
        has switched "New inbox mail as it arrives" on for this account (10d).
        """
        def run() -> bool:
            session = self._open()
            try:
                if "IDLE" not in session.caps:
                    return False
                if self._examine(session, folder) is None:
                    return False
                return self._idle_loop(session, folder, on_new, stop_event, max_seconds)
            finally:
                self._logout(session.conn)
        return self._guarded(run)

    def close(self) -> None:
        if self._session is not None:
            self._logout(self._session.conn)
            self._session = None

    # -- connection ----------------------------------------------------------

    def _guarded(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (imaplib.IMAP4.abort, OSError, EOFError) as exc:
            self._drop()
            raise_error("ERR_ACCOUNT_UNREACHABLE", _COMPONENT, account=self._account,
                        reason=str(exc) or type(exc).__name__, details=repr(exc))
        except imaplib.IMAP4.error as exc:
            self._drop()
            raise_error("ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account,
                        reason=str(exc), details=repr(exc))

    def _ensure(self) -> _Session:
        if self._session is None:
            self._session = self._open()
        return self._session

    def _drop(self) -> None:
        if self._session is not None:
            try:
                self._session.conn.shutdown()
            except Exception:                                   # noqa: BLE001 - already broken
                pass
            self._session = None

    @staticmethod
    def _logout(conn: imaplib.IMAP4) -> None:
        try:
            conn.logout()
        except Exception as exc:                                # noqa: BLE001 - the server may have gone
            _log.debug("IMAP logout: {}", exc)

    def _connect(self) -> imaplib.IMAP4:
        if self._factory is not None:
            return self._factory()
        if self.security == "ssl":
            return imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(),
                                     timeout=self._timeout)
        conn = imaplib.IMAP4(self.host, self.port, timeout=self._timeout)
        if self.security == "starttls":
            conn.starttls(ssl_context=ssl.create_default_context())
        return conn

    def _open(self) -> _Session:
        """Connect and sign in. A refused sign-in is a revoked password, not a failure."""
        try:
            conn = self._connect()
        except (OSError, imaplib.IMAP4.error, EOFError) as exc:
            raise_error("ERR_ACCOUNT_UNREACHABLE", _COMPONENT, account=self._account,
                        reason=str(exc) or type(exc).__name__, details=repr(exc))
        secret = self._token_source()
        try:
            if self._auth == "xoauth2":
                blob = f"user={self._username}\x01auth=Bearer {secret}\x01\x01".encode("ascii")
                conn.authenticate("XOAUTH2", lambda _challenge: blob)
            else:
                conn.login(self._username, secret)
        except imaplib.IMAP4.abort as exc:
            raise_error("ERR_ACCOUNT_UNREACHABLE", _COMPONENT, account=self._account,
                        reason=str(exc), details=repr(exc))
        except imaplib.IMAP4.error as exc:
            raise_error("ERR_ACCOUNT_SIGNIN_EXPIRED", _COMPONENT, account=self._account, details=str(exc))
        typ, data = conn.capability()
        caps = {c.upper() for c in (data[0] or b"").decode("latin-1").split()} if typ == "OK" and data else set()
        # imaplib keeps its own copy from the greeting and `enable()` consults it;
        # the useful list is the one after sign-in.
        conn.capabilities = tuple(caps)
        session = _Session(conn, caps, self._gmail)
        if session.condstore:
            try:
                conn.enable("CONDSTORE")
            except imaplib.IMAP4.error as exc:
                _log.warning("ENABLE CONDSTORE refused: {}", exc)
                session.condstore = False
        return session

    # -- commands (the only ones this module issues) ---------------------------

    def _list(self, session: _Session) -> list[tuple[str, frozenset[str]]]:
        typ, data = session.conn.list()
        if typ != "OK":
            raise_error("ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account, reason="LIST refused")
        out = []
        for item in data or []:
            if item is None:
                continue
            line = item if isinstance(item, bytes) else item[0] + b'"' + item[1] + b'"'
            parsed = _parse_list(line)
            if parsed:
                out.append(parsed)
        return out

    @staticmethod
    def _usable(attrs: frozenset[str]) -> bool:
        return not (attrs & {"\\noselect", "\\nonexistent"})

    def _examine(self, session: _Session, name: str) -> Optional[dict[str, Any]]:
        # Quoted here because older imaplib does not, and newer leaves a quoted
        # argument alone (the 2026 hardening), so this is right for both.
        mailbox = _quote(name)
        # readonly=True is what makes imaplib send EXAMINE; SELECT would let a
        # client flag mail, and some servers set \Recent on it.
        typ, _data = session.conn.select(mailbox, readonly=True)
        if typ != "OK":
            return None

        def number(code: str) -> Optional[int]:
            _t, vals = session.conn.response(code)
            try:
                return int(vals[0]) if vals and vals[0] is not None else None
            except (TypeError, ValueError):
                return None

        validity = number("UIDVALIDITY")
        if validity is None:
            return None
        return {"uidvalidity": validity, "uidnext": number("UIDNEXT"), "modseq": number("HIGHESTMODSEQ") or 0}

    def _search(self, session: _Session, criteria: list[str]) -> set[int]:
        typ, data = session.conn.uid("SEARCH", *criteria)
        if typ != "OK":
            raise_error("ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account, reason="UID SEARCH refused")
        found: set[int] = set()
        for chunk in data or []:
            if chunk:
                found.update(int(x) for x in bytes(chunk).split())
        return found

    def _changed_since(self, session: _Session, last_uid: int, modseq: int) -> set[int]:
        typ, data = session.conn.uid("FETCH", f"1:{last_uid}", f"(FLAGS) (CHANGEDSINCE {modseq})")
        if typ != "OK":
            raise_error("ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account, reason="UID FETCH refused")
        found: set[int] = set()
        for part in data or []:
            text = bytes(part[0] if isinstance(part, tuple) else part or b"").decode("latin-1")
            m = _UID.search(text)
            if m:
                found.add(int(m.group(1)))
        return found

    def _fetch(self, session: _Session, uids: list[int]) -> dict[int, dict[str, Any]]:
        # BODY.PEEK, not BODY or RFC822: those set \Seen on the server (rule 10).
        items = "(BODY.PEEK[] FLAGS INTERNALDATE" + (" X-GM-LABELS)" if session.gm else ")")
        typ, data = session.conn.uid("FETCH", ",".join(str(u) for u in uids), items)
        if typ != "OK":
            raise_error("ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account, reason="UID FETCH refused")
        return {u: e for u, e in _parse_fetch(data or []).items() if e["raw"] is not None}

    # -- which folders -----------------------------------------------------------

    @staticmethod
    def _is_bin(name: str, attrs: frozenset[str]) -> bool:
        last = re.split(r"[/.]", name)[-1].strip().lower()
        return bool(attrs & {"\\junk", "\\trash"}) or last in _BIN_NAMES

    def _select_folders(self, session: _Session, infos: list[tuple[str, frozenset[str]]],
                        folders: Optional[list[str]], include_spam_trash: bool
                        ) -> tuple[list[tuple[str, frozenset[str]]], Optional[set[str]]]:
        usable = [(n, a) for n, a in infos if self._usable(a)]
        if session.gm:
            allmail = next((i for i in usable if "\\all" in i[1]), None) or next(
                (i for i in usable if i[0].lower().endswith("all mail")), None)
            if allmail is not None:
                return self._select_gmail(usable, allmail, folders, include_spam_trash)
        if folders:
            wanted = {f.lower() for f in folders}
            return [i for i in usable if i[0] in folders or i[0].lower() in wanted], None
        if include_spam_trash:
            return usable, None
        return [i for i in usable if not self._is_bin(*i)], None

    @staticmethod
    def _select_gmail(usable, allmail, folders, include):
        scope: Optional[set[str]] = None
        if folders:
            scope = {_friendly_folder_label(f) for f in folders}
            if "all mail" in scope:
                scope = None
        if include and scope is not None:
            scope |= {"spam", "trash"}
        chosen = [allmail]
        for entry in usable:
            if entry is allmail:
                continue
            if "\\junk" in entry[1] and (include or (scope and "spam" in scope)):
                chosen.append(entry)
            elif "\\trash" in entry[1] and (include or (scope and "trash" in scope)):
                chosen.append(entry)
        return chosen, scope

    # -- pages ---------------------------------------------------------------------

    @staticmethod
    def _load_states(token: Optional[str]) -> dict[str, dict[str, Any]]:
        try:
            states = load_token(token) or {}
            for st in states.values():
                if not isinstance(st, dict):
                    raise ValueError("folder state is not a map")
                for key in ("uidvalidity", "last_uid", "modseq"):
                    if not isinstance(st.get(key), int):
                        raise ValueError(f"folder state lacks {key}")
                if not isinstance(st.setdefault("uids", ""), str):
                    raise ValueError("uids is not text")
                decode_uids(st["uids"])
                pend = st.get("pending")
                if pend is not None and not (isinstance(pend, dict) and isinstance(pend.get("modseq"), int)):
                    raise ValueError("pending is malformed")
        except (ValueError, TypeError) as exc:
            raise TokenExpired(str(exc)) from exc
        return states

    @staticmethod
    def _paged(pages: Iterator[tuple[list[Any], str]]) -> Iterator[MailPage]:
        """Turn (changes, token) steps into MailPages, the last one `done`.

        A step with no changes is dropped because every token is the whole map and
        the next step's token includes it. One step is held back so the last page
        can be marked done, and it carries the very latest token.
        """
        held: Optional[tuple[list[Any], str]] = None
        latest = "{}"
        for changes, token in pages:
            latest = token
            if not changes:
                continue
            if held is not None:
                yield MailPage(held[0], held[1], done=False)
            held = (changes, token)
        yield MailPage(held[0] if held else [], latest, done=True)

    def _all_pages(self, session, states, selected, scope, since) -> Iterator[tuple[list[Any], str]]:
        names = [n for n, _a in selected]
        # A folder that left the choice (or the server) takes its mail with it (5e).
        for name in [n for n in list(states) if n not in names]:
            yield from self._retire(states, name)
        for name, attrs in selected:
            yield from self._folder_pages(session, name, attrs, states, scope, since)
        yield [], dump_token(states)

    @staticmethod
    def _retire(states, name) -> Iterator[tuple[list[Any], str]]:
        st = states[name]
        validity = st["uidvalidity"]
        remaining = sorted(decode_uids(st["uids"]))
        if not remaining:
            states.pop(name, None)
            return
        while remaining:
            chunk, remaining = remaining[:_RETIRE_CHUNK], remaining[_RETIRE_CHUNK:]
            # The ids still to retire stay in the token until their page is out, so
            # a crash between pages retires the rest instead of forgetting them.
            if remaining:
                st["uids"] = encode_uids(remaining)
            else:
                states.pop(name, None)
            yield [RemoteMessageGone(remote_id(name, validity, u)) for u in chunk], dump_token(states)

    def _message(self, session, name, validity, uid, entry, attrs) -> RemoteMessage:
        labels: list[str] = []
        folder = name
        if session.gm:
            folder = ""                  # labels stay labels; the folder would only repeat one of them
            labels = [_gmail_label(x) for x in (entry["labels"] or [])]
            extra = "Spam" if "\\junk" in attrs else "Trash" if "\\trash" in attrs else None
            if extra and extra not in labels:
                labels.append(extra)
        return RemoteMessage(
            remote_id=remote_id(name, validity, uid), raw=entry["raw"], folder=folder, labels=labels,
            received_at=_internaldate(entry["date"]), seen="\\Seen" in entry["flags"],
            version=str(entry["modseq"] or ""))

    @staticmethod
    def _in_scope(labels: list[str], scope: Optional[set[str]]) -> bool:
        return scope is None or any(label.lower() in scope for label in labels)

    def _folder_pages(self, session, name, attrs, states, scope, since) -> Iterator[tuple[list[Any], str]]:
        info = self._examine(session, name)
        if info is None:
            # Unreadable right now (permissions, a race with a delete). Its state is
            # kept; if it is really gone, the next LIST no longer names it and it retires.
            _log.warning("IMAP folder {!r} could not be examined; skipped", name)
            return
        validity = info["uidvalidity"]
        st = states.get(name)
        if st is not None and st["uidvalidity"] != validity:
            yield from self._retire(states, name)
            st = None
        fresh = st is None
        if st is None:
            top = info["uidnext"] - 1 if info["uidnext"] else None
            st = {"uidvalidity": validity, "last_uid": 0, "modseq": 0, "uids": "",
                  "pending": {"to": top, "since": int(since) if since else None, "modseq": info["modseq"]}}
            states[name] = st
        known = decode_uids(st["uids"])

        # 1. Deleted at the server: what was delivered and is no longer listed.
        if known and not fresh:
            gone = sorted(known - self._search(session, ["ALL"]))
            if gone:
                known -= set(gone)
                st["uids"] = encode_uids(known)
                yield [RemoteMessageGone(remote_id(name, validity, u)) for u in gone], dump_token(states)

        # 2. Changed since last time (CONDSTORE): flags, and on Gmail labels.
        if (session.condstore and not fresh and "pending" not in st and st["last_uid"] > 0
                and st["modseq"] > 0 and info["modseq"] > st["modseq"]):
            changed = sorted(self._changed_since(session, st["last_uid"], st["modseq"]))
            for chunk in _chunks(changed, self._batch):
                fetched = self._fetch(session, chunk)
                out: list[Any] = []
                for uid in chunk:
                    entry = fetched.get(uid)
                    if entry is None:
                        continue
                    message = self._message(session, name, validity, uid, entry, attrs)
                    if self._in_scope(message.labels, scope):
                        out.append(message)
                        known.add(uid)
                    elif uid in known:
                        out.append(RemoteMessageGone(message.remote_id))
                        known.discard(uid)
                st["uids"] = encode_uids(known)
                yield out, dump_token(states)

        # 3. New mail, in batches, each batch saved before the next (rule 4).
        pending = st.get("pending")
        if pending:
            ceiling, window = pending["to"], pending["since"]
        else:
            ceiling, window = (info["uidnext"] - 1 if info["uidnext"] else None), None
        lo = st["last_uid"] + 1
        new: list[int] = []
        if ceiling is None or ceiling >= lo:
            criteria = ["UID", f"{lo}:{ceiling if ceiling is not None else '*'}"]
            if window:
                criteria += ["SINCE", _imap_date(window)]
            # "n:*" always matches the highest message even when it is below n.
            new = sorted(u for u in self._search(session, criteria)
                         if u >= lo and (ceiling is None or u <= ceiling))
        for chunk in _chunks(new, self._batch):
            fetched = self._fetch(session, chunk)
            out = []
            for uid in chunk:
                entry = fetched.get(uid)
                if entry is None:
                    continue                      # deleted since it was listed
                message = self._message(session, name, validity, uid, entry, attrs)
                if self._in_scope(message.labels, scope):
                    out.append(message)
                    known.add(uid)
            st["last_uid"] = chunk[-1]
            st["uids"] = encode_uids(known)
            yield out, dump_token(states)

        # 4. The folder is caught up: settle the cursor.
        if pending:
            st["modseq"] = pending["modseq"] or 0
            st.pop("pending", None)
        elif info["modseq"]:
            st["modseq"] = info["modseq"]
        if ceiling:
            st["last_uid"] = max(st["last_uid"], ceiling)
        st["uids"] = encode_uids(known)
        yield [], dump_token(states)

    # -- IDLE --------------------------------------------------------------------

    @staticmethod
    def _buffered(conn: imaplib.IMAP4) -> bytes:
        """Bytes imaplib has already read ahead of the line it returned, without blocking."""
        sock = conn.socket()
        try:
            sock.setblocking(False)
            data = conn.file.peek(65536)
            if data:
                conn.file.read(len(data))
            return bytes(data)
        except (OSError, ValueError):
            return b""
        finally:
            sock.settimeout(1.0)

    def _idle_loop(self, session: _Session, folder: str, on_new, stop_event, max_seconds) -> bool:
        conn = session.conn
        sock = conn.socket()
        tag = conn._new_tag()                                   # noqa: SLF001 - imaplib has no idle() before 3.14
        conn.send(tag + b" IDLE\r\n")
        if not conn.readline().startswith(b"+"):
            return False
        buf = self._buffered(conn)
        deadline = time.monotonic() + max(0.0, max_seconds)
        # imaplib's file object cannot survive a read timeout, so from here the
        # socket is read directly in one-second slices to watch stop_event.
        while not stop_event.is_set() and time.monotonic() < deadline:
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if re.match(rb"\* \d+ EXISTS\b", line):
                    on_new(folder)
                elif line.startswith(b"* BYE"):
                    return True
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                return True
            buf += chunk
        conn.send(b"DONE\r\n")
        end = time.monotonic() + 10
        while time.monotonic() < end and (tag + b" ") not in buf:
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                break
            buf += chunk
        sock.settimeout(self._timeout)
        return True
