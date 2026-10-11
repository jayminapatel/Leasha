"""No mail connector can write to a mailbox (rule 10, order 1k 5e).

Layer: L7 (test)

Three guards, because each catches what the others cannot:

1. **The surface.** Each connector's public methods are a fixed list; a new one
   (say `mark_read`) fails here until someone looks at it.
2. **The source.** An AST scan: HTTP calls are GET only, no write verb on the IMAP
   connection, folders opened with `readonly=True` (EXAMINE), bodies fetched with
   BODY.PEEK and never BODY[] or RFC822, and `UID` is used for SEARCH and FETCH only.
   The scanner is itself tested against deliberately bad snippets so it cannot go
   quiet without anyone noticing.
3. **The wire.** A full run of every connector against its fake records every HTTP
   verb and every IMAP command and checks them, and that a fake server's messages
   come out with the flags they went in with.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from pathlib import Path

import pytest

from app.accounts.providers.gmail_api import GmailApiMailProvider
from app.accounts.providers.graph_mail import GraphMailProvider
from app.accounts.providers.imap_mail import ImapMailProvider
from tests.unit import test_mail_gmail_api as gm
from tests.unit import test_mail_graph as gr
from tests.unit import test_mail_imap as im

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = ROOT / "app" / "accounts" / "providers"
MODULES = {"gmail": PROVIDERS / "gmail_api.py", "graph": PROVIDERS / "graph_mail.py",
           "imap": PROVIDERS / "imap_mail.py"}
INGEST = ROOT / "app" / "accounts" / "mail_ingest.py"

#: The only public names a mail connector may have. Anything else must be argued for.
ALLOWED_PUBLIC = {
    GmailApiMailProvider: {"list_folders", "pull", "close"},
    GraphMailProvider: {"list_folders", "pull", "close"},
    ImapMailProvider: {"list_folders", "pull", "close", "idle", "icloud", "host", "port", "security"},
}
IMAP_WRITE_METHODS = {"store", "append", "copy", "expunge", "delete", "create", "rename", "setacl",
                      "deleteacl", "subscribe", "unsubscribe", "setquota", "setannotation", "move",
                      "close"}   # CLOSE on a selected mailbox expunges on a writable one
IMAP_FORBIDDEN_WORDS = re.compile(r"\b(STORE|APPEND|EXPUNGE|COPY|MOVE|DELETE|CREATE|RENAME|SELECT)\b")
HTTP_FORBIDDEN = {"POST", "PUT", "PATCH", "DELETE"}


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def violations(source: str, kind: str) -> list[str]:
    """Everything in `source` that could write to a mailbox. `kind` is "http" or "imap"."""
    tree = ast.parse(source)
    skip = _docstrings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            text = node.value
            if kind == "http" and text.strip().upper() in HTTP_FORBIDDEN:
                found.append(f"line {node.lineno}: HTTP verb {text!r}")
            if kind == "imap":
                if IMAP_FORBIDDEN_WORDS.search(text):
                    found.append(f"line {node.lineno}: IMAP write command in {text!r}")
                if ("BODY[" in text or "RFC822" in text.upper().replace("RFC 822", "")) and "BODY.PEEK[" not in text:
                    found.append(f"line {node.lineno}: a fetch that marks mail seen: {text!r}")
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr, receiver = node.func.attr, ast.unparse(node.func.value)
        if attr == "request" and "transport" in receiver:
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Constant) and first.value == "GET"):
                found.append(f"line {node.lineno}: transport.request is not a literal GET")
        if kind == "imap" and "conn" in receiver:
            if attr in IMAP_WRITE_METHODS and attr != "close":
                found.append(f"line {node.lineno}: IMAP write method .{attr}()")
            if attr == "close":
                found.append(f"line {node.lineno}: IMAP .close() (use logout)")
            if attr == "select":
                ok = any(k.arg == "readonly" and isinstance(k.value, ast.Constant) and k.value.value is True
                         for k in node.keywords)
                if not ok:
                    found.append(f"line {node.lineno}: .select() without readonly=True")
            if attr == "uid":
                first = node.args[0] if node.args else None
                if not (isinstance(first, ast.Constant) and first.value in ("SEARCH", "FETCH")):
                    found.append(f"line {node.lineno}: .uid() with something other than SEARCH or FETCH")
    return found


# -- 1. the surface ---------------------------------------------------------------

@pytest.mark.parametrize("cls", list(ALLOWED_PUBLIC))
def test_the_public_surface_has_no_way_to_write(cls):
    public = {n for n in dir(cls) if not n.startswith("_")}
    assert public <= ALLOWED_PUBLIC[cls], f"unexpected public names: {sorted(public - ALLOWED_PUBLIC[cls])}"


# -- 2. the source ------------------------------------------------------------------

@pytest.mark.parametrize("name,kind", [("gmail", "http"), ("graph", "http"), ("imap", "imap")])
def test_the_source_has_no_write_verbs(name, kind):
    assert violations(MODULES[name].read_text(encoding="utf-8"), kind) == []


def test_the_scanner_catches_what_it_is_there_for():
    bad_http = 'def f(transport):\n    transport.request("POST", url)\n    verb = "DELETE"\n    transport.request(m, url)\n'
    assert len(violations(bad_http, "http")) == 4
    bad_imap = ('def f(conn):\n    conn.select("INBOX")\n    conn.store("1", "+FLAGS", "x")\n'
                '    conn.uid("STORE", "1")\n    conn.uid("FETCH", "1", "(BODY[])")\n    conn.append("x")\n'
                '    c = "UID COPY 1 Trash"\n    conn.close()\n    conn.select("a", readonly=False)\n')
    found = violations(bad_imap, "imap")
    assert len(found) >= 8, found
    good = 'def f(conn):\n    conn.select("a", readonly=True)\n    conn.uid("FETCH", "1", "(BODY.PEEK[])")\n'
    assert violations(good, "imap") == []


# -- 3. the wire ------------------------------------------------------------------------

def test_gmail_makes_only_get_requests_through_a_full_cycle():
    first = gm.Scripted(gm.first_fetch_routes())
    list(gm.provider(first).pull(None))
    hist = gm.Scripted({
        f"{gm.B}/labels": [gm.ok(gm.LABELS)],
        f"{gm.B}/history?" + gm.HIST.format(h="100"): [gm.ok({"historyId": "5", "history": [
            {"id": "1", "messagesAdded": [{"message": {"id": "n"}}]},
            {"id": "2", "messagesDeleted": [{"message": {"id": "d"}}]}]})],
        f"{gm.B}/messages/n?format=raw": [gm.ok(gm.gmail_msg("n", ["INBOX"]))],
    })
    list(gm.provider(hist).pull(gm.hist_token()))
    calls = first.calls + hist.calls
    assert calls and {m for m, _u, _h in calls} == {"GET"}
    assert not any(re.search(r"/(modify|trash|untrash|delete|send|import|insert|batchModify)", u) for _m, u, _h in calls)


def test_graph_makes_only_get_requests_through_a_full_cycle():
    first = gr.Scripted(gr.first_routes())
    list(gr.provider(first).pull(None))
    token = json.dumps({"v": 1, "f": {"AAA": {"delta": f"{gr.G}/delta-aaa"}}, "cur": None})
    again = gr.Scripted({**gr.folder_routes(),
                         f"{gr.G}/delta-aaa": [gr.ok({"value": [{"id": "a1", "@removed": {"reason": "deleted"}}],
                                                      "@odata.deltaLink": f"{gr.G}/d2"})],
                         gr.delta("CCC"): [gr.ok({"value": [], "@odata.deltaLink": f"{gr.G}/d3"})],
                         gr.delta("BBB"): [gr.ok({"value": [], "@odata.deltaLink": f"{gr.G}/d4"})]})
    list(gr.provider(again).pull(token))
    calls = first.calls + again.calls
    assert calls and {m for m, _u, _h in calls} == {"GET"}
    assert not any(re.search(r"/(move|copy|send|forward|reply|createReply)", u) for _m, u, _h in calls)


@pytest.fixture
def servers():
    made: list[im.FakeImap] = []

    def make(**kw):
        s = im.FakeImap(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.stop()


def _flags(server):
    return {(n, u): set(m.flags) for n, f in server.folders.items() for u, m in f.msgs.items()}


def test_imap_speaks_only_the_read_commands_and_changes_no_flag(servers):
    plain = im.basic_server(servers, condstore=True, idle=True)
    gmail = im.gmail_server(servers, condstore=True, idle=True)
    before = {id(s): _flags(s) for s in (plain, gmail)}
    for s, host in ((plain, "imap.example.com"), (gmail, "imap.gmail.com")):
        p = im.provider(s, host=host)
        token = im.pull_all(p, include_spam_trash=True)[-1].next_token
        s.folders["INBOX"].add(im.mail("late"))
        if s is plain:
            s.folders["INBOX"].remove(1)
        im.pull_all(p, token, include_spam_trash=True)
        stop = threading.Event()
        stop.set()
        p.idle("INBOX", lambda f: None, stop, max_seconds=1)
        p.close()
    for s in (plain, gmail):
        assert set(s.verbs) <= im.ALLOWED_VERBS, set(s.verbs) - im.ALLOWED_VERBS
        assert s.violations == []
        assert "SELECT" not in s.verbs and "EXAMINE" in s.verbs
        for args in s.args_of("UID FETCH"):
            assert "BODY.PEEK[]" in args or "CHANGEDSINCE" in args
    # nothing the connector did turned an unread message into a read one
    after = _flags(plain)
    for key, flags in before[id(plain)].items():
        if key in after:
            assert after[key] == flags


def test_imap_idle_uses_examine_and_no_other_write(servers):
    s = im.basic_server(servers, idle=True)
    stop = threading.Event()
    stop.set()
    im.provider(s).idle("INBOX", lambda f: None, stop, max_seconds=1)
    assert set(s.verbs) <= im.ALLOWED_VERBS and s.violations == []


# -- house rules for the new files -----------------------------------------------------

@pytest.mark.parametrize("path", [*MODULES.values(), INGEST, PROVIDERS / "_mailcommon.py"])
def test_modules_name_their_layer_and_are_ascii(path):
    source = path.read_text(encoding="utf-8")
    assert source.isascii(), "Python source here is ASCII by convention"
    assert "Layer: L7" in (ast.get_docstring(ast.parse(source)) or "")
