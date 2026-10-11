"""Order 1k section 8c, 8g, D15: no tool writes, and no answer holds a secret.

Layer: L6

D15 (the owner, 2026-10-11: "the only read for mcp"): the server lists tools
that read. This file fails if one can write to the contact hub, an account or
the index - by what happens when every tool is called, and by what the tool
functions' source says - and if any tool's answer contains a secret that was
put in a fake Credential Manager or in the settings.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import textwrap

import pytest

from tests.unit.test_mcp_contacts import ATTACHMENT, MESSAGE_FROM, SECRET, CLIENT_ID, call, seed, tools_for

#: Names that write: hub methods, store methods and SQL verbs.
WRITE_NAMES = {"create", "update", "delete", "restore", "merge", "set_kept_in", "set_link",
               "drop_link", "link", "add", "add_to_group", "remove_from_group", "set_company",
               "set_favourite", "tag", "create_tag", "delete_tag", "rename_group", "undo_entry",
               "undo_batch", "undo_sync", "purge_bin", "empty_bin", "save_token", "save_settings",
               "update_settings", "reset_token", "start_run", "finish_run", "record_success",
               "record_failure", "reorder", "delete_rows", "upsert_file", "set_message",
               "replace_chunks", "set_state", "write", "batch", "executescript", "executemany",
               "commit", "add_face", "assign_face", "create_pile", "note", "add_conflict",
               "acknowledge_conflicts", "reject_pair", "unreject_pair", "save_smart_group",
               "delete_smart_group", "confirm_deleted_elsewhere", "restore_to_account"}
WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|PRAGMA)\b", re.IGNORECASE)
TOOL_METHODS = ("search", "find_files", "read_text", "index_status", "find_contact",
                "get_contact", "contact_activity")


class MemoryKeyring:
    """An in-memory keyring backend: nothing here touches a real Credential Manager."""

    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


def _fake_keyring():
    import keyring
    from keyring.backend import KeyringBackend

    class Memory(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self.items = {}

        def get_password(self, service, username):
            return self.items.get((service, username))

        def set_password(self, service, username, password):
            self.items[(service, username)] = password

        def delete_password(self, service, username):
            self.items.pop((service, username), None)

    previous = keyring.get_keyring()
    backend = Memory()
    keyring.set_keyring(backend)
    return backend, previous


@pytest.fixture()
def world(tmp_path):
    from pathlib import Path

    from app.core.config import load_settings
    from tests.unit.test_cli_wiring import env_file

    settings = load_settings(Path(env_file(tmp_path)))
    settings.fts_db.parent.mkdir(parents=True, exist_ok=True)
    return settings, seed(settings.fts_db)


def _arguments(ids):
    return {"search": {"query": "mail"}, "find_files": {"name": "quote"},
            "read_text": {"path": MESSAGE_FROM}, "index_status": {},
            "find_contact": {"query": "Ann"}, "get_contact": {"contact_id": ids["ann"]},
            "contact_activity": {"contact_id": ids["ann"]}}


def _counts(settings):
    """Every table's row count, plus a digest of the accounts and the hub."""
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        names = [r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '%\\_fts%' "
            "ESCAPE '\\' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {}
        for name in names:
            try:
                counts[name] = store.conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except Exception:                    # noqa: BLE001 - a virtual table without a reader
                continue
        digest = [tuple(r) for r in store.conn.execute(
            "SELECT * FROM accounts ORDER BY id")] + [tuple(r) for r in store.conn.execute(
                "SELECT id, rev, edited_at, updated_at, deleted_at FROM contacts ORDER BY id")]
        links = [tuple(r) for r in store.conn.execute("SELECT * FROM contact_links ORDER BY id")]
        return counts, digest, links


# -- every tool, called: nothing changes -------------------------------------

def test_every_listed_tool_leaves_the_hub_the_accounts_and_the_index_as_they_were(world):
    from mcp import Client

    from app.serve.mcp import TOOL_NAMES, build_server

    settings, ids = world
    server = build_server(tools_for(settings))

    async def listed():
        async with Client(server) as client:
            return await client.list_tools()

    names = [t.name for t in asyncio.run(listed()).tools]
    assert sorted(names) == sorted(TOOL_NAMES) and set(names) <= set(_arguments(ids))
    before = _counts(settings)
    assert before[0]["contact_journal"] > 0                 # the journal is being counted
    for name in names:
        answer = call(server, name, _arguments(ids)[name])
        assert "error" not in answer or name == "read_text", (name, answer)
        # And again with a limit, a miss and a bad id: every path, not only the good one.
    for name, arguments in (("find_contact", {"query": "zzz-nobody"}),
                            ("get_contact", {"contact_id": 99999}),
                            ("contact_activity", {"contact_id": 99999}),
                            ("find_contact", {"query": "ann@acme.com", "limit": 1}),
                            ("contact_activity", {"contact_id": ids["bob"], "limit": 1})):
        call(server, name, arguments)
    assert _counts(settings) == before


def test_a_database_opened_read_only_still_answers_every_tool(world, tmp_path):
    """The strongest form of 'reads only': the same answers from a copy with writes refused."""
    import shutil
    import sqlite3

    settings, ids = world
    before = _counts(settings)
    uri = f"file:{settings.fts_db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO contact_tags (name, colour) VALUES ('x', '#fff')")
    finally:
        conn.close()
    assert shutil.copy(settings.fts_db, tmp_path / "copy.db")
    assert _counts(settings) == before


# -- the source says so too ---------------------------------------------------

def _tool_sources():
    """The AST of every function a tool runs: the tool methods and the module-level helpers
    they call (everything defined in the module that is not the server, the key or the host)."""
    import app.serve.mcp as module

    tree = ast.parse(inspect.getsource(module))
    wanted = {"IndexTools"}
    helpers = {"_accounts_of", "_account_names", "_contact_of", "_hub_of", "_is_address_in",
               "_messages", "_message_entry", "_mail_with", "_files_exchanged", "_photos_of",
               "_mail_card", "_mail_of", "_result", "_when", "_preferences_of"}
    found = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in wanted:
            found += [n for n in node.body if isinstance(n, ast.FunctionDef)
                      and (n.name in TOOL_METHODS or n.name in ("_result", "_missing"))]
        elif isinstance(node, ast.FunctionDef) and node.name in helpers:
            found.append(node)
    return found


def _without_docstring(function):
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    return body


def test_the_tool_functions_name_no_write_method_and_hold_no_write_sql():
    functions = _tool_sources()
    assert {f.name for f in functions} >= set(TOOL_METHODS) | {"_photos_of", "_files_exchanged"}
    for function in functions:
        for statement in _without_docstring(function):
            for node in ast.walk(statement):
                if isinstance(node, ast.Attribute):
                    assert node.attr not in WRITE_NAMES, f"{function.name} uses .{node.attr}"
                elif isinstance(node, ast.Name):
                    assert node.id not in WRITE_NAMES - {"link", "add", "note", "tag", "batch"}, (
                        f"{function.name} uses {node.id}")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert not WRITE_SQL.search(node.value), (
                        f"{function.name} holds SQL that writes: {node.value[:60]!r}")


def test_the_server_registers_no_tool_beyond_the_read_ones_and_each_is_marked_read_only():
    import app.serve.mcp as module

    registered = [n for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(module._register))))
                  if isinstance(n, ast.AsyncFunctionDef)]
    assert sorted(n.name for n in registered) == sorted(module.TOOL_NAMES)
    for function in registered:
        called = {n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)}
        assert called & set(module.TOOL_NAMES) and not called & WRITE_NAMES, function.name
        assert any("server.tool" in ast.unparse(d) and "read_only" in ast.unparse(d)
                   for d in function.decorator_list), function.name


def test_the_module_imports_no_write_method_of_the_hub_or_the_accounts():
    import app.serve.mcp as module

    tree = ast.parse(inspect.getsource(module))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                for alias in node.names}
    assert imported.isdisjoint(WRITE_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
                ("app.contacts", "app.accounts")):
            assert {a.name for a in node.names} <= {
                "ContactHub", "AccountStore", "redact", "SCALARS", "field_key"}, node.module
    # The hub class is reached only to read: its read-only methods are the ones named.
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for read in ("find_by_email", "find_by_phone", "search", "get_card", "get_row", "sources",
                 "kept_in", "tags_of"):
        assert read in names


# -- 8c: no secret in any answer ---------------------------------------------

def test_no_tool_answers_with_a_secret_from_the_keyring_or_the_settings(world):
    from app.accounts.secrets import SecretStore, account_key, client_secret_key
    from app.accounts.store import AccountStore
    from app.serve.mcp import build_server
    from app.storage.sqlite_store import SqliteStore

    settings, ids = world
    backend, previous = _fake_keyring()
    import keyring

    try:
        secrets = SecretStore()
        secrets.put(account_key(ids["work"], "refresh_token"), SECRET)
        secrets.put(account_key(ids["home"], "app_password"), SECRET + "-app")
        secrets.put(client_secret_key("google"), SECRET + "-client")
        assert backend.items                                   # the secrets really are in the fake store
        with SqliteStore(settings.fts_db) as store:
            accounts = AccountStore(store)
            accounts.update_settings(google_client_id=CLIENT_ID, microsoft_client_id=CLIENT_ID + "-ms")
            # A sign-in failure that quotes the token, as a provider's error text can.
            accounts.update(ids["work"], last_error=f"invalid_grant for refresh_token={SECRET}",
                            last_error_code="ERR_TOKEN")
            accounts.save_token(ids["work"], "contacts", SECRET + "-sync-token")
        server = build_server(tools_for(settings))
        every = {**_arguments(ids), "read_text": {"path": ATTACHMENT}}
        answers = {name: json.dumps(call(server, name, arguments), ensure_ascii=False)
                   for name, arguments in every.items()}
        answers["search:secret"] = json.dumps(call(server, "search", {"query": SECRET}))
        answers["find_contact:client"] = json.dumps(call(server, "find_contact", {"query": CLIENT_ID}))
        for fragment in (SECRET, SECRET + "-app", SECRET + "-client", SECRET + "-sync-token",
                         CLIENT_ID, CLIENT_ID + "-ms", "refresh_token=", "oauth-client-secret",
                         "account:1:"):
            for name, text in answers.items():
                assert fragment not in text, f"{name} answered with {fragment!r}"
        # The account line is still useful: its error is there, scrubbed.
        status = call(server, "index_status")
        work = next(a for a in status["accounts"] if a["name"] == "Work")
        assert work["last_error_code"] == "ERR_TOKEN" and "invalid_grant" in work["last_error"]
    finally:
        keyring.set_keyring(previous)


import json  # noqa: E402 - used by the test above; kept beside it
