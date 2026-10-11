"""Order 1k section 8b, 8c, 8e, 8g: the contact tools and the accounts in index_status.

Layer: L6

Every tool is read against a real `SqliteStore` in `tmp_path`, seeded through
`ContactHub.create` and `AccountStore.add` with a fake account. Nothing here
signs in to anything.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.contacts.card import Card, Field
from tests.unit.test_cli_wiring import env_file

MESSAGE_FROM = "pst://2024/1"
MESSAGE_TO = "pst://2024/2"
MESSAGE_LOOKALIKE = "pst://2024/3"
MESSAGE_OTHER = "pst://2024/4"
ATTACHMENT = MESSAGE_FROM + "/attachments/quote.pdf"
PHOTO = "C:/photos/ann on the beach.jpg"

SECRET = "zq9-Distinct-Secret-7731-do-not-leak"
CLIENT_ID = "9999-fake-client-id.apps.example.invalid"


def _card(given, family, *, email="", phone="", company="", notes="", groups=(), favourite=False):
    card = Card(scalars={k: v for k, v in (("given", given), ("family", family),
                                           ("company", company), ("notes", notes)) if v},
                groups=list(groups), favourite=favourite)
    if email:
        card.fields.append(Field("email", email, "work", primary=True))
    if phone:
        card.fields.append(Field("phone", phone, "mobile"))
    return card


def seed(fts_db: Path) -> dict:
    """Two contacts, one account, four messages, one attachment, one photo."""
    from app.accounts.store import AccountStore
    from app.contacts.hub import ContactHub
    from app.storage.sqlite_store import SqliteStore

    ids: dict = {}
    with SqliteStore(fts_db) as store:
        accounts = AccountStore(store)
        ids["work"] = accounts.add("google", "ann.owner@example.invalid", "Work", mail_on=True)
        ids["home"] = accounts.add("apple", "me@example.invalid", "Home", direction="in-only")
        hub = ContactHub(store)
        ids["ann"] = hub.create(
            _card("Ann", "Smith", email="ann@acme.com", phone="+44 7700 900123", company="Acme",
                  notes="Met at the trade fair", groups=["Clients"], favourite=True),
            kept_in=[ids["work"]], source="google", source_ref="people/c1")
        hub.tag([ids["ann"]], "VIP")
        ids["bob"] = hub.create(_card("Bob", "Jones", email="bob@other.com", company="Other Ltd"))

        def message(path, sender, recipients, sent_at, attach=False):
            fid = store.upsert_file(path, size_bytes=1, mtime_ns=1, ext="pst",
                                    source_kind="pst_message", status="INDEXED")
            store.set_message(fid, store_path="D:/a.pst", entry_id=path.rsplit("/", 1)[1],
                              subject=f"About {path[-1]}", sender=sender, recipients=recipients,
                              sent_at=sent_at, has_attach=1 if attach else 0)
            store.replace_chunks(fid, [{"ordinal": 0, "text": f"mail {path}"}])

        message(MESSAGE_FROM, "Ann Smith <ann@acme.com>", "me@example.invalid", 1_700_000_000, True)
        message(MESSAGE_TO, "me@example.invalid", "Ann Smith <ann@acme.com>", 1_710_000_000)
        message(MESSAGE_LOOKALIKE, "joann@acme.com", "me@example.invalid", 1_720_000_000)
        message(MESSAGE_OTHER, "Bob <bob@other.com>", "me@example.invalid", 1_705_000_000)
        store.upsert_file(ATTACHMENT, size_bytes=4096, mtime_ns=1, ext="pdf",
                          source_kind="pst_message", status="INDEXED")
        photo = store.upsert_file(PHOTO, size_bytes=10, mtime_ns=1_600_000_000 * 10**9, ext="jpg",
                                  source_kind="file", status="INDEXED")
        pile = store.create_pile("Ann")
        store.assign_face(store.add_face(photo, (0.1, 0.1, 0.2, 0.2), b"x" * 8), pile)
        with store.write() as conn:
            conn.execute("INSERT INTO contact_pile_links (contact_id, pile_id, state) "
                         "VALUES (?, ?, 'accepted')", (ids["ann"], pile))
    return ids


@pytest.fixture()
def world(tmp_path):
    from app.core.config import load_settings

    settings = load_settings(Path(env_file(tmp_path)))
    settings.fts_db.parent.mkdir(parents=True, exist_ok=True)
    return settings, seed(settings.fts_db)


def tools_for(settings):
    from app.serve.mcp import IndexTools
    from tests.unit.conftest import _NoModel

    return IndexTools(settings, lambda: (_NoModel(), None, None))


def call(server, name, arguments=None):
    from mcp import Client

    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})

    result = asyncio.run(go())
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    return content if content is not None else json.loads(result.content[0].text)


# -- 8b: find_contact ---------------------------------------------------------

def test_find_contact_by_name_email_phone_and_company(world):
    settings, ids = world
    tools = tools_for(settings)
    by_name = tools.find_contact("Ann")["contacts"]
    assert [c["contact_id"] for c in by_name] == [ids["ann"]]
    assert by_name[0]["name"] == "Ann Smith" and by_name[0]["company"] == "Acme"
    by_email = tools.find_contact("ANN@acme.com")["contacts"]
    assert by_email[0]["contact_id"] == ids["ann"] and by_email[0]["matched"] == "email"
    by_phone = tools.find_contact("07700 900123")["contacts"]
    assert by_phone and by_phone[0]["contact_id"] == ids["ann"] and by_phone[0]["matched"] == "phone"
    assert [c["contact_id"] for c in tools.find_contact("Other Ltd")["contacts"]] == [ids["bob"]]
    assert tools.find_contact("nobody at all")["contacts"] == []


def test_find_contact_ranks_an_exact_match_first_and_caps_the_limit(world):
    settings, ids = world
    tools = tools_for(settings)
    from app.contacts.hub import ContactHub
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        hub = ContactHub(store)
        for n in range(60):
            hub.create(_card(f"Anna{n}", "Smith", email=f"anna{n}@acme.com"))
    assert len(tools.find_contact("Smith", limit=500)["contacts"]) == 50
    assert len(tools.find_contact("Smith", limit=3)["contacts"]) == 3
    first = tools.find_contact("ann@acme.com")["contacts"][0]
    assert first["contact_id"] == ids["ann"]


def test_find_contact_asks_for_something_and_does_not_list_the_bin(world):
    settings, ids = world
    tools = tools_for(settings)
    assert "Give a name" in tools.find_contact("  ")["error"]
    from app.contacts.hub import ContactHub
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        ContactHub(store).delete(ids["bob"])
    assert tools.find_contact("bob@other.com")["contacts"] == []
    assert "No contact" in tools.get_contact(ids["bob"])["error"]


# -- 8b: get_contact ----------------------------------------------------------

def test_get_contact_has_every_field_with_label_and_source_and_account_names(world):
    settings, ids = world
    answer = tools_for(settings).get_contact(ids["ann"])
    assert answer["name"] == "Ann Smith" and answer["company"] == "Acme"
    assert answer["favourite"] is True and answer["groups"] == ["Clients"]
    assert answer["tags"] == ["VIP"] and answer["notes"] == "Met at the trade fair"
    email = next(f for f in answer["fields"] if f["kind"] == "email")
    assert email["label"] == "work" and email["value"] == "ann@acme.com"
    assert email["sources"] == [{"source": "google", "ref": "people/c1"}]
    phone = next(f for f in answer["fields"] if f["kind"] == "phone")
    assert phone["label"] == "mobile"
    given = next(d for d in answer["details"] if d["field"] == "given")
    assert given["value"] == "Ann" and given["sources"][0]["source"] == "google"
    # By NAME, never the account's id or sign-in.
    assert answer["kept_in"] == ["Work"]


def test_get_contact_unknown_or_bad_id_is_an_answer_not_a_raise(world):
    settings, _ = world
    tools = tools_for(settings)
    assert "No contact" in tools.get_contact(99999)["error"]
    assert "No contact" in tools.get_contact("x")["error"]


# -- 8b: contact_activity -----------------------------------------------------

def test_contact_activity_names_mail_files_photos_and_dates(world):
    settings, ids = world
    answer = tools_for(settings).contact_activity(ids["ann"])
    paths = [m["path"] for m in answer["messages"]]
    # Newest first; the look-alike address and Bob's mail are not hers.
    assert paths == [MESSAGE_TO, MESSAGE_FROM]
    assert answer["messages"][0]["direction"] == "to them"
    assert answer["messages"][1]["direction"] == "from them"
    assert answer["messages"][1]["from"].startswith("Ann Smith") and answer["messages"][1]["sent"]
    assert answer["first_contact"].startswith("2023-11-1")
    assert answer["last_contact"].startswith("2024-03-")
    [exchanged] = answer["files_exchanged"]
    assert exchanged["path"] == ATTACHMENT and exchanged["name"] == "quote.pdf"
    assert exchanged["type"] == "pdf" and exchanged["message"]["path"] == MESSAGE_FROM
    assert answer["photo_match"] == "accepted"
    assert [p["path"] for p in answer["photos"]] == [PHOTO]
    assert answer["emails"] == ["ann@acme.com"]


def test_contact_activity_for_a_contact_with_no_mail_or_photos(world):
    settings, ids = world
    answer = tools_for(settings).contact_activity(ids["bob"], limit=500)
    assert [m["path"] for m in answer["messages"]] == [MESSAGE_OTHER]
    assert answer["files_exchanged"] == [] and answer["photos"] == []
    assert answer["photo_match"] == "none"
    from app.contacts.hub import ContactHub
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        nomail = ContactHub(store).create(_card("Cy", "Nomail"))
    quiet = tools_for(settings).contact_activity(nomail)
    assert quiet["messages"] == [] and "no email address" in quiet["note"]


def test_a_suggested_photo_match_is_not_listed_as_theirs(world):
    settings, ids = world
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store, store.write() as conn:
        conn.execute("UPDATE contact_pile_links SET state = 'suggested' WHERE contact_id = ?",
                     (ids["ann"],))
    answer = tools_for(settings).contact_activity(ids["ann"])
    assert answer["photo_match"] == "suggested" and answer["photos"] == []


def test_with_no_index_the_contact_tools_say_so(tmp_path):
    from app.core.config import load_settings
    from app.serve.mcp import IndexTools

    tools = IndexTools(load_settings(Path(env_file(tmp_path))), lambda: pytest.fail("model"))
    for answer in (tools.find_contact("x"), tools.get_contact(1), tools.contact_activity(1)):
        assert "No index has been built" in answer["error"]


# -- 8c: the accounts in index_status ----------------------------------------

def test_index_status_reports_each_account_without_its_sign_in(world):
    settings, ids = world
    from app.accounts.store import AccountStore
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        AccountStore(store).update(ids["work"], last_synced_at=1_700_000_000,
                                   last_error="Sign-in expired", last_error_code="ERR_TOKEN")
    status = tools_for(settings).index_status()
    assert status["files"] >= 1 and status["messages"] == 4
    work, home = status["accounts"]
    assert work == {"name": "Work", "provider": "google", "mail_sync": "on",
                    "contact_sync": "on", "contact_direction": "two-way",
                    "last_sync": work["last_sync"], "last_error": "Sign-in expired",
                    "last_error_code": "ERR_TOKEN"}
    assert work["last_sync"].startswith("2023-11-1")
    assert home["provider"] == "apple" and home["mail_sync"] == "off"
    assert home["contact_direction"] == "in-only" and home["last_sync"] == ""
    # No account address, id or any field beyond these.
    assert set(work) == {"name", "provider", "mail_sync", "contact_sync", "contact_direction",
                         "last_sync", "last_error", "last_error_code"}


# -- 8e: the bridge carries them, and a stopped server answers the same ------

def test_the_bridge_carries_the_new_tools_and_a_stopped_server_answers_each_the_same(world):
    from app.serve.mcp import STOPPED, TOOL_NAMES, McpHost, bridge_server
    from tests.unit.test_mcp_server import _free_port

    settings, ids = world
    host, port = McpHost(), _free_port()
    url = host.start(tools_for(settings), port, "the-key")
    arguments = {"find_contact": {"query": "Ann"}, "get_contact": {"contact_id": ids["ann"]},
                 "contact_activity": {"contact_id": ids["ann"]}}
    try:
        bridge = bridge_server(url, "the-key")
        assert call(bridge, "find_contact", arguments["find_contact"])["contacts"]
        assert call(bridge, "get_contact", arguments["get_contact"])["name"] == "Ann Smith"
        assert call(bridge, "contact_activity", arguments["contact_activity"])["messages"]
        assert call(bridge, "index_status")["accounts"]
    finally:
        host.stop()
    stopped = {name: call(bridge_server(url, "the-key"), name, arguments.get(name))
               for name in TOOL_NAMES if name in ("index_status", *arguments)}
    assert set(stopped) == {"index_status", *arguments}
    for name, answer in stopped.items():
        assert answer == {"error": STOPPED}, name


def test_the_new_tools_are_listed_with_the_old(world):
    from mcp import Client

    from app.serve.mcp import TOOL_NAMES, build_server

    assert {"find_contact", "get_contact", "contact_activity"} <= set(TOOL_NAMES)
    assert TOOL_NAMES[:4] == ("search", "find_files", "read_text", "index_status")

    async def listed():
        async with Client(build_server(tools_for(world[0]))) as client:
            return await client.list_tools()

    names = [t.name for t in asyncio.run(listed()).tools]
    assert sorted(names) == sorted(TOOL_NAMES)


def test_instructions_gain_contacts_and_accounts_and_keep_the_old_words():
    from app.serve.mcp import INSTRUCTIONS

    assert INSTRUCTIONS.startswith(
        "Leasha is the owner's local search index of their own files and Outlook mail. "
        "Use `search` to find documents and messages by what they say, `find_files` to find a "
        "file by its name, and `read_text` for the whole indexed text of one result. Every answer "
        "names the file it came from - cite the path. Nothing here can change, move or open a "
        "file.")
    assert "find_contact" in INSTRUCTIONS and "accounts" in INSTRUCTIONS
