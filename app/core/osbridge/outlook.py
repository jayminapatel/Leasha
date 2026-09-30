r"""Show one message in Outlook. Windows only - there is no Outlook COM on a Mac.

Layer: L0

Order 0y section 4d: "Open in Outlook" on a previewed message. Leasha's index
holds a message's own words and deliberately leaves out the quoted thread under
them; the full message is in the archive, and Outlook is the program that shows
it.

**Nothing here runs unless somebody clicks the button.** `show_in_outlook`
starts Outlook (or reaches the one already running) and, when the message's
archive is not already in Outlook's list of data files, asks Outlook to open it
- which Outlook then keeps open, because the message window needs it. That is a
visible change in another program, so it must only ever follow an explicit
click: it is never called while previewing, indexing or testing. The interface
reaches it through a seam (`app/ui/widgets/mail_open.py`) and every test passes
a stand-in.

**Read-only** (non-negotiable 10). The message is displayed; nothing is sent,
saved, moved or changed, and the archive's contents are not written to.

**(UNCONFIRMED against a real Outlook.)** The calls are the documented Outlook
object model - `Namespace.AddStore`, `Namespace.GetItemFromID`,
`MailItem.Display` - and `pst_entry_id` follows the published layout of an
identifier for an item in a `.pst`, but none of it has been run against
Outlook from this code: acceptance A9 is the owner's check.
"""

from __future__ import annotations

from typing import Any

from app.core.osbridge._platform import is_windows

__all__ = ["show_in_outlook", "pst_entry_id"]

#: An identifier for an item in a `.pst`, as Outlook writes it in hexadecimal:
#: 4 bytes of flags, the archive's own 16-byte identifier, the item's 4-byte
#: number inside the archive. 24 bytes, 48 characters.
_PST_ENTRY_ID_CHARS = 48


def _is_hex(text: str) -> bool:
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


def pst_entry_id(entry_id: str, root_entry_id: str) -> str:
    r"""The identifier Outlook wants for a message, from the one the index holds.

    Two readers write `messages.entry_id`, and they write different things:

    * The Outlook reader stores Outlook's own identifier - a long hexadecimal
      string. It is returned as it is.
    * The libpff reader stores the message's **number inside the archive**, in
      decimal (`2097188`). Outlook's identifier for that message is the
      archive's own prefix followed by the number as four bytes, low byte
      first - so it is built from the identifier of the archive's root folder,
      which has the same prefix, with its last four bytes replaced.

    Raises `ValueError` for anything that is neither: libpff's last-resort key
    for a message with no number (`Inbox/Sub#1234`) cannot be found again, and
    saying so is better than asking Outlook for a message that is not there.
    """
    text = str(entry_id or "").strip()
    if not text:
        raise ValueError("this message has no identifier in the index")
    if len(text) >= 40 and _is_hex(text):
        return text
    if not text.isdigit():
        raise ValueError(f"'{text}' is not an identifier Outlook can look up")
    root = str(root_entry_id or "").strip()
    if len(root) != _PST_ENTRY_ID_CHARS or not _is_hex(root):
        raise ValueError("the archive's own identifier is not a .pst identifier")
    number = int(text)
    if not 0 < number < 2 ** 32:
        raise ValueError(f"'{text}' is not a message number inside an archive")
    return root[:-8] + number.to_bytes(4, "little").hex().upper()


def _find_store(namespace: Any, store_path: str) -> Any:
    wanted = str(store_path).replace("/", "\\").lower()
    for store in namespace.Stores:
        found = str(getattr(store, "FilePath", "") or "").replace("/", "\\").lower()
        if found == wanted:
            return store
    return None


def show_in_outlook(entry_id: str, store_path: str) -> None:
    """Show the message `entry_id` of the archive `store_path` in Outlook.

    **Call only from an explicit click, and only on a worker thread** - it
    starts a program and can take seconds. Raises whatever goes wrong (no
    Outlook, an archive Outlook will not open, a message it cannot find); the
    caller turns that into an error with a way out.
    """
    if not is_windows():
        raise OSError("Outlook can only be opened from Leasha on Windows")

    import pythoncom
    import win32com.client

    # COM is per-thread, and this runs on a worker - see `Win32ComSession`.
    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        namespace = outlook.GetNamespace("MAPI")
        store = _find_store(namespace, store_path)
        if store is None:
            # Outlook opens the archive and keeps it in its list: the message
            # window reads from it for as long as it is on screen, so it is
            # deliberately not removed again here.
            namespace.AddStore(str(store_path).replace("/", "\\"))
            store = _find_store(namespace, store_path)
        if store is None:
            raise OSError(f"Outlook did not open the archive {store_path}")
        wanted = pst_entry_id(entry_id, str(store.GetRootFolder().EntryID))
        namespace.GetItemFromID(wanted, store.StoreID).Display()
    finally:
        pythoncom.CoUninitialize()
