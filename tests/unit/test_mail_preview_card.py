r"""Order 0y section 4a: the mail preview's header card.

Layer: L5

> **4a** A header card, drawn rather than typed: the sender's name large with
> the address beside it, To and Cc, the date in words (*Tuesday 2 January 2024,
> 09:00*), the subject as a heading, and attachments as chips. The plain
> `From: ...` block remains what Copy produces.

Three layers, tested where each can be:

* the words on the card are decided without Qt (`presenter/mail.py`);
* the message is read on a worker (`preview_loader.mail_preview`), from a real
  store;
* the card is drawn (`widgets/mail_card.py`) and the pane shows it in place of
  its title line - checked offscreen, and grabbed to a PNG to be looked at.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.storage.sqlite_store import FileStatus, SqliteStore
from app.ui.presenter.mail import (
    MailCard,
    card_from_row,
    mail_card,
    sent_in_words,
    split_index_headers,
)

#: 09:00 on Tuesday 2 January 2024, in this machine's own time zone - the card
#: shows local time, as the Mail list's Date column does.
TUESDAY = int(datetime(2024, 1, 2, 9, 0).timestamp())


def add_message(store, *, path, subject="", sender="", to=(), sent_at=0, body="",
                attachments=(), conversation=None, entry_id=None, store_path=None,
                source_kind="pst_message", quoted_removed=None):
    """One message, written the way the indexer writes one: the `messages` row,
    and the stored text with the index's own header lines above the body."""
    file_id = store.upsert_file(
        path, size_bytes=len(body) + 100, mtime_ns=(sent_at or 1) * 1_000_000_000,
        status=FileStatus.INDEXED, source_kind=source_kind)
    store.set_message(
        file_id, subject=subject, sender=sender, recipients=json.dumps(list(to)),
        sent_at=sent_at or None, has_attach=1 if attachments else 0,
        conversation=conversation, entry_id=entry_id, store_path=store_path,
        quoted_removed=quoted_removed)
    lines = [f"Subject: {subject}" if subject else "", f"From: {sender}" if sender else ""]
    if to:
        lines.append(f"To: {', '.join(to)}")
    if attachments:
        lines.append(f"Attachments: {', '.join(attachments)}")
    header = "\n".join(line for line in lines if line)
    text = f"{header}\n\n{body}" if header and body else (header or body)
    store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
    return file_id


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


# --- the words, without Qt ---------------------------------------------------------

def test_the_date_is_said_in_words() -> None:
    assert sent_in_words(TUESDAY) == "Tuesday 2 January 2024, 09:00"


def test_a_message_with_no_date_says_nothing_rather_than_1970() -> None:
    assert sent_in_words(None) == ""
    assert sent_in_words(0) == ""
    assert sent_in_words("not a date") == ""


def test_the_day_is_worded_by_the_timeline_helper_not_a_second_formatter() -> None:
    """One way of saying a day in the application: `timeline_words.day_heading`."""
    from app.reports.timeline_words import day_heading

    assert sent_in_words(TUESDAY).startswith(day_heading(datetime.fromtimestamp(TUESDAY)))


def test_a_name_and_an_address_are_shown_apart() -> None:
    card = mail_card({"sender": "Dave Smith <dave@acme.com>", "subject": "Trip"})

    assert (card.sender_name, card.sender_address) == ("Dave Smith", "dave@acme.com")


def test_a_bare_address_is_the_name_and_is_not_said_twice() -> None:
    """The index usually holds only the address. Nothing is invented from it."""
    card = mail_card({"sender": "dave@acme.com"})

    assert (card.sender_name, card.sender_address) == ("dave@acme.com", "")


def test_every_recipient_is_on_the_card_not_the_first_two() -> None:
    """The list column shortens to "a, b +3"; the card is where the rest are read."""
    people = ["priya@acme.com", "bob@x.org", "carol@x.org", "dan@x.org"]

    card = mail_card({"recipients": json.dumps(people)})

    assert card.recipients == tuple(people)


def test_recipients_that_are_not_a_list_cost_nothing_but_themselves() -> None:
    assert mail_card({"recipients": "priya@acme.com"}).recipients == ("priya@acme.com",)
    assert mail_card({"recipients": "[broken"}).recipients == ("[broken",)
    assert mail_card({"recipients": None}).recipients == ()


def test_attachments_are_named_from_the_text_the_index_holds() -> None:
    stored = "Subject: Trip\nFrom: dave@acme.com\nAttachments: form.pdf, map.png\n\nSee attached."

    card = mail_card({"subject": "Trip", "has_attach": 1}, stored)

    assert card.attachments == ("form.pdf", "map.png")


def test_an_attachment_nobody_named_is_still_a_chip() -> None:
    assert mail_card({"has_attach": 1}).attachments == ("Attachment",)
    assert mail_card({"has_attach": 0}).attachments == ()


def test_an_empty_subject_says_so() -> None:
    assert mail_card({"subject": "  "}).subject == "(no subject)"


def test_the_index_header_lines_are_split_from_the_message() -> None:
    stored = "Subject: Trip\nFrom: dave@acme.com\nTo: priya@acme.com\n\nThe trip is on Friday."

    headers, body = split_index_headers(stored)

    assert headers == {"Subject": "Trip", "From": "dave@acme.com", "To": "priya@acme.com"}
    assert body == "The trip is on Friday."


def test_only_the_leading_block_is_taken() -> None:
    """A body that quotes a header further down keeps it."""
    stored = "Subject: Trip\n\nHe wrote:\nFrom: someone else\nSubject: another"

    headers, body = split_index_headers(stored)

    assert headers == {"Subject": "Trip"}
    assert body == "He wrote:\nFrom: someone else\nSubject: another"


def test_text_with_no_header_block_is_left_alone() -> None:
    assert split_index_headers("Just a note.\n\nFrom: me") == ({}, "Just a note.\n\nFrom: me")
    assert split_index_headers("") == ({}, "")


def test_a_mail_row_fills_the_card_at_once() -> None:
    """The Mail list's own row has enough to draw the card before the read lands."""
    row = SimpleNamespace(file_id=7, sender="Dave Smith", recipients="Priya, Bob +2",
                          sent_at=TUESDAY, subject="Trip", has_attachment=True)

    card = card_from_row(row)

    assert isinstance(card, MailCard)
    assert card.sender_name == "Dave Smith"
    assert card.date_words == "2024-01-02 09:00"   # 2026-10-11: the chosen format
    assert card.subject == "Trip"


def test_a_row_that_is_not_mail_fills_nothing() -> None:
    assert card_from_row(SimpleNamespace(file_id=7, name="report.pdf", path="D:/x")) is None
    assert card_from_row(None) is None


# --- the read, on a worker, from a real store --------------------------------------

def test_a_message_is_previewed_as_a_card_and_its_own_words(store) -> None:
    from app.ui.preview_loader import load_preview_for

    file_id = add_message(
        store, path="pst://Archive/E1", subject="School trip",
        sender="Dave Smith <dave@acme.com>", to=["priya@acme.com", "bob@x.org"],
        sent_at=TUESDAY, body="The trip is on Friday.\n\nBring a coat.",
        attachments=["form.pdf"], quoted_removed=480)
    row = SimpleNamespace(file_id=file_id, path="pst://Archive/E1", name="School trip")

    preview = load_preview_for(row, store=store)

    mail = preview.meta["mail"]
    assert mail.card.sender_name == "Dave Smith"
    assert mail.card.recipients == ("priya@acme.com", "bob@x.org")
    assert mail.card.date_words == "2024-01-02 09:00"   # 2026-10-11: the chosen format
    assert mail.card.attachments == ("form.pdf",)
    assert mail.body == "The trip is on Friday.\n\nBring a coat."
    assert preview.title == "School trip"
    assert "480" in preview.notice, "the quoted-text notice stays"


def test_copy_still_produces_the_plain_from_block(store) -> None:
    """The card is drawn; what is copied is text, as it was before the card."""
    from app.ui.preview_loader import load_preview_for

    file_id = add_message(store, path="pst://Archive/E1", subject="School trip",
                          sender="dave@acme.com", to=["priya@acme.com"], sent_at=TUESDAY,
                          body="The trip is on Friday.")

    preview = load_preview_for(SimpleNamespace(file_id=file_id, path="pst://Archive/E1"),
                               store=store)

    assert preview.body.startswith("From: dave@acme.com\nTo: priya@acme.com\n")
    assert "Subject: School trip" in preview.body
    assert preview.body.endswith("The trip is on Friday.")
    assert preview.meta["mail"].copy_header + preview.meta["mail"].body == preview.body


def test_a_file_that_is_not_a_message_previews_exactly_as_before(store, tmp_path) -> None:
    from app.ui.preview_loader import load_preview_for

    note = tmp_path / "note.txt"
    note.write_text("plain", encoding="utf-8")
    file_id = store.upsert_file(str(note), size_bytes=5, mtime_ns=1, status=FileStatus.INDEXED)

    preview = load_preview_for(SimpleNamespace(file_id=file_id, path=str(note)), store=store)

    assert "mail" not in preview.meta
    assert preview.body == "plain"


def test_a_store_that_cannot_answer_costs_the_card_not_the_preview(tmp_path) -> None:
    from app.ui.preview_loader import load_preview_for

    class Broken:
        def get_message(self, _file_id):
            raise RuntimeError("closed")

    note = tmp_path / "note.txt"
    note.write_text("plain", encoding="utf-8")

    preview = load_preview_for(SimpleNamespace(file_id=3, path=str(note)), store=Broken())

    assert preview.body == "plain"


# --- drawn --------------------------------------------------------------------------

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


CARD = MailCard(
    sender_name="Dave Smith", sender_address="dave@acme.com",
    recipients=("priya@acme.com", "bob@x.org", "carol@x.org"),
    date_words="Tuesday 2 January 2024, 09:00", subject="School trip",
    attachments=("form.pdf", "map.png"))


def test_the_card_draws_each_part(qapp, tmp_path) -> None:
    from app.ui.theme import stylesheet
    from app.ui.widgets.mail_card import MailCardView

    view = MailCardView()
    view.setStyleSheet(stylesheet("light"))
    view.resize(520, 200)
    view.show_card(CARD)
    view.show()
    qapp.processEvents()

    assert view.sender.text() == "Dave Smith"
    assert view.address.text() == "dave@acme.com"
    assert "priya@acme.com" in view.recipients.text() and "carol@x.org" in view.recipients.text()
    assert view.date.text() == "Tuesday 2 January 2024, 09:00"
    assert view.subject.text() == "School trip"
    assert view.chip_texts() == ["form.pdf", "map.png"]
    assert view.sender.font().pointSizeF() >= view.date.font().pointSizeF()

    target = os.environ.get("LEASHA_GRAB_DIR") or str(tmp_path)
    assert view.grab().save(os.path.join(target, "mail_card.png"))
    view.close()


def test_a_card_with_less_to_say_draws_less(qapp) -> None:
    from app.ui.widgets.mail_card import MailCardView

    view = MailCardView()
    view.show_card(CARD)
    view.show_card(MailCard(sender_name="dave@acme.com", subject="(no subject)"))

    assert view.address.isHidden()
    assert view.recipients_row.isHidden()
    assert view.date.isHidden()
    assert view.chips.isHidden() and view.chip_texts() == []
    view.close()


def _mail_preview(body="The trip is on Friday."):
    from app.ui.preview_loader import KIND_TEXT, MailPreview, Preview

    header = "From: dave@acme.com\nSubject: School trip\n\n" + "-" * 40 + "\n\n"
    mail = MailPreview(file_id=7, card=CARD, body=body, copy_header=header)
    return Preview(kind=KIND_TEXT, body=header + body, path="pst://Archive/E1",
                   title="School trip", meta={"mail": mail})


def test_the_pane_shows_the_card_in_place_of_its_title(qapp) -> None:
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    pane.show()
    pane.show_row(SimpleNamespace(file_id=7, path="pst://Archive/E1", name="School trip"))
    pane._timer.stop()

    pane._rendered(_mail_preview(), pane._generation)

    assert not pane.mail.isHidden()
    assert pane.title.isHidden() and pane.subtitle.isHidden()
    assert pane.mail.subject.text() == "School trip"
    assert pane.text.toPlainText() == "The trip is on Friday.", "the block is not typed twice"
    pane.close()


def test_select_all_and_copy_gives_the_block_and_the_message(qapp) -> None:
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    pane.show_row(SimpleNamespace(file_id=7, path="pst://Archive/E1", name="School trip"))
    pane._timer.stop()
    pane._rendered(_mail_preview(), pane._generation)

    pane.text.selectAll()
    copied = pane.text.createMimeDataFromSelection().text()

    assert copied.startswith("From: dave@acme.com\nSubject: School trip")
    assert copied.endswith("The trip is on Friday.")
    pane.close()


def test_copying_part_of_the_message_copies_only_that_part(qapp) -> None:
    from PySide6.QtGui import QTextCursor

    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    pane.show_row(SimpleNamespace(file_id=7, path="pst://Archive/E1", name="School trip"))
    pane._timer.stop()
    pane._rendered(_mail_preview(), pane._generation)

    cursor = pane.text.textCursor()
    cursor.setPosition(4)
    cursor.setPosition(8, QTextCursor.MoveMode.KeepAnchor)
    pane.text.setTextCursor(cursor)

    assert pane.text.createMimeDataFromSelection().text() == "trip"
    pane.close()


def test_a_file_after_a_message_gets_its_title_back(qapp) -> None:
    from app.ui.preview_loader import KIND_TEXT, Preview
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    pane.show()
    pane.show_row(SimpleNamespace(file_id=7, path="pst://Archive/E1", name="School trip"))
    pane._timer.stop()
    pane._rendered(_mail_preview(), pane._generation)

    pane.show_row(SimpleNamespace(path="D:/x/note.txt", name="note.txt"))
    pane._timer.stop()
    pane._rendered(Preview(kind=KIND_TEXT, body="plain", path="D:/x/note.txt"),
                   pane._generation)

    assert pane.mail.isHidden()
    assert not pane.title.isHidden()
    pane.text.selectAll()
    assert pane.text.createMimeDataFromSelection().text() == "plain"
    pane.close()


def test_a_mail_row_shows_its_card_before_the_read_lands(qapp) -> None:
    """Arrowing down the Mail list must not flick between a title and a card."""
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    pane.show()
    pane.show_row(SimpleNamespace(file_id=7, path="pst://Archive/E1", name="Trip",
                                  sender="Dave Smith", recipients="Priya", sent_at=TUESDAY,
                                  subject="Trip", has_attachment=False))
    pane._timer.stop()

    assert not pane.mail.isHidden()
    assert pane.mail.sender.text() == "Dave Smith"
    assert pane.title.isHidden()
    pane.close()


# --- the Mail tab, end to end --------------------------------------------------------

def pump(ms: int = 5_000) -> None:
    """Let every queued worker finish and its result be delivered."""
    from PySide6.QtCore import QThreadPool

    app = QApplication.instance()
    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(10):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(ms)


# 2026-09-30: `release(view)`, which deleted a Mail view by hand, has gone from
# here and from the mail test files that borrowed it. A view that is let go is
# freed at once now - the cause is fixed and pinned in `test_views_are_freed.py`.


def preview_now(pane, row) -> None:
    """Select `row` in `pane` and wait for the read, without the debounce."""
    pane.setVisible(True)
    pane.show_row(row)
    pane._timer.stop()
    pane._start()
    pump()


def test_a_message_selected_in_the_mail_tab_shows_its_card(qapp, store) -> None:
    from app.ui.mail_view import MailView

    add_message(store, path="pst://Archive/E1", subject="School trip",
                sender="Dave Smith <dave@acme.com>", to=["priya@acme.com"],
                sent_at=TUESDAY, body="The trip is on Friday.", attachments=["form.pdf"])
    view = MailView(store)
    try:
        pump()
        preview_now(view.preview, view._rows[0])

        assert not view.preview.mail.isHidden()
        assert view.preview.mail.address.text() == "dave@acme.com"
        assert view.preview.mail.date.text() == "2024-01-02 09:00"   # 2026-10-11: the chosen format
        assert view.preview.mail.chip_texts() == ["form.pdf"]
        assert view.preview.text.toPlainText() == "The trip is on Friday."
    finally:
        view.shutdown()
