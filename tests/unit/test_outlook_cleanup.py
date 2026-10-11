r"""Order 1l: copies inside one mailbox, moved to its Deleted Items through Outlook.

Layer: L2

2026-10-11, the owner: "yes write the order for the outlook clean-up and
execute". The one place Leasha writes to a mail archive, so it reads first,
moves only what was counted, and never deletes. Driven here through a fake
Outlook built from the same attributes `email_pst` reads.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from app.extract.email_pst import _ComStore
from app.extract.outlook_cleanup import DELETED_ITEMS, find_copies, move_copies, plan_lines

WHEN = datetime.datetime(2018, 3, 7, 14, 5, tzinfo=datetime.timezone.utc)


class Attachment:
    def __init__(self, name):
        self.FileName, self.Size = name, 10


class Item:
    def __init__(self, entry_id, subject="Quotation", body="Please find the quote.", when=WHEN,
                 sender="hans@example.com", attachments=()):
        self.EntryID, self.Subject, self.Body = entry_id, subject, body
        self.SentOn = self.ReceivedTime = when
        self.SenderEmailAddress = sender
        self.Recipients = []
        self.Attachments = [Attachment(n) for n in attachments]
        self.moved_to = None

    def Move(self, folder):                                     # noqa: N802 - Outlook's name
        self.moved_to = folder
        folder.Items.append(self)


class Items(list):
    def Sort(self, *_args):                                     # noqa: N802
        pass


class Folder:
    def __init__(self, name, entry_id, items=(), folders=()):
        self.Name, self.EntryID = name, entry_id
        self.Items = Items(items)
        self.Folders = list(folders)


class Store:
    def __init__(self, path, root, deleted):
        self.FilePath, self.DisplayName, self.StoreID = str(path), "Archive", "store-1"
        self._root, self._deleted = root, deleted

    def GetRootFolder(self):                                    # noqa: N802
        return self._root

    def GetDefaultFolder(self, which):                          # noqa: N802
        assert which == DELETED_ITEMS
        return self._deleted


class Namespace:
    def __init__(self, items):
        self._items = {item.EntryID: item for item in items}

    def GetItemFromID(self, entry_id, store_id):                # noqa: N802
        assert store_id == "store-1"
        return self._items[entry_id]


class Session:
    """What `Win32ComSession` offers: attach, stores, the namespace, close."""

    def __init__(self, store, items):
        self.store, self._namespace = store, Namespace(items)
        self.attached, self.closed = [], False

    def attach(self, path):
        self.attached.append(str(path))

    def stores(self):
        return [_ComStore(self.store)]

    def close(self):
        self.closed = True


def _mailbox(tmp_path: Path):
    a = Item("A", attachments=["quote.pdf"])
    b = Item("B", attachments=["quote.pdf"])                    # a copy of A, in another folder
    c = Item("C", attachments=["quote-v2.pdf"])                 # same words, another attachment: stays
    d = Item("D", subject="Quotation (revised)")                # another subject: stays
    e = Item("E", attachments=["quote.pdf"])                    # a copy of A already in Deleted Items
    deleted = Folder("Deleted Items", "deleted", items=[e])
    inbox = Folder("Inbox", "inbox", items=[a, c, d])
    sent = Folder("Sent Items", "sent", items=[b])
    root = Folder("Top of Personal Folders", "root", folders=[inbox, sent, deleted])
    path = tmp_path / "2018.pst"
    session = Session(Store(path, root, deleted), [a, b, c, d, e])
    return path, session, {"a": a, "b": b, "c": c, "d": d, "e": e, "deleted": deleted}


def test_only_a_true_copy_is_found_and_deleted_items_is_left_out(tmp_path):
    path, session, items = _mailbox(tmp_path)
    plan = find_copies(path, session_factory=lambda: session)
    assert [c.entry_id for c in plan.copies] == ["B"]
    assert plan.copies[0].kept_entry_id == "A"
    assert plan.copies[0].folder.endswith("Sent Items")
    assert plan.examined == 4                                 # Deleted Items not looked at
    assert session.closed
    assert all(item.moved_to is None for item in items.values() if hasattr(item, "moved_to")), \
        "counting moved something"


def test_moving_takes_only_the_counted_copy_to_deleted_items(tmp_path):
    path, session, items = _mailbox(tmp_path)
    plan = find_copies(path, session_factory=lambda: session)
    moved, left = move_copies(plan, session_factory=lambda: session)
    assert (moved, left) == (1, 0)
    assert items["b"].moved_to is items["deleted"]
    assert all(items[k].moved_to is None for k in "acde")


def test_an_item_that_changed_since_it_was_counted_is_left_where_it_is(tmp_path):
    path, session, items = _mailbox(tmp_path)
    plan = find_copies(path, session_factory=lambda: session)
    items["b"].Subject = "Quotation - edited since"
    assert move_copies(plan, session_factory=lambda: session) == (0, 1)
    assert items["b"].moved_to is None


def test_the_words_say_how_many_and_that_nothing_is_deleted(tmp_path):
    path, session, _items = _mailbox(tmp_path)
    words = "\n".join(plan_lines(find_copies(path, session_factory=lambda: session)))
    assert "2018.pst: 1 copies" in words and "Deleted Items" in words and "restore" in words


def test_a_mailbox_with_no_copies_moves_nothing(tmp_path):
    path, session, items = _mailbox(tmp_path)
    items["b"].Body = "Something else entirely."
    plan = find_copies(path, session_factory=lambda: session)
    assert plan.copies == [] and move_copies(plan, session_factory=lambda: session) == (0, 0)
    assert "Nothing to move" in plan_lines(plan)[0]


class _Columns(list):
    def RemoveAll(self):                                        # noqa: N802
        self.clear()

    def Add(self, name):                                        # noqa: N802
        self.append(name)


class _Table:
    """`Folder.GetTable()`: the listed columns of every item, row by row."""

    def __init__(self, items):
        self._items, self._at, self.Columns = list(items), 0, _Columns()

    @property
    def EndOfTable(self):                                       # noqa: N802
        return self._at >= len(self._items)

    def GetArray(self, most):                                   # noqa: N802
        """Up to `most` rows, each the listed columns' values, as Outlook gives them."""
        block = []
        for item in self._items[self._at:self._at + most]:
            by_name = {"EntryID": item.EntryID, "Subject": item.Subject,
                       "SenderName": item.SenderEmailAddress, "SentOn": item.SentOn, "Size": 100}
            block.append([by_name[name] for name in self.Columns])
        self._at += len(block)
        return block


class TableFolder(Folder):
    def GetTable(self):                                         # noqa: N802
        return _Table(self.Items)


def test_the_tables_name_the_candidates_and_only_those_are_opened(tmp_path, monkeypatch):
    path, session, items = _mailbox(tmp_path)
    root = session.store._root

    def as_table(folder):
        table = TableFolder(folder.Name, folder.EntryID, folder.Items, [as_table(f) for f in folder.Folders])
        if folder.EntryID == "deleted":
            session.store._deleted = table
        return table

    session.store._root = as_table(root)
    opened = []
    real = session._namespace.GetItemFromID
    monkeypatch.setattr(session._namespace, "GetItemFromID",
                        lambda eid, sid: opened.append(eid) or real(eid, sid))
    plan = find_copies(path, session_factory=lambda: session)
    assert [c.entry_id for c in plan.copies] == ["B"]
    assert plan.examined == 4
    assert "D" not in opened, "an item no other matched on subject was opened"
