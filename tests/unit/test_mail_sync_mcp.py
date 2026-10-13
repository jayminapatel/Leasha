"""What AI programs can reach of live mail: `search` by account, the account on a message's card,
and an `account://` path `read_text` accepts (order 1k 8a).

Layer: L6 (test)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.accounts.mail_index import MailIndexer
from app.accounts.provider import RemoteMessage
from tests.unit.mail_sync_fakes import raw_message
from tests.unit.test_cli_wiring import env_file


@pytest.fixture()
def settings(tmp_path):
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore

    loaded = load_settings(Path(env_file(tmp_path)))
    loaded.fts_db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteStore(loaded.fts_db) as store:
        for address, name in (("me@home.com", "Home"), ("me@work.com", "Work")):
            store.conn.execute("INSERT INTO accounts (provider, address, name, colour, added_at) "
                               "VALUES ('google', ?, ?, '#fff', 0)", (address, name))
        store.conn.commit()
        indexer = MailIndexer(store)

        def put(account, remote, subject, body, folder="INBOX", **kw):
            msg = RemoteMessage(remote, raw_message(subject, body, **kw), folder=folder, received_at=1_709_640_000)
            indexer.apply_page(account, [indexer.prepare(account, msg)], [])

        put(1, "h1", "Boiler quote", "The boiler quote from the plumber.", message_id="<h1@x>",
            attachments=(("quote.txt", b"boiler quote attachment words", "text/plain"),))
        put(2, "w1", "Boiler contract", "The boiler contract for the office.", message_id="<w1@x>")
        put(1, "g1", "Gmail style", "Boiler words in a label-style mailbox.", folder="", message_id="<g1@x>")
        file_id = store.upsert_file("C:/work/boiler notes.pdf", parent_dir="C:/work", ext="pdf", size_bytes=10,
                                    mtime_ns=1_700_000_000 * 10**9, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": "Boiler notes for the house."}])
    return loaded


def _tools(settings):
    from app.serve.mcp import IndexTools
    from tests.unit.conftest import _NoModel

    return IndexTools(settings, lambda: (_NoModel(), None, None))


def test_search_names_the_account_beside_the_sender_and_date(settings):
    found = _tools(settings).search("boiler quote")
    mail = {r["path"]: r["mail"] for r in found["results"] if "mail" in r}
    assert mail["account://1/INBOX/h1"]["account"] == "Home"
    assert mail["account://1/INBOX/h1"]["from"] == "priya@example.com" and mail["account://1/INBOX/h1"]["sent"]
    attachment = mail["account://1/INBOX/h1/attachments/quote.txt"]
    assert attachment["account"] == "Home" and attachment["attachment"] == "quote.txt"
    assert all("mail" not in r for r in found["results"] if r["path"].startswith("C:/"))


def test_search_can_be_limited_to_one_account_by_name_or_address(settings):
    tools = _tools(settings)
    everything = {r["path"] for r in tools.search("boiler", limit=50)["results"]}
    assert {"account://1/INBOX/h1", "account://2/INBOX/w1", "account://1//g1", "C:/work/boiler notes.pdf"} <= everything
    for ask in ("Work", "work", "me@work.com", "2"):
        only = tools.search("boiler", account=ask)["results"]
        assert [r["path"] for r in only] == ["account://2/INBOX/w1"] and only[0]["mail"]["account"] == "Work"
    home = {r["path"] for r in tools.search("boiler", account="Home")["results"]}
    assert home == {"account://1/INBOX/h1", "account://1/INBOX/h1/attachments/quote.txt", "account://1//g1"} \
        or home == {"account://1/INBOX/h1", "account://1//g1"}
    assert len(tools.search("boiler", limit=1, account="Home")["results"]) == 1


def test_an_unknown_account_is_said_so_not_answered_empty(settings):
    answer = _tools(settings).search("boiler", account="Nobody")
    assert "No account is called 'Nobody'" in answer["error"]


def test_read_text_takes_the_path_as_search_returned_it(settings):
    answer = _tools(settings).read_text("account://1/INBOX/h1")
    assert "boiler quote from the plumber" in answer["text"] and answer["mail"]["account"] == "Home"
    assert answer["path"] == "account://1/INBOX/h1"


def test_read_text_also_resolves_the_account_by_name_or_address(settings):
    tools = _tools(settings)
    for quoted in ("account://Work/INBOX/w1", "account://me@work.com/INBOX/w1", "account://work/INBOX/w1"):
        answer = tools.read_text(quoted)
        assert "boiler contract" in answer["text"], quoted
        assert answer["path"] == "account://2/INBOX/w1" and answer["mail"]["account"] == "Work"
    label_style = tools.read_text("account://Home//g1")
    assert "label-style" in label_style["text"] and label_style["path"] == "account://1//g1"
    assert "not in the index" in tools.read_text("account://Home/INBOX/nothing")["error"]
    assert "not in the index" in tools.read_text("account://Nobody/INBOX/w1")["error"]


@pytest.mark.slow
def test_the_tool_is_offered_with_an_account_and_stays_read_only(settings):
    from mcp import Client

    from app.serve.mcp import INSTRUCTIONS, build_server

    async def go():
        async with Client(build_server(_tools(settings))) as client:
            listed = await client.list_tools()
            result = await client.call_tool("search", {"query": "boiler", "account": "Work"})
            return listed, result

    listed, result = asyncio.run(go())
    search = next(t for t in listed.tools if t.name == "search")
    assert "account" in search.input_schema["properties"] and "query" in search.input_schema["required"]
    assert "account" not in search.input_schema["required"] and search.annotations.read_only_hint is True
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        content = content["result"]
    answer = content if content is not None else json.loads(result.content[0].text)
    assert [r["path"] for r in answer["results"]] == ["account://2/INBOX/w1"]
    assert INSTRUCTIONS.startswith("Leasha is the owner's local search index of their own files and Outlook mail.")


def test_the_bridge_sends_the_account_only_when_there_is_one():
    from app.serve.mcp import _Forward

    sent = []
    bridge = _Forward.__new__(_Forward)

    async def call(name, arguments):
        sent.append((name, arguments))
        return {}

    bridge._call = call
    asyncio.run(bridge.search("boiler"))
    asyncio.run(bridge.search("boiler", 5, "Work"))
    assert sent == [("search", {"query": "boiler", "limit": 10}),
                    ("search", {"query": "boiler", "limit": 5, "account": "Work"})]
