"""IMAP connector (order 1k 5c) against an in-process fake IMAP server.

Layer: L7 (test)

The fake is a small `socketserver` that speaks just enough IMAP4rev1 for a
read-only client: CAPABILITY, LOGIN, AUTHENTICATE XOAUTH2, ENABLE CONDSTORE,
LIST, EXAMINE, UID SEARCH, UID FETCH (BODY.PEEK[], FLAGS, INTERNALDATE,
CHANGEDSINCE, Gmail's X-GM-*), IDLE and LOGOUT. It listens on 127.0.0.1 only,
so nothing here leaves the machine.

It also behaves like a real server in the one way that matters for rule 10: a
`BODY[]` fetch (not `BODY.PEEK[]`) marks the message \\Seen, and a `SELECT`,
`STORE`, `APPEND`, `COPY`, `EXPUNGE` and the like are recorded as violations.
`tests/unit/test_mail_readonly.py` fails the build on any of them.
"""

from __future__ import annotations

import base64
import imaplib
import json
import re
import select
import socketserver
import threading
import time
from typing import Callable, Optional

import pytest

from app.accounts.provider import MailProvider, RemoteMessage, RemoteMessageGone, TokenExpired
from app.accounts.providers import imap_mail
from app.accounts.providers.imap_mail import ImapMailProvider
from app.core.errors import AppErrorException

ALLOWED_VERBS = {"CAPABILITY", "LOGIN", "AUTHENTICATE", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH",
                 "ENABLE", "IDLE", "DONE", "LOGOUT"}
WRITE_VERBS = {"SELECT", "STORE", "APPEND", "COPY", "MOVE", "EXPUNGE", "DELETE", "CREATE", "RENAME",
               "SUBSCRIBE", "UNSUBSCRIBE", "UID STORE", "UID COPY", "UID MOVE", "UID EXPUNGE", "CLOSE"}


def mail(subject: str) -> bytes:
    return (f"From: a@example.com\r\nTo: me@example.com\r\nSubject: {subject}\r\n"
            f"Message-ID: <{subject}@fake>\r\n\r\nBody of {subject}\r\n").encode()


class FakeMsg:
    def __init__(self, uid, raw, flags, date, labels, modseq):
        self.uid, self.raw, self.flags, self.date, self.labels, self.modseq = uid, raw, set(flags), date, labels, modseq


class FakeFolder:
    def __init__(self, uidvalidity=77, attrs=()):
        self.uidvalidity = uidvalidity
        self.attrs = list(attrs)
        self.msgs: dict[int, FakeMsg] = {}
        self.uidnext = 1
        self.modseq = 1

    def add(self, raw: bytes, *, flags=(), date="05-Mar-2024 10:00:00 +0000", labels=None) -> int:
        uid = self.uidnext
        self.uidnext += 1
        self.modseq += 1
        self.msgs[uid] = FakeMsg(uid, raw, flags, date, list(labels or []), self.modseq)
        return uid

    def remove(self, uid: int) -> None:
        del self.msgs[uid]
        self.modseq += 1

    def touch(self, uid: int, *, flags=None, labels=None) -> None:
        self.modseq += 1
        m = self.msgs[uid]
        m.modseq = self.modseq
        if flags is not None:
            m.flags = set(flags)
        if labels is not None:
            m.labels = list(labels)


def _uid_set(text: str, top: int) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        if ":" in part:
            a, b = part.split(":")
            lo = int(a) if a != "*" else top
            hi = int(b) if b != "*" else top
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        elif part == "*":
            out.add(top)
        else:
            out.add(int(part))
    return out


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _date_key(text: str) -> tuple[int, int, int]:
    day, mon, year = text.split(" ")[0].split("-")
    return int(year), _MONTHS.index(mon), int(day)


class FakeImap:
    def __init__(self, *, user="me@example.com", password="app-pass", condstore=False, idle=False,
                 gmail=False, oauth_token="ya29.token"):
        self.user, self.password, self.oauth_token = user, password, oauth_token
        self.condstore, self.idle_ok, self.gmail = condstore, idle, gmail
        self.folders: dict[str, FakeFolder] = {}
        self.commands: list[tuple[str, str]] = []
        self.violations: list[str] = []
        self.idle_events: list[str] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self_inner):               # noqa: N805
                fake._serve(self_inner)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    # -- test helpers --------------------------------------------------------

    def folder(self, name: str, **kw) -> FakeFolder:
        self.folders[name] = FakeFolder(**kw)
        return self.folders[name]

    def factory(self) -> Callable[[], imaplib.IMAP4]:
        return lambda: imaplib.IMAP4("127.0.0.1", self.port)

    def notify(self, folder: str) -> None:
        self.idle_events.append(folder)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def verbs(self) -> list[str]:
        return [v for v, _a in self.commands]

    def args_of(self, verb: str) -> list[str]:
        return [a for v, a in self.commands if v == verb]

    # -- protocol ------------------------------------------------------------

    def _caps(self, authed: bool) -> str:
        caps = ["IMAP4rev1", "AUTH=XOAUTH2"]
        if authed:
            if self.condstore:
                caps += ["ENABLE", "CONDSTORE"]
            if self.idle_ok:
                caps.append("IDLE")
            if self.gmail:
                caps.append("X-GM-EXT-1")
        return " ".join(caps)

    def _serve(self, h) -> None:
        w = h.wfile

        def send(text: str | bytes) -> None:
            w.write(text.encode("latin-1") if isinstance(text, str) else text)
            w.flush()

        send("* OK IMAP4rev1 fake ready\r\n")
        authed = False
        selected: Optional[str] = None
        cond_on = False
        while True:
            raw = h.rfile.readline()
            if not raw:
                return
            line = raw.rstrip(b"\r\n").decode("latin-1")
            tag, _, rest = line.partition(" ")
            verb, _, args = rest.partition(" ")
            verb = verb.upper()
            if verb == "UID":
                sub, _, args = args.partition(" ")
                verb = "UID " + sub.upper()
            with self.lock:
                self.commands.append((verb, args))
            if verb in WRITE_VERBS:
                self.violations.append(f"{verb} {args}")
                send(f"{tag} NO read-only fake\r\n")
            elif verb == "CAPABILITY":
                send(f"* CAPABILITY {self._caps(authed)}\r\n{tag} OK done\r\n")
            elif verb == "LOGIN":
                parts = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', args)
                user, pw = [a or b for a, b in parts][:2]
                if user == self.user and pw == self.password:
                    authed = True
                    send(f"{tag} OK [CAPABILITY {self._caps(True)}] logged in\r\n")
                else:
                    send(f"{tag} NO [AUTHENTICATIONFAILED] bad credentials\r\n")
            elif verb == "AUTHENTICATE":
                send("+ \r\n")
                blob = h.rfile.readline().rstrip(b"\r\n")
                decoded = base64.b64decode(blob).decode("latin-1")
                if f"user={self.user}\x01auth=Bearer {self.oauth_token}\x01\x01" == decoded:
                    authed = True
                    send(f"{tag} OK authenticated\r\n")
                else:
                    send(f"{tag} NO [AUTHENTICATIONFAILED] bad token\r\n")
            elif not authed:
                send(f"{tag} BAD login first\r\n")
            elif verb == "ENABLE":
                if self.condstore:
                    cond_on = True
                    send(f"* ENABLED CONDSTORE\r\n{tag} OK enabled\r\n")
                else:
                    send(f"{tag} BAD no\r\n")
            elif verb == "LIST":
                for name, f in self.folders.items():
                    attrs = " ".join(["\\HasNoChildren", *f.attrs])
                    send(f'* LIST ({attrs}) "/" "{name}"\r\n')
                send(f"{tag} OK done\r\n")
            elif verb == "EXAMINE":
                m = re.match(r'"((?:[^"\\]|\\.)*)"(?: \(CONDSTORE\))?$', args)
                name = m.group(1) if m else None
                if m and "(CONDSTORE)" in args:
                    cond_on = True
                if name not in self.folders:
                    send(f"{tag} NO no such folder\r\n")
                    continue
                selected = name
                f = self.folders[name]
                out = f"* {len(f.msgs)} EXISTS\r\n* 0 RECENT\r\n* OK [UIDVALIDITY {f.uidvalidity}] ok\r\n"
                out += f"* OK [UIDNEXT {f.uidnext}] ok\r\n"
                if cond_on:
                    out += f"* OK [HIGHESTMODSEQ {f.modseq}] ok\r\n"
                send(out + f"{tag} OK [READ-ONLY] examined\r\n")
            elif verb == "UID SEARCH":
                f = self.folders[selected]
                top = max(f.msgs) if f.msgs else 0
                toks = args.split()
                hits = set(f.msgs)
                i = 0
                while i < len(toks):
                    t = toks[i].upper()
                    if t == "ALL":
                        i += 1
                    elif t == "UID":
                        hits &= _uid_set(toks[i + 1], f.uidnext - 1 if f.msgs else 0)
                        # RFC 3501: "n:*" always includes the highest message
                        i += 2
                    elif t == "SINCE":
                        key = _date_key(toks[i + 1])
                        hits = {u for u in hits if _date_key(f.msgs[u].date) >= key}
                        i += 2
                    else:
                        i += 1
                if any("UID" == t.upper() for t in toks):
                    spec = toks[[t.upper() for t in toks].index("UID") + 1]
                    if spec.endswith(":*") and f.msgs and not (hits):
                        hits = {max(f.msgs)}
                send("* SEARCH " + " ".join(str(u) for u in sorted(hits)) + f"\r\n{tag} OK done\r\n")
            elif verb == "UID FETCH":
                self._fetch(send, tag, args, self.folders[selected], cond_on)
                if "CHANGEDSINCE" in args:
                    cond_on = True
            elif verb == "IDLE":
                if not self.idle_ok:
                    send(f"{tag} BAD no idle\r\n")
                    continue
                send("+ idling\r\n")
                while True:
                    ready, _, _ = select.select([h.request], [], [], 0.05)
                    if ready:
                        done = h.rfile.readline()
                        if not done or done.strip().upper() == b"DONE":
                            with self.lock:
                                self.commands.append(("DONE", ""))
                            break
                    with self.lock:
                        events, self.idle_events = self.idle_events, []
                    for name in events:
                        send(f"* {len(self.folders[name].msgs)} EXISTS\r\n")
                send(f"{tag} OK IDLE terminated\r\n")
            elif verb == "LOGOUT":
                send(f"* BYE bye\r\n{tag} OK logout\r\n")
                return
            else:
                self.violations.append(f"UNKNOWN {verb} {args}")
                send(f"{tag} BAD unknown\r\n")

    def _fetch(self, send, tag: str, args: str, f: FakeFolder, cond_on: bool) -> None:
        m = re.match(r"(\S+) \((.*?)\)(?: \(CHANGEDSINCE (\d+)\))?$", args)
        uidset, items, since = m.group(1), m.group(2), m.group(3)
        wanted = _uid_set(uidset, f.uidnext - 1)
        wants_body = "BODY[" in items or "BODY.PEEK[" in items
        peek = "BODY.PEEK[" in items
        for seq, uid in enumerate(sorted(f.msgs), start=1):
            msg = f.msgs[uid]
            if uid not in wanted:
                continue
            if since is not None and msg.modseq <= int(since):
                continue
            if wants_body and not peek:
                msg.flags.add("\\Seen")
                self.violations.append(f"non-peek BODY[] fetch of uid {uid}")
            flags = " ".join(sorted(msg.flags))
            if not wants_body:
                send(f"* {seq} FETCH (UID {uid} MODSEQ ({msg.modseq}) FLAGS ({flags}))\r\n")
                continue
            head = f'* {seq} FETCH (UID {uid} FLAGS ({flags}) INTERNALDATE "{msg.date}" BODY[] {{{len(msg.raw)}}}\r\n'
            tail = b""
            if "X-GM-LABELS" in items:
                labels = " ".join('"' + lab.replace("\\", "\\\\") + '"' for lab in msg.labels)
                tail = f" X-GM-MSGID {1000 + uid} X-GM-THRID {2000 + uid} X-GM-LABELS ({labels})".encode("latin-1")
            send(head.encode("latin-1") + msg.raw + tail + b")\r\n")
        send(f"{tag} OK fetched\r\n")


@pytest.fixture
def fake():
    servers: list[FakeImap] = []

    def make(**kw) -> FakeImap:
        s = FakeImap(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()


def provider(fake: FakeImap, **kw) -> ImapMailProvider:
    kw.setdefault("batch_size", 2)
    kw.setdefault("connection_factory", fake.factory())
    kw.setdefault("host", "imap.example.com")
    kw.setdefault("account_name", "Fastmail")
    return ImapMailProvider(username="me@example.com", token_source=lambda: "app-pass", **kw)


def pull_all(p, token=None, **kw):
    return list(p.pull(token, **kw))


def changes_of(pages):
    return [c for p in pages for c in p.changes]


def basic_server(fake, **kw) -> FakeImap:
    s = fake(**kw)
    inbox = s.folder("INBOX", uidvalidity=77)
    for n in ("one", "two", "three"):
        inbox.add(mail(n), flags=["\\Seen"] if n == "two" else [])
    s.folder("Sent", uidvalidity=5, attrs=["\\Sent"]).add(mail("sent1"))
    s.folder("Spam", uidvalidity=6, attrs=["\\Junk"]).add(mail("junk1"))
    s.folder("Trash", uidvalidity=7, attrs=["\\Trash"]).add(mail("trash1"))
    return s


# --- basics ----------------------------------------------------------------

def test_it_satisfies_the_mail_provider_contract(fake):
    assert isinstance(provider(basic_server(fake)), MailProvider)


def test_icloud_preset_points_at_apples_server():
    p = ImapMailProvider.icloud(username="me@icloud.com", token_source=lambda: "abcd-efgh")
    assert (p.host, p.port, p.security) == ("imap.mail.me.com", 993, "ssl")


def test_first_fetch_ids_labels_tokens_and_nothing_is_marked_seen(fake):
    s = basic_server(fake)
    p = provider(s)
    got = pull_all(p)
    msgs = changes_of(got)
    ids = sorted(m.remote_id for m in msgs)
    assert ids == ["INBOX:77:1", "INBOX:77:2", "INBOX:77:3", "Sent:5:1"]       # Spam and Trash left out
    one = next(m for m in msgs if m.remote_id == "INBOX:77:1")
    assert isinstance(one, RemoteMessage) and one.raw == mail("one") and one.folder == "INBOX"
    assert one.seen is False and next(m for m in msgs if m.remote_id == "INBOX:77:2").seen is True
    assert one.received_at == 1709632800.0
    assert got[-1].done and not any(p.done for p in got[:-1])

    token = json.loads(got[-1].next_token)
    assert set(token) == {"INBOX", "Sent"}
    assert token["INBOX"]["uidvalidity"] == 77 and token["INBOX"]["last_uid"] == 3
    assert token["INBOX"]["modseq"] == 0 and token["INBOX"]["uids"] == "1:3"
    assert "pending" not in token["INBOX"]

    assert set(s.verbs) <= ALLOWED_VERBS
    assert s.violations == []
    p.close()
    assert s.verbs[-1] == "LOGOUT"
    assert [ln for ln in s.args_of("UID FETCH") if "BODY.PEEK[]" not in ln] == []


def test_examine_is_used_and_select_never(fake):
    s = basic_server(fake)
    pull_all(provider(s))
    assert "EXAMINE" in s.verbs and "SELECT" not in s.verbs


def test_paging_and_resume_from_a_mid_fetch_token(fake):
    s = fake()
    box = s.folder("INBOX", uidvalidity=9)
    for i in range(5):
        box.add(mail(f"m{i}"))
    first = provider(s)
    it = first.pull(None)
    page1 = next(it)
    assert [c.remote_id for c in page1.changes] == ["INBOX:9:1", "INBOX:9:2"]
    mid = json.loads(page1.next_token)["INBOX"]
    assert mid["last_uid"] == 2 and mid["pending"]["to"] == 5 and mid["uids"] == "1:2"
    it.close()

    rest = pull_all(provider(s), page1.next_token)
    ids = [c.remote_id for c in changes_of(rest)]
    assert ids == ["INBOX:9:3", "INBOX:9:4", "INBOX:9:5"]
    assert rest[-1].done and "pending" not in json.loads(rest[-1].next_token)["INBOX"]


def test_a_later_pull_fetches_only_new_mail(fake):
    s = basic_server(fake)
    token = pull_all(provider(s))[-1].next_token
    s.folders["INBOX"].add(mail("four"))
    s.folders["INBOX"].add(mail("five"))
    s.commands.clear()
    got = pull_all(provider(s), token)
    assert [c.remote_id for c in changes_of(got)] == ["INBOX:77:4", "INBOX:77:5"]
    assert json.loads(got[-1].next_token)["INBOX"]["last_uid"] == 5
    assert not any("1,2" in a for a in s.args_of("UID FETCH"))


def test_nothing_new_is_one_empty_done_page_with_the_same_cursor(fake):
    s = basic_server(fake)
    token = pull_all(provider(s))[-1].next_token
    got = pull_all(provider(s), token)
    assert len(got) == 1 and got[0].done and got[0].changes == []
    assert json.loads(got[0].next_token) == json.loads(token)


def test_a_message_deleted_at_the_server_is_gone_without_condstore(fake):
    s = basic_server(fake)
    token = pull_all(provider(s))[-1].next_token
    s.folders["INBOX"].remove(2)
    got = pull_all(provider(s), token)
    gone = changes_of(got)
    assert [type(c) for c in gone] == [RemoteMessageGone] and gone[0].remote_id == "INBOX:77:2"
    assert json.loads(got[-1].next_token)["INBOX"]["uids"] == "1,3"
    assert changes_of(pull_all(provider(s), got[-1].next_token)) == []          # said once only


def test_condstore_follows_flag_changes_and_deletes(fake):
    s = basic_server(fake, condstore=True)
    first = pull_all(provider(s))
    assert "ENABLE" in s.verbs
    token = first[-1].next_token
    assert json.loads(token)["INBOX"]["modseq"] == s.folders["INBOX"].modseq

    s.folders["INBOX"].touch(1, flags=["\\Seen"])
    s.folders["INBOX"].remove(3)
    s.commands.clear()
    got = pull_all(provider(s), token)
    kinds = {c.remote_id: c for c in changes_of(got)}
    assert isinstance(kinds["INBOX:77:3"], RemoteMessageGone)
    assert kinds["INBOX:77:1"].seen is True and kinds["INBOX:77:1"].raw == mail("one")
    assert any("CHANGEDSINCE" in a for a in s.args_of("UID FETCH"))
    assert json.loads(got[-1].next_token)["INBOX"]["modseq"] == s.folders["INBOX"].modseq
    assert s.violations == []


def test_a_changed_uidvalidity_drops_the_old_ids_and_refetches_the_folder(fake):
    s = basic_server(fake)
    token = pull_all(provider(s))[-1].next_token
    box = s.folders["INBOX"]
    box.uidvalidity = 88
    box.msgs.clear()
    box.uidnext = 1
    box.add(mail("fresh-a"))
    box.add(mail("fresh-b"))
    got = pull_all(provider(s), token)
    out = changes_of(got)
    gone = sorted(c.remote_id for c in out if isinstance(c, RemoteMessageGone))
    new = sorted(c.remote_id for c in out if isinstance(c, RemoteMessage))
    assert gone == ["INBOX:77:1", "INBOX:77:2", "INBOX:77:3"]
    assert new == ["INBOX:88:1", "INBOX:88:2"]
    st = json.loads(got[-1].next_token)["INBOX"]
    assert st["uidvalidity"] == 88 and st["uids"] == "1:2"
    assert json.loads(got[-1].next_token)["Sent"]["uidvalidity"] == 5      # others untouched


def test_since_bounds_the_first_fetch_and_old_mail_stays_unfetched(fake):
    s = fake()
    box = s.folder("INBOX", uidvalidity=3)
    box.add(mail("ancient"), date="01-Jan-2020 09:00:00 +0000")
    box.add(mail("recent"), date="05-Mar-2024 10:00:00 +0000")
    since = 1672531200.0                                    # 2023-01-01 UTC
    got = pull_all(provider(s), since=since)
    assert [c.remote_id for c in changes_of(got)] == ["INBOX:3:2"]
    assert any("SINCE 01-Jan-2023" in a for a in s.args_of("UID SEARCH"))
    token = got[-1].next_token
    assert json.loads(token)["INBOX"]["last_uid"] == 2
    box.add(mail("newest"), date="06-Mar-2024 10:00:00 +0000")
    assert [c.remote_id for c in changes_of(pull_all(provider(s), token))] == ["INBOX:3:3"]


def test_the_since_window_survives_a_resume(fake):
    s = fake()
    box = s.folder("INBOX", uidvalidity=3)
    box.add(mail("ancient"), date="01-Jan-2020 09:00:00 +0000")
    for i in range(3):
        box.add(mail(f"r{i}"), date="05-Mar-2024 10:00:00 +0000")
    it = provider(s).pull(None, since=1672531200.0)
    page1 = next(it)
    it.close()
    rest = pull_all(provider(s), page1.next_token)                 # no `since` passed again
    ids = [c.remote_id for c in changes_of([page1] + rest)]
    assert ids == ["INBOX:3:2", "INBOX:3:3", "INBOX:3:4"]


def test_spam_and_trash_come_only_when_asked(fake):
    s = basic_server(fake)
    ids = {c.remote_id for c in changes_of(pull_all(provider(s), include_spam_trash=True))}
    assert {"Spam:6:1", "Trash:7:1"} <= ids


def test_chosen_folders_only(fake):
    s = basic_server(fake)
    got = pull_all(provider(s), folders=["Sent"])
    assert [c.remote_id for c in changes_of(got)] == ["Sent:5:1"]
    assert s.args_of("EXAMINE") == ['"Sent"']


def test_a_folder_dropped_from_the_choice_retires_its_mail(fake):
    s = basic_server(fake)
    token = pull_all(provider(s))[-1].next_token
    got = pull_all(provider(s), token, folders=["INBOX"])
    assert [c.remote_id for c in changes_of(got)] == ["Sent:5:1"]
    assert isinstance(changes_of(got)[0], RemoteMessageGone)
    assert set(json.loads(got[-1].next_token)) == {"INBOX"}


def test_list_folders_names_every_selectable_folder(fake):
    s = basic_server(fake)
    assert provider(s).list_folders() == ["INBOX", "Sent", "Spam", "Trash"]


def test_a_token_this_code_did_not_write_is_expired(fake):
    s = basic_server(fake)
    for bad in ("nope", "[]", json.dumps({"INBOX": "x"}), json.dumps({"INBOX": {"uidvalidity": "a"}})):
        with pytest.raises(TokenExpired):
            pull_all(provider(s), bad)


# --- sign-in and reachability -------------------------------------------------

def test_xoauth2_when_the_secret_is_an_access_token(fake):
    s = basic_server(fake)
    p = ImapMailProvider(host="imap.example.com", username="me@example.com",
                         token_source=lambda: "ya29.token", auth="xoauth2",
                         connection_factory=s.factory(), batch_size=2)
    assert len(changes_of(pull_all(p))) == 4
    assert "AUTHENTICATE" in s.verbs and "LOGIN" not in s.verbs


def test_a_wrong_password_is_signin_expired(fake):
    s = basic_server(fake)
    p = ImapMailProvider(host="imap.example.com", username="me@example.com", token_source=lambda: "revoked",
                         connection_factory=s.factory(), account_name="iCloud")
    with pytest.raises(AppErrorException) as err:
        pull_all(p)
    assert err.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED" and "iCloud" in err.value.error.message


def test_an_unreachable_server_is_an_account_error():
    def refuse():
        raise ConnectionRefusedError("nothing listens")
    p = ImapMailProvider(host="imap.example.com", username="u", token_source=lambda: "p", connection_factory=refuse)
    with pytest.raises(AppErrorException) as err:
        pull_all(p)
    assert err.value.error.code == "ERR_ACCOUNT_UNREACHABLE" and err.value.error.suggestion


def test_plain_security_none_builds_its_own_connection(fake):
    s = basic_server(fake)
    p = ImapMailProvider(host="127.0.0.1", port=s.port, security="none", username="me@example.com",
                         token_source=lambda: "app-pass", batch_size=2)
    assert len(changes_of(pull_all(p))) == 4


def test_an_unknown_security_setting_is_refused():
    with pytest.raises(ValueError):
        ImapMailProvider(host="h", username="u", token_source=lambda: "p", security="wide-open")


# --- Gmail over IMAP (D3 route 2) -----------------------------------------------

def gmail_server(fake, **kw) -> FakeImap:
    s = fake(gmail=True, **kw)
    s.folder("INBOX", uidvalidity=1)                        # the same mail again: must not be fetched
    allmail = s.folder("[Gmail]/All Mail", uidvalidity=40, attrs=["\\All"])
    allmail.add(mail("g1"), labels=["\\Inbox", "Work"])
    allmail.add(mail("g2"), labels=["\\Sent"], flags=["\\Seen"])
    allmail.add(mail("g3"), labels=["Personal stuff", "\\Important"])
    s.folder("[Gmail]/Spam", uidvalidity=41, attrs=["\\Junk"]).add(mail("gs"), labels=[])
    s.folder("[Gmail]/Trash", uidvalidity=42, attrs=["\\Trash"]).add(mail("gt"), labels=[])
    s.folder("[Gmail]/Sent Mail", uidvalidity=43, attrs=["\\Sent"])
    s.folder("Work", uidvalidity=44)
    return s


def gmail_provider(s, **kw):
    return provider(s, host="imap.gmail.com", **kw)


def test_gmail_over_imap_reads_all_mail_once_with_labels_as_labels(fake):
    s = gmail_server(fake)
    got = pull_all(gmail_provider(s))
    msgs = changes_of(got)
    assert s.args_of("EXAMINE") == ['"[Gmail]/All Mail"']
    assert [m.remote_id for m in msgs] == ["[Gmail]/All Mail:40:1", "[Gmail]/All Mail:40:2", "[Gmail]/All Mail:40:3"]
    assert all(m.folder == "" for m in msgs)
    assert msgs[0].labels == ["Inbox", "Work"] and msgs[1].labels == ["Sent"]
    assert msgs[2].labels == ["Personal stuff", "Important"]
    assert msgs[1].seen is True
    assert any("X-GM-LABELS" in a for a in s.args_of("UID FETCH"))
    assert s.violations == []


def test_gmail_spam_and_trash_only_when_asked_and_labelled_as_such(fake):
    s = gmail_server(fake)
    msgs = changes_of(pull_all(gmail_provider(s), include_spam_trash=True))
    by_id = {m.remote_id: m for m in msgs}
    assert by_id["[Gmail]/Spam:41:1"].labels == ["Spam"] and by_id["[Gmail]/Trash:42:1"].labels == ["Trash"]
    assert len(msgs) == 5


def test_gmail_chosen_labels_scope_the_all_mail_fetch_and_follow_label_changes(fake):
    s = gmail_server(fake, condstore=True)
    p = gmail_provider(s)
    first = pull_all(p, folders=["Work", "Sent"])
    assert [m.remote_id for m in changes_of(first)] == ["[Gmail]/All Mail:40:1", "[Gmail]/All Mail:40:2"]
    token = first[-1].next_token
    box = s.folders["[Gmail]/All Mail"]
    box.touch(1, labels=["\\Inbox"])                      # Work removed: out of scope now
    box.touch(3, labels=["Work"])                         # Work added: in scope now
    got = pull_all(gmail_provider(s), token, folders=["Work", "Sent"])
    by_id = {c.remote_id: c for c in changes_of(got)}
    assert isinstance(by_id["[Gmail]/All Mail:40:1"], RemoteMessageGone)
    assert by_id["[Gmail]/All Mail:40:3"].labels == ["Work"]


def test_gmail_label_names_are_decoded_from_modified_utf7():
    assert imap_mail.imap_utf7_decode("Caf&AOk-") == "Café"
    assert imap_mail.imap_utf7_decode("a&-b") == "a&b"
    assert imap_mail.imap_utf7_encode("Café & Co") == "Caf&AOk- &- Co"
    for text in ("日本語", "Mix ü ed", "plain"):
        assert imap_mail.imap_utf7_decode(imap_mail.imap_utf7_encode(text)) == text


def test_uid_sets_round_trip_compactly():
    assert imap_mail.encode_uids([1, 2, 3, 5, 9, 10]) == "1:3,5,9:10"
    assert imap_mail.decode_uids("1:3,5,9:10") == {1, 2, 3, 5, 9, 10}
    assert imap_mail.decode_uids("") == set() and imap_mail.encode_uids([]) == ""


# --- IDLE (10d: off unless the master thread turns it on) ------------------------

def test_idle_is_not_used_by_pull(fake):
    s = basic_server(fake, idle=True)
    pull_all(provider(s))
    assert "IDLE" not in s.verbs


def test_idle_reports_new_mail_and_stops_on_request(fake):
    s = basic_server(fake, idle=True)
    seen: list[str] = []
    stop = threading.Event()
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(
        provider(s).idle("INBOX", seen.append, stop, max_seconds=20)), daemon=True)
    t.start()
    deadline = time.time() + 5
    while "IDLE" not in s.verbs and time.time() < deadline:
        time.sleep(0.02)
    s.folders["INBOX"].add(mail("arrived"))
    s.notify("INBOX")
    while not seen and time.time() < deadline:
        time.sleep(0.02)
    stop.set()
    t.join(5)
    assert seen == ["INBOX"] and result == [True] and not t.is_alive()
    assert s.verbs.count("DONE") == 1 and s.verbs[-1] == "LOGOUT"
    assert "SELECT" not in s.verbs and s.violations == []


def test_idle_gives_up_after_max_seconds(fake):
    s = basic_server(fake, idle=True)
    started = time.time()
    assert provider(s).idle("INBOX", lambda f: None, threading.Event(), max_seconds=1) is True
    assert time.time() - started < 6


def test_idle_on_a_server_without_it_returns_false(fake):
    s = basic_server(fake, idle=False)
    assert provider(s).idle("INBOX", lambda f: None, threading.Event(), max_seconds=1) is False
    assert "IDLE" not in s.verbs
