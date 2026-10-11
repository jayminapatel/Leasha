"""Which mailbox a message is in, in words and in SQL - for the Mail list on the left. Qt-free.

Layer: L5 presenter. Order 1n, extended 2026-10-11 (the owner: "no archive is not a good description ...
if mail from live it should show the mail account", and a click on a live folder showed nothing).

Three kinds of place hold mail, told apart by one key (the "archive"):

* an Outlook archive: its `.pst` path (`messages.store_path`);
* **a live account**: `account://<id>` - its mail was fetched, and each message's `files.path` is
  `account://<id>/<folder>/<remote id>` (`accounts.mail_ingest.account_path`), with no `store_path`;
* **messages saved as files**: `""` - `.eml` and `.msg` files, in no archive and in no account.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any, Mapping
from urllib.parse import quote

from app.storage.like import like_escape

__all__ = ["ACCOUNT_PREFIX", "LOOSE", "LOOSE_TIP", "is_account", "account_id", "place_label", "place_tip",
           "place_icon", "folder_sql"]

ACCOUNT_PREFIX = "account://"
#: The key of messages that are files on disk, in no archive and in no account.
LOOSE = ""
LOOSE_TIP = "Messages saved as .eml or .msg files on this computer - not in an Outlook archive and not fetched from an account"


def is_account(archive: str) -> bool:
    return str(archive or "").startswith(ACCOUNT_PREFIX)


def account_id(archive: str) -> str:
    return str(archive)[len(ACCOUNT_PREFIX):].strip("/")


def place_label(archive: str, names: Mapping[str, str]) -> str:
    """What the mailbox is called: the account's own name, the archive's file name, or what loose files are."""
    if is_account(archive):
        return names.get(account_id(archive)) or f"Account {account_id(archive)}"
    if not archive:
        return "Messages saved as files"
    return PureWindowsPath(archive).name or archive


def place_tip(archive: str, names: Mapping[str, str]) -> str:
    if is_account(archive):
        return f"Mail fetched live from {names.get(account_id(archive), 'an account')}"
    return LOOSE_TIP if not archive else archive


def place_icon(archive: str) -> str:
    return "cloud" if is_account(archive) else "archive" if archive else "file"


def folder_sql(archive: str, path: str) -> tuple[str, list[Any]]:
    """The `AND ...` fragment (and its parameters) narrowing a message query to a mailbox, or to a folder in
    it with the folders inside it. The query joins `messages m` and `files f`."""
    path = str(path or "")
    folder = "(? = '' OR m.folder_path = ? OR m.folder_path LIKE ? ESCAPE '\\')"
    folder_args = [path, path, like_escape(path + "/") + "%"]
    if is_account(archive):
        ident = account_id(archive)
        base = like_escape(f"{ACCOUNT_PREFIX}{ident}/") + "%"
        if not path:
            return " AND f.path LIKE ? ESCAPE '\\'", [base]
        # the folder is in the path, percent-encoded as one part, or in the message's own `folder_path`
        own = like_escape(f"{ACCOUNT_PREFIX}{ident}/{quote(path, safe='')}/") + "%"
        inside = like_escape(f"{ACCOUNT_PREFIX}{ident}/{quote(path + '/', safe='')}") + "%"
        return (" AND f.path LIKE ? ESCAPE '\\' AND (m.folder_path = ? OR m.folder_path LIKE ? ESCAPE '\\'"
                " OR f.path LIKE ? ESCAPE '\\' OR f.path LIKE ? ESCAPE '\\')",
                [base, path, like_escape(path + "/") + "%", own, inside])
    if not archive:
        # Not the attachments inside an archive (`pst://<archive>/.../attachments/...`): those are not messages
        # saved as files, and the Mail list had 4,000 of them under that name (the owner, 2026-10-12).
        return (" AND m.store_path IS NULL AND f.path NOT LIKE 'account://%' AND f.path NOT LIKE 'pst://%' AND "
                + folder, folder_args)
    return " AND m.store_path = ? AND " + folder, [archive, *folder_args]
