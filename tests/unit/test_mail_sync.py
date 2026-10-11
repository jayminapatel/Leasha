"""Live mail in: the syncer, over a fake mailbox (order 1k 5a-5g, 10f, 10j, 10q, D12).

Layer: L7 (test)

Offline: a `FakeMailProvider` stands in for Gmail, Graph and IMAP (their own tests are
`test_mail_gmail_api.py`, `test_mail_graph.py`, `test_mail_imap.py`). What is proved here is
what the syncer adds: a first fetch searchable by its words at once, resuming after a crash,
following a server delete, the first-fetch window, the speed cap, giving way to contacts,
and never asking a mailbox for anything but a read.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.accounts.mail_sync import MailOptions, MailSyncer, _ReadOnly, attach_mail
from app.accounts.mail_ingest import account_path
from app.accounts.service import AccountService
from app.core.errors import AppErrorException, make_error
from app.storage.sqlite_store import SqliteStore
from tests.unit.mail_sync_fakes import FakeMailbox, FakeMailProvider, secret_store


class FakeVectors:
    """The pipeline's vector store, as far as mail uses it."""

    def __init__(self):
        self.rows: dict[int, int] = {}           # chunk id -> file id
        self.deleted: list[list[int]] = []

    def delete_by_file_ids(self, ids):
        ids = list(ids)
        self.deleted.append(ids)
        self.rows = {c: f for c, f in self.rows.items() if f not in set(ids)}

    def add(self, chunk_ids, file_ids, vectors, **_k):
        self.rows.update(dict(zip(chunk_ids, file_ids)))
        return len(chunk_ids)


class Clock:
    """A monotonic clock that only the injected `sleep` moves."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def env(tmp_path):
    store = SqliteStore(tmp_path / "k.db").connect()
    svc = AccountService(store, secrets=secret_store())
    svc.accounts.update_settings(dry_run_first=False)
    box = FakeMailbox()
    provider = FakeMailProvider(box)
    clock = Clock()
    vectors = FakeVectors()
    syncer = attach_mail(svc, vectors=vectors, provider_for=lambda a: provider,
                         sleep=clock.sleep, monotonic=clock.monotonic)

    class Env:
        pass

    e = Env()
    e.store, e.svc, e.box, e.provider, e.clock, e.vectors, e.syncer = store, svc, box, provider, clock, vectors, syncer
    e.aid = svc.accounts.add("google", "me@x.com", "Home", mail_on=True, contacts_on=False)
    yield e
    store.close()


def sync(env, aid=None):
    out = env.svc.sync_now(aid or env.aid, "mail")
    assert len(out) == 1
    return out[0]


def paths(store, like="account://%"):
    return [r[0] for r in store.conn.execute("SELECT path FROM files WHERE path LIKE ? ORDER BY path", (like,))]


def search_paths(store, query):
    from app.chat.testing import keyword_engine

    engine = keyword_engine(store)
    try:
        return [r.path for r in engine.search(query, limit=20).results]
    finally:
        engine.close()


# ------------------------------------------------------------------ first fetch

def test_a_first_fetch_is_searchable_by_its_words_at_once(env):
    env.box.add("Boiler invoice", "The boiler invoice for March is attached.", message_id="<a@x>")
    env.box.add("Lunch", "Shall we meet for lunch on Friday?")
    out = sync(env)
    assert out.status == "ok" and out.plan.added == 2
    key = account_path(env.aid, "INBOX", "m1")
    assert key in search_paths(env.store, "boiler invoice")
    row = env.store.conn.execute("SELECT * FROM messages m JOIN files f ON f.id = m.file_id WHERE f.path = ?",
                                 (key,)).fetchone()
    assert row["account_id"] == env.aid and row["remote_id"] == "m1" and row["message_id"] == "a@x"
    assert row["store_path"] is None and row["subject"] == "Boiler invoice" and row["sender"] == "priya@example.com"
    assert row["source_kind"] == "eml" and row["status"] == "PARTIAL"
    # words first: no vectors yet (no embedder was given), every passage still waiting for its meaning
    assert env.store.conn.execute("SELECT COUNT(*) FROM chunks WHERE embedded = 0").fetchone()[0] >= 2


def test_gmail_style_labels_are_kept_as_labels_not_folders(env):
    env.box.add("Quarterly plan", "The plan for the quarter.", folder="", labels=("Inbox", "Work"), remote_id="g1")
    sync(env)
    key = account_path(env.aid, "", "g1")
    assert key == f"account://{env.aid}//g1" and key in paths(env.store)
    labels = env.store.conn.execute("SELECT labels FROM messages m JOIN files f ON f.id = m.file_id "
                                    "WHERE f.path = ?", (key,)).fetchone()[0]
    assert json.loads(labels) == ["Inbox", "Work"]


def test_the_sync_log_and_the_account_row_say_what_happened(env):
    env.box.add("Hello", "Hello there, friend.")
    out = sync(env)
    run = env.svc.accounts.run(out.run_id)
    assert run.kind == "mail" and run.status == "ok" and run.added == 1 and run.finished_at
    assert env.svc.accounts.get(env.aid).last_synced_at
    assert env.svc.accounts.state(env.aid, "mail").last_success_at


def test_the_mail_page_lists_live_messages_with_their_account(env):
    env.box.add("Agenda", "The agenda for Monday's meeting.", received_at=1_700_000_000)
    sync(env)
    rows = env.store.browse_messages()
    assert [r["subject"] for r in rows] == ["Agenda"] and rows[0]["path"].startswith(f"account://{env.aid}/")
    named = env.store.message_accounts([rows[0]["file_id"]])
    assert named[rows[0]["file_id"]]["account"] == "Home" and named[rows[0]["file_id"]]["account_id"] == env.aid


def test_a_file_on_disk_has_no_account(env):
    fid = env.store.upsert_file("/x.eml", size_bytes=1, mtime_ns=1, source_kind="eml")
    assert env.store.message_accounts([fid]) == {}


# -------------------------------------------------------------------- attachments

def _zip(*names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, "x")
    return buf.getvalue()


def test_attachments_are_indexed_as_the_mail_attachment_rule_says(env):
    env.box.add("Papers", "Papers for you.", attachments=(
        ("minutes.txt", b"The committee approved the zeppelin budget.", "text/plain"),
        ("photo.jpg", b"\xff\xd8\xff" + b"0" * 200, "image/jpeg"),
        ("bundle.zip", _zip("designs/blueprint.dwg"), "application/zip")), inline_logo=True)
    sync(env)
    msg = account_path(env.aid, "INBOX", "m1")
    rows = {r["path"][len(msg):]: r["source_kind"] for r in
            env.store.conn.execute("SELECT path, source_kind FROM files WHERE path LIKE ?", (msg + "/attachments/%",))}
    assert rows == {"/attachments/minutes.txt": "pst_message", "/attachments/photo.jpg": "pst_message",
                    "/attachments/bundle.zip": "pst_message"}               # the inline logo has no row
    assert msg + "/attachments/minutes.txt" in search_paths(env.store, "zeppelin budget")     # read for its words
    assert msg + "/attachments/bundle.zip" in search_paths(env.store, "blueprint")            # a zip: names inside
    assert msg + "/attachments/photo.jpg" in search_paths(env.store, "photo.jpg")             # a picture: by name
    assert env.store.conn.execute("SELECT has_attach FROM messages m JOIN files f ON f.id = m.file_id "
                                  "WHERE f.path = ?", (msg,)).fetchone()[0] == 1


def test_attachments_can_be_switched_off_and_limited(env):
    env.svc.accounts.update(env.aid, options={"mail_attachments": False})
    env.box.add("Papers", "Papers for you.", attachments=(("minutes.txt", b"zeppelin budget approved", "text/plain"),))
    sync(env)
    assert paths(env.store, "%/attachments/%") == []
    # a limit: the bigger one is kept by its name, not read
    env.svc.accounts.update(env.aid, options={"mail_attachments": True, "mail_attachment_limit_mb": 0.0001})
    env.syncer.indexer.forget(env.store.file_ids_under_prefix(f"account://{env.aid}/"))
    env.svc.accounts.reset_token(env.aid, "mail")
    sync(env)
    assert search_paths(env.store, "zeppelin") == []                       # not read
    assert any(p.endswith("/attachments/minutes.txt") for p in search_paths(env.store, "minutes.txt"))


# -------------------------------------------------------------------- resuming

def test_a_crash_in_the_middle_of_a_first_fetch_resumes_where_it_stopped(env):
    for n in range(6):
        env.box.add(f"Message number {n}", f"Body of message {n} with the word quokka{n}.", received_at=1000 - n)
    env.provider.crash_after_pages = 2                        # page size 2: four messages, then the network goes
    first = sync(env)
    assert first.status == "failed" and first.error.code == "ERR_SYNC_FAILED"
    assert len(paths(env.store)) == 4
    state = env.svc.accounts.state(env.aid, "mail")
    assert state.token.startswith("first:2:") and state.extra["first_fetch"]["active"]
    assert state.failures == 1 and env.svc.accounts.get(env.aid).last_error_code == "ERR_SYNC_FAILED"
    second = sync(env)
    assert second.status == "ok"
    assert env.provider.pull_args[-1]["token"] == state.token             # it carried on from the saved token
    assert len(paths(env.store)) == 6
    assert env.svc.accounts.state(env.aid, "mail").extra["first_fetch"]["active"] is False
    assert [r.status for r in env.svc.accounts.runs(account_id=env.aid, kind="mail")] == ["ok", "failed"]


def test_a_page_and_its_token_are_written_together_or_not_at_all(env, monkeypatch):
    env.box.add("One", "First body text.")
    env.box.add("Two", "Second body text.")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(type(env.svc.accounts), "save_token", boom)
    out = sync(env)
    assert out.status == "failed"
    assert paths(env.store) == []                             # the page's messages rolled back with its token


def test_a_token_the_provider_dropped_starts_the_fetch_again(env):
    env.box.add("First", "Alpha message body.")
    sync(env)
    env.box.add("Second", "Beta message body.")
    env.provider.expire_tokens = True
    out = sync(env)
    assert out.status == "ok"
    assert [a["token"] for a in env.provider.pull_args][-2:][0] is not None
    assert env.provider.pull_args[-1]["token"] is None
    assert len(paths(env.store)) == 2


# -------------------------------------------------------------- following the server

def test_mail_deleted_at_the_server_leaves_the_index_with_its_vectors_and_attachments(env):
    env.box.add("Keep me", "This message stays in the mailbox.")
    env.box.add("Delete me", "This message will go away.", attachments=(("notes.txt", b"unicorn notes", "text/plain"),))
    sync(env)
    gone_key = account_path(env.aid, "INBOX", "m2")
    ids = env.store.file_ids_under_prefix(gone_key)
    assert len(ids) == 2
    # give the doomed rows vectors, as an embedded index would have
    for fid in ids:
        for cid in env.store.conn.execute("SELECT id FROM chunks WHERE file_id = ?", (fid,)).fetchall():
            env.vectors.rows[cid[0]] = fid
    env.box.remove("m2")
    out = sync(env)
    assert out.status == "ok" and out.plan.deleted == 1
    assert gone_key not in paths(env.store) and paths(env.store, gone_key + "/%") == []
    assert env.store.conn.execute("SELECT COUNT(*) FROM chunks WHERE file_id IN (%s)" % ",".join(map(str, ids))).fetchone()[0] == 0
    assert not (set(env.vectors.rows.values()) & set(ids))                  # no orphaned vector
    assert search_paths(env.store, "unicorn") == [] and search_paths(env.store, "vanish") == []
    assert account_path(env.aid, "INBOX", "m1") in paths(env.store)


def test_a_message_that_leaves_the_chosen_folders_is_followed_and_widening_fetches_again(env):
    env.box.add("In the inbox", "Inbox words aardvark.", folder="INBOX")
    env.box.add("In projects", "Projects words badger.", folder="Projects")
    sync(env)
    assert len(paths(env.store)) == 2
    env.svc.accounts.update(env.aid, options={"mail_folders": ["INBOX"]})
    out = sync(env)
    assert out.plan.deleted == 1 and [p.split("/")[3] for p in paths(env.store)] == ["INBOX"]
    # widening starts again from the beginning, with the new folder list
    env.svc.accounts.update(env.aid, options={"mail_folders": ["INBOX", "Projects"]})
    sync(env)
    assert env.provider.pull_args[-1]["token"] is None and env.provider.pull_args[-1]["folders"] == ["INBOX", "Projects"]
    assert len(paths(env.store)) == 2


def test_gmail_labels_chosen_are_judged_by_label(env):
    env.box.add("Work thing", "Work words.", folder="", labels=("Inbox", "Work"), remote_id="g1")
    env.box.add("Family thing", "Family words.", folder="", labels=("Inbox", "Family"), remote_id="g2")
    sync(env)
    env.svc.accounts.update(env.aid, options={"mail_folders": ["Work"]})
    sync(env)
    assert [p.rsplit("/", 1)[-1] for p in paths(env.store)] == ["g1"]


# -------------------------------------------------------- D12: the first-fetch window

def test_the_first_fetch_window_is_handed_to_the_connector_and_kept_across_resumes(env):
    env.box.add("Old", "Old mail body.", received_at=1_000)
    env.box.add("New", "New mail body.", received_at=2_000_000_000)
    env.svc.accounts.update(env.aid, options={"mail_since": 1_500_000_000})
    sync(env)
    assert env.provider.pull_args[0]["since"] == 1_500_000_000
    assert [p.rsplit("/", 1)[-1] for p in paths(env.store)] == ["m2"]
    # a later sync is incremental: the window is a first-fetch thing
    sync(env)
    assert env.provider.pull_args[-1]["since"] is None


def test_the_window_can_be_a_date_or_the_last_n_years():
    from app.accounts.store import Account

    a = Account(1, "google", "a@x.com", "A", options={"mail_since": "2024-01-02"})
    assert MailOptions.from_account(a).since is not None
    b = Account(1, "google", "a@x.com", "A", options={"mail_years": 2})
    assert abs(MailOptions.from_account(b, now=1_000_000_000).since - (1_000_000_000 - 2 * 365.25 * 86400)) < 1
    c = Account(1, "google", "a@x.com", "A")
    o = MailOptions.from_account(c)
    assert o.since is None and o.folders is None and o.include_spam_trash is False and o.attachments is True


def test_a_window_set_after_the_first_fetch_does_not_remove_anything(env):
    env.box.add("Old", "Old mail body.", received_at=1_000)
    sync(env)
    env.svc.accounts.update(env.aid, options={"mail_since": 1_500_000_000})
    sync(env)
    assert len(paths(env.store)) == 1


# ----------------------------------------------------------------- speed cap, yielding

def test_the_first_fetch_speed_cap_paces_a_first_fetch_by_bytes(env):
    for n in range(6):
        env.box.add(f"Message {n}", "x" * 3000 + f" word{n}", received_at=1000 - n)
    total = sum(len(m.raw) for m in env.box.messages.values())
    env.svc.accounts.update_settings(first_fetch_kb_s=2)
    sync(env)
    # the last page is not waited for; the rest of the bytes were held to 2 KB a second
    slept = sum(env.clock.slept)
    assert slept > 0 and max(env.clock.slept) <= 1.0
    served_before_last = total - sum(len(m.raw) for m in list(env.box.messages.values())[-2:])
    assert slept == pytest.approx(served_before_last / 2048, rel=0.1)
    # no cap, no waiting; and an incremental sync is never paced
    env.clock.slept.clear()
    env.svc.accounts.update_settings(first_fetch_kb_s=0)
    env.box.add("Late", "A late message " + "y" * 5000)
    env.svc.accounts.update_settings(first_fetch_kb_s=1)
    sync(env)
    assert env.clock.slept == []


def test_no_cap_means_no_waiting(env):
    for n in range(4):
        env.box.add(f"Message {n}", "z" * 2000)
    sync(env)
    assert env.clock.slept == []


def test_a_first_fetch_gives_way_to_a_due_contact_sync_and_carries_on_later(env):
    for n in range(6):
        env.box.add(f"Message number {n}", f"Body {n} narwhal{n}.", received_at=1000 - n)
    asked = []
    env.syncer._should_yield = lambda: asked.append(1) or len(asked) == 1       # contacts are due after page 1
    first = sync(env)
    assert first.status == "paused" and first.error is None
    assert len(paths(env.store)) == 2
    st = env.svc.accounts.state(env.aid, "mail")
    assert st.token and st.extra["first_fetch"]["active"] and st.extra["first_fetch"]["state"] == "paused"
    assert env.svc.accounts.runs(account_id=env.aid, kind="mail")[0].status == "paused"
    assert env.svc.accounts.state(env.aid, "mail").last_success_at is None       # not a success yet
    second = sync(env)
    assert second.status == "ok" and len(paths(env.store)) == 6


def test_the_default_yield_asks_the_service_whether_contacts_are_due(env):
    env.syncer = MailSyncer(env.svc)
    assert env.syncer._contacts_due() is False                       # manual schedules: nothing is due


def test_stop_ends_a_run_paused_with_the_token_kept(env):
    for n in range(6):
        env.box.add(f"Message {n}", "words " * 20)
    calls = []
    out = env.svc.sync_now(env.aid, "mail", cancel=lambda: calls.append(1) or len(calls) >= 1)
    assert out[0].status == "paused" and len(paths(env.store)) == 2
    assert env.svc.accounts.state(env.aid, "mail").token


def test_progress_is_written_for_the_account_panel(env):
    for n in range(5):
        env.box.add(f"Message {n}", "progress words " * 10, received_at=100 - n)
    env.syncer._should_yield = lambda: True
    sync(env)
    p = env.syncer.progress(env.aid)
    assert p.state == "paused" and p.fetched == 2 and p.total == 5
    env.syncer._should_yield = lambda: False
    sync(env)
    done = env.syncer.progress(env.aid)
    assert done.state == "done" and done.fetched == 5 and done.total == 5


def test_pause_and_resume_hold_every_sync_of_the_account(env):
    env.box.add("Hello", "Hello world body.")
    env.syncer.pause(env.aid)
    out = sync(env)
    assert out.status == "paused" and env.provider.calls == [] and paths(env.store) == []
    assert env.syncer.progress(env.aid).paused is True
    env.syncer.resume(env.aid)
    assert sync(env).status == "ok" and len(paths(env.store)) == 1


# ------------------------------------------------------------------- nothing is written

class RecordingProvider(FakeMailProvider):
    """A provider that also has every verb that could change a mailbox, and fails if one is used."""

    WRITES = ("mark_read", "delete", "move", "store", "append", "expunge", "send", "flag", "create", "copy")

    def __getattr__(self, name):
        if name in self.WRITES:
            def forbidden(*a, **k):
                raise AssertionError(f"the syncer called {name}() on a mailbox")
            return forbidden
        raise AttributeError(name)


def test_an_account_is_never_written_to(env):
    env.box.add("Hello", "Hello again, body.", attachments=(("a.txt", b"attached words", "text/plain"),))
    env.box.add("Bye", "Goodbye now, body.")
    provider = RecordingProvider(env.box)
    env.syncer.provider_for = lambda a: provider
    sync(env)
    env.box.remove("m2")
    sync(env)
    env.svc.remove_account(env.aid)
    assert set(provider.calls) <= {"pull", "close", "list_folders"} and provider.closed


def test_the_syncer_holds_a_provider_that_can_only_read():
    assert set(_ReadOnly.__dict__) - {"__slots__", "__module__", "__doc__", "__init__", "__qualname__",
                                      "__firstlineno__", "__static_attributes__"} == {"pull", "list_folders", "close"}
    wrapped = _ReadOnly(RecordingProvider(FakeMailbox()))
    for verb in RecordingProvider.WRITES:
        assert not hasattr(wrapped, verb)


# --------------------------------------------------------------------- failure domains

def test_one_failing_account_never_stops_the_others(env):
    good = env.aid
    bad = env.svc.accounts.add("microsoft", "work@x.com", "Work", mail_on=True, contacts_on=False)
    env.box.add("Fine", "A perfectly fine message.")

    def provider_for(account):
        if account.id == bad:
            raise AppErrorException(make_error("ERR_ACCOUNT_SIGNIN_EXPIRED", "test", account=account.name))
        return env.provider

    env.syncer.provider_for = provider_for
    outs = env.svc.sync_now(kind="mail")
    assert {o.account_id: o.status for o in outs} == {good: "ok", bad: "failed"}
    work = env.svc.accounts.get(bad)
    assert work.last_error_code == "ERR_ACCOUNT_SIGNIN_EXPIRED"
    assert env.svc.accounts.runs(account_id=bad, kind="mail")[0].status == "failed"
    assert paths(env.store) == [account_path(good, "INBOX", "m1")]


def test_an_unreadable_message_costs_only_itself(env):
    env.box.add("Fine", "A perfectly fine message.")
    env.box.add("Fine two", "Another perfectly fine message.")
    real = env.syncer.indexer.prepare
    seen = {"n": 0}

    def flaky(account_id, msg, **kw):
        seen["n"] += 1
        if seen["n"] == 1:
            raise ValueError("garbled")
        return real(account_id, msg, **kw)

    env.syncer.indexer.prepare = flaky
    out = sync(env)
    assert out.status == "ok" and len(paths(env.store)) == 1


# ------------------------------------------------------------- meaning, space, removal

def test_meaning_follows_the_words_when_an_embedder_is_given(env):
    seen_when_embedding = []

    def embed(texts):
        # at this moment the page is committed and searchable by its words
        seen_when_embedding.append(search_paths(env.store, "pelican"))
        return [[0.0] * 4 for _ in texts]

    env.syncer.indexer.embed = embed
    env.syncer.indexer.vectors = env.vectors
    env.box.add("Birds", "A pelican landed on the pier.")
    sync(env)
    assert seen_when_embedding and seen_when_embedding[0] == [account_path(env.aid, "INBOX", "m1")]
    assert env.store.conn.execute("SELECT COUNT(*) FROM chunks WHERE embedded = 0").fetchone()[0] == 0
    assert env.store.conn.execute("SELECT status FROM files").fetchone()[0] == "INDEXED"
    assert env.vectors.rows


def test_a_model_that_fails_leaves_the_words_searchable_and_the_sync_ok(env):
    def embed(texts):
        raise RuntimeError("model will not load")

    env.syncer.indexer.embed, env.syncer.indexer.vectors = embed, env.vectors
    env.box.add("Birds", "A pelican landed on the pier.")
    assert sync(env).status == "ok"
    assert search_paths(env.store, "pelican")
    assert env.store.conn.execute("SELECT COUNT(*) FROM chunks WHERE embedded = 0").fetchone()[0] >= 1
    assert env.store.conn.execute("SELECT status FROM files").fetchone()[0] == "PARTIAL"


def test_space_used_and_remove_this_accounts_mail(env):
    env.box.add("One", "First body about marmalade.", attachments=(("n.txt", b"attachment text", "text/plain"),))
    env.box.add("Two", "Second body about kippers.")
    other = env.svc.accounts.add("google", "other@x.com", "Other", mail_on=True, contacts_on=False)
    sync(env)
    sync(env, other)
    used = env.syncer.space_used(env.aid)
    assert used.messages == 2 and used.attachments == 1 and used.passages >= 3 and used.bytes > 0
    # the second account holds the same two messages under its own keys
    removed = env.svc.accounts and env.syncer.remove_account_mail(env.aid)
    assert removed == 3
    assert env.syncer.space_used(env.aid).messages == 0 and env.syncer.space_used(env.aid).bytes == 0
    assert paths(env.store, f"account://{env.aid}/%") == [] and len(paths(env.store, f"account://{other}/%")) == 3
    assert env.svc.accounts.state(env.aid, "mail").token is None          # the next fetch starts from the beginning
    assert env.syncer.space_used(other).messages == 2


def test_removing_the_account_takes_its_mail_out_through_the_service_hook(env):
    env.box.add("One", "First body about marmalade.")
    sync(env)
    assert len(paths(env.store)) == 1
    env.svc.remove_account(env.aid, remove_mail=True)
    assert paths(env.store) == [] and env.svc.accounts.get(env.aid) is None


def test_keeping_the_mail_when_removing_the_account(env):
    env.box.add("One", "First body about marmalade.")
    sync(env)
    env.svc.remove_account(env.aid, remove_mail=False)
    assert len(paths(env.store)) == 1


def test_attach_mail_installs_the_two_seams(env):
    assert env.svc.mail_sync is env.syncer and env.svc.remove_mail_hook == env.syncer.remove_account_mail
