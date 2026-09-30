r"""Order 0y section 4c: the conversation, under the header card.

Layer: L1 / L5

> **4c** The conversation. Under the header, `4 messages in this conversation`:
> a short list (sender, date, first line) from `messages.conversation`, which is
> already indexed. Clicking one shows it in the same pane. One indexed query, on
> a worker. Budget (section 5): under 20 ms.

* `SqliteStore.conversation_messages` is the one query, on `idx_messages_conv`.
* `presenter.mail.conversation_lines` words each line without Qt.
* `preview_loader.mail_preview` asks it on the worker, with the message.
* `widgets/mail_card.py` draws the list; the pane shows the clicked message.
* The budget is measured over a synthetic store, like the search tier tests.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from types import SimpleNamespace

import pytest

from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.mail import (
    CONVERSATION_SHOWN,
    conversation_heading,
    conversation_lines,
    first_line,
)
from tests.unit.test_mail_preview_card import TUESDAY, add_message

HOUR = 3600


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _thread(store, *, key="<root@x>", count=4, prefix="E"):
    people = ["dave@acme.com", "priya@acme.com"]
    return [
        add_message(store, path=f"pst://Archive/{prefix}{n}", subject="Re: School trip" if n else "School trip",
                    sender=people[n % 2], to=[people[(n + 1) % 2]], sent_at=TUESDAY + n * HOUR,
                    body=f"Reply number {n}.\nSecond line.", conversation=key)
        for n in range(count)
    ]


# --- the query ---------------------------------------------------------------------

def test_a_conversation_is_its_messages_oldest_first(store) -> None:
    ids = _thread(store)
    add_message(store, path="pst://Archive/other", subject="Unrelated", sender="x@y.z",
                sent_at=TUESDAY, conversation="<other@x>")

    rows = store.conversation_messages("<root@x>")

    assert [row["file_id"] for row in rows] == ids
    assert rows[0]["path"] == "pst://Archive/E0"
    assert rows[1]["opening"].endswith("Reply number 1.\nSecond line.")


def test_a_message_with_no_conversation_has_none(store) -> None:
    add_message(store, path="pst://Archive/E1", subject="Alone", sender="x@y.z", sent_at=TUESDAY)

    assert store.conversation_messages(None) == []
    assert store.conversation_messages("") == []
    assert store.conversation_messages("<nobody@x>") == []


def test_a_very_long_conversation_returns_its_newest_and_no_more(store) -> None:
    ids = _thread(store, count=9)

    rows = store.conversation_messages("<root@x>", limit=5)

    assert [row["file_id"] for row in rows] == ids[-5:], "the newest five, oldest of them first"


def test_it_is_answered_from_the_conversation_index(store) -> None:
    _thread(store)

    plan = " ".join(str(tuple(row)) for row in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT m.file_id FROM messages m JOIN files f ON f.id = m.file_id "
        "WHERE m.conversation = ? ORDER BY m.sent_at IS NULL, m.sent_at DESC, m.file_id DESC "
        "LIMIT 26", ("<root@x>",)))

    assert "idx_messages_conv" in plan, plan


def test_it_is_one_statement(store) -> None:
    _thread(store)
    seen: list[str] = []
    store.conn.set_trace_callback(seen.append)
    try:
        store.conversation_messages("<root@x>")
    finally:
        store.conn.set_trace_callback(None)

    assert len([s for s in seen if s.lstrip().upper().startswith("SELECT")]) == 1, seen


# --- the words ---------------------------------------------------------------------

def test_the_heading_counts_the_messages() -> None:
    assert conversation_heading(4) == "4 messages in this conversation"
    assert conversation_heading(2) == "2 messages in this conversation"


def test_one_message_is_not_a_conversation() -> None:
    assert conversation_heading(1) == ""
    assert conversation_heading(0) == ""


def test_a_conversation_longer_than_the_list_says_so() -> None:
    heading = conversation_heading(CONVERSATION_SHOWN + 1)

    assert heading == (f"More than {CONVERSATION_SHOWN} messages in this conversation — "
                       f"the newest {CONVERSATION_SHOWN} are listed")


def test_the_first_line_is_the_messages_own_not_the_index_header() -> None:
    stored = "Subject: Trip\nFrom: dave@acme.com\n\n\n  The trip is on Friday.  \nBring a coat."

    assert first_line(stored) == "The trip is on Friday."
    assert first_line("Subject: Trip\nFrom: dave@acme.com") == ""
    assert first_line("") == ""


def test_a_long_first_line_is_cut_at_a_word() -> None:
    line = first_line("word " * 60)

    assert len(line) <= 121 and line.endswith("…") and not line.endswith(" …")


def test_each_line_says_who_when_and_how_it_starts(store) -> None:
    ids = _thread(store)

    lines = conversation_lines(store.conversation_messages("<root@x>"), ids[2])

    assert [line.sender for line in lines] == ["dave@acme.com", "priya@acme.com"] * 2
    assert lines[1].first_line == "Reply number 1."
    assert lines[1].when, "a date, as the Mail list writes it"
    assert [line.current for line in lines] == [False, False, True, False]
    assert lines[3].row.file_id == ids[3] and lines[3].row.path == "pst://Archive/E3"


# --- read on the worker, with the message ---------------------------------------------

def test_a_previewed_message_carries_its_conversation(store) -> None:
    from app.ui.preview_loader import load_preview_for

    ids = _thread(store)

    preview = load_preview_for(SimpleNamespace(file_id=ids[1], path="pst://Archive/E1"),
                               store=store)

    mail = preview.meta["mail"]
    assert mail.conversation_heading == "4 messages in this conversation"
    assert [line.row.file_id for line in mail.conversation] == ids
    assert [line.current for line in mail.conversation] == [False, True, False, False]


def test_a_message_on_its_own_carries_none(store) -> None:
    from app.ui.preview_loader import load_preview_for

    alone = add_message(store, path="pst://Archive/E1", subject="Alone", sender="x@y.z",
                        sent_at=TUESDAY, conversation="<alone@x>")

    mail = load_preview_for(SimpleNamespace(file_id=alone, path="pst://Archive/E1"),
                            store=store).meta["mail"]

    assert mail.conversation == () and mail.conversation_heading == ""


def test_a_conversation_that_cannot_be_read_costs_the_list_not_the_message(store, monkeypatch) -> None:
    from app.ui.preview_loader import load_preview_for

    ids = _thread(store)
    monkeypatch.setattr(SqliteStore, "conversation_messages",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("closed")))

    preview = load_preview_for(SimpleNamespace(file_id=ids[0], path="pst://Archive/E0"),
                               store=store)

    assert preview.meta["mail"].conversation == ()
    assert preview.meta["mail"].body.startswith("Reply number 0.")


# --- the budget: under 20 ms ----------------------------------------------------------

def _synthetic(store, *, conversations: int, each: int) -> int:
    """`conversations` threads of `each` messages, written straight into the
    tables - the shape a real mailbox has, without parsing a single message."""
    files, messages, chunks = [], [], []
    file_id = 0
    for thread in range(conversations):
        key = f"<thread-{thread}@synthetic>"
        for reply in range(each):
            file_id += 1
            sent = 1_600_000_000 + thread * 600 + reply * 60
            files.append((file_id, f"pst://Synthetic/E{file_id}", "pst://Synthetic", "",
                          2_000, sent * 1_000_000_000, "INDEXED", "pst_message"))
            messages.append((file_id, "D:/mail/synthetic.pst", str(file_id), key,
                             f"Re: subject {thread}", f"person{reply % 7}@example.org",
                             json.dumps([f"person{(reply + 1) % 7}@example.org"]), sent, 0))
            chunks.append((file_id, 0,
                           f"Subject: Re: subject {thread}\nFrom: person{reply % 7}@example.org\n\n"
                           + f"Reply {reply} in thread {thread}. " + "More of the message. " * 12))
    with store.write() as conn:
        # The full-text indexes are not what this measures, and filling them is
        # what makes a synthetic mailbox slow to build: measured 2026-09-30 on
        # this (shared) machine, 2,000 `messages` rows take 0.1 s without the
        # header-index trigger and 12-29 s with it once the table holds 6,000.
        # The conversation query reads neither `messages_fts` nor `chunks_fts`,
        # so their triggers are dropped for this throwaway store only.
        for trigger in ("messages_ai", "messages_au", "chunks_ai", "chunks_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.executemany(
            "INSERT INTO files (id, path, parent_dir, ext, size_bytes, mtime_ns, status, source_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", files)
        conn.executemany(
            "INSERT INTO messages (file_id, store_path, entry_id, conversation, subject, sender, "
            "recipients, sent_at, has_attach) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", messages)
        conn.executemany("INSERT INTO chunks (file_id, ordinal, text) VALUES (?, ?, ?)", chunks)
    return file_id


def test_the_conversation_strip_stays_inside_its_budget(tmp_path) -> None:
    """Section 5: under 20 ms. 40,000 messages in 10,000 conversations of four,
    and one of sixty - longer than the list - which is the worst case the
    query meets, since it reads the first passage of every message it lists."""
    with SqliteStore(tmp_path / "big.db") as store:
        total = _synthetic(store, conversations=10_000, each=4)
        with store.write() as conn:
            conn.execute("UPDATE messages SET conversation = '<long@synthetic>' "
                         "WHERE file_id <= 60")
        timings: dict[str, list[float]] = {"four": [], "sixty": []}
        for name, key in (("four", "<thread-7000@synthetic>"), ("sixty", "<long@synthetic>")):
            store.conversation_messages(key, limit=CONVERSATION_SHOWN + 1)      # warm
            for _ in range(7):
                started = time.perf_counter()
                rows = store.conversation_messages(key, limit=CONVERSATION_SHOWN + 1)
                lines = conversation_lines(rows, rows[0]["file_id"])
                timings[name].append(time.perf_counter() - started)
            assert lines
    assert total == 40_000
    print(f"\nconversation strip over {total:,} messages: "
          f"four {statistics.median(timings['four']) * 1000:.2f} ms, "
          f"sixty-capped {statistics.median(timings['sixty']) * 1000:.2f} ms (median of 7, warm)")
    assert statistics.median(timings["four"]) < 0.020, timings
    assert statistics.median(timings["sixty"]) < 0.020, timings


# --- drawn, and clicked -----------------------------------------------------------------

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_a7_a_message_with_three_replies_shows_the_card_and_four_messages(qapp, store) -> None:
    """Acceptance A7."""
    from app.ui.mail_view import MailView
    from tests.unit.test_mail_preview_card import preview_now, pump

    ids = _thread(store)
    view = MailView(store)
    try:
        pump()
        first = next(row for row in view._rows if row.file_id == ids[0])
        preview_now(view.preview, first)

        card = view.preview.mail
        assert not card.isHidden()
        assert card.conversation_heading.text() == "4 messages in this conversation"
        assert card.conversation.count() == 4
        assert "dave@acme.com" in card.conversation.item(0).text()
        assert "Reply number 1." in card.conversation.item(1).text()
        assert card.conversation.item(0).font().bold(), "the message on show is marked"
        assert not card.conversation.item(1).font().bold()
    finally:
        view.shutdown()


def test_clicking_a_message_shows_it_in_the_same_pane(qapp, store) -> None:
    from app.ui.mail_view import MailView
    from tests.unit.test_mail_preview_card import preview_now, pump

    ids = _thread(store)
    view = MailView(store)
    try:
        pump()
        preview_now(view.preview, next(row for row in view._rows if row.file_id == ids[0]))
        pane = view.preview

        pane.mail.conversation.itemClicked.emit(pane.mail.conversation.item(2))
        pane._timer.stop()
        pane._start()
        pump()

        assert pane._row.file_id == ids[2]
        assert pane.text.toPlainText().startswith("Reply number 2.")
        assert pane.mail.conversation.count() == 4, "the list stays, to go back by"
        assert pane.mail.conversation.item(2).font().bold()
        assert not pane.mail.conversation.item(0).font().bold()
    finally:
        view.shutdown()


def test_a_message_on_its_own_shows_no_list(qapp, store) -> None:
    from app.ui.mail_view import MailView
    from tests.unit.test_mail_preview_card import preview_now, pump

    add_message(store, path="pst://Archive/E1", subject="Alone", sender="x@y.z",
                sent_at=TUESDAY, body="Just me.", conversation="<alone@x>")
    view = MailView(store)
    try:
        pump()
        preview_now(view.preview, view._rows[0])

        assert view.preview.mail.conversation_box.isHidden()
    finally:
        view.shutdown()


def test_the_list_is_named_for_a_screen_reader(qapp) -> None:
    from app.ui.widgets.mail_card import MailCardView

    assert MailCardView().conversation.accessibleName() == "Messages in this conversation"
