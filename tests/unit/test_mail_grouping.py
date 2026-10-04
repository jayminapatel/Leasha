r"""Order 0z, lane F, item F2: mail grouped by conversation.

Layer: L5

> **F2** Mail results grouped by conversation, with the parent message shown
> when only an attachment matched (overlaps order 0y section 4c).

Built as a view choice - "One row per conversation" in the View menu, off until
asked for - in both lists that show mail:

* **Mail**: each conversation's listed messages fold into the newest of them,
  with a Messages column saying how many. The summary line still counts
  messages, so `Showing 500 of 12,431 messages` stays true.
* **Search**: passages from every message of a conversation, and from their
  attachments, fold into one row. When the best passage is in an attachment the
  row is the *message* it is attached to, with the attachment named beside it.

And wherever an attachment is previewed, the pane shows the message it is
attached to above the attachment's own text.

Nothing is hidden silently: a folded row says how many it stands for.
"""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.storage.sqlite_store import FileStatus, SqliteStore
from app.ui.presenter import ResultRow, group_results, mail_rows, mail_summary
from app.ui.presenter.mail import fold_conversations, mail_list
from app.ui.presenter.snippets import Snippet
from app.ui.view_options import ViewPreferences, parse_prefs, prefs_to_state
from tests.unit.test_mail_preview_card import TUESDAY, add_message

HOUR = 3600


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _store_rows():
    """Seven messages, newest first, as `browse_messages` returns them: a
    conversation of three, one of two, and two with none recorded."""
    def row(file_id, subject, conversation, age):
        return {"file_id": file_id, "subject": subject, "sender": "dave@acme.com",
                "recipients": "[]", "sent_at": TUESDAY - age * HOUR, "has_attach": 0,
                "conversation": conversation, "path": f"pst://A/E{file_id}", "size_bytes": 10,
                "status": "INDEXED", "skip_code": None}
    return [
        row(7, "Re: trip", "<trip@x>", 0),
        row(6, "Lunch", None, 1),
        row(5, "Re: invoice", "<invoice@x>", 2),
        row(4, "Re: trip", "<trip@x>", 3),
        row(3, "Parking", "", 4),
        row(2, "Invoice", "<invoice@x>", 5),
        row(1, "Trip", "<trip@x>", 6),
    ]


# --- the Mail list ------------------------------------------------------------------

def test_a_mail_row_knows_its_conversation() -> None:
    rows = mail_rows(_store_rows())

    assert rows[0].conversation == "<trip@x>"
    assert rows[1].conversation == "" and rows[4].conversation == ""
    assert rows[0].thread == "" and rows[0].thread_count == 0, "nothing folded, nothing claimed"


def test_each_conversation_folds_into_its_newest_listed_message() -> None:
    folded = fold_conversations(mail_rows(_store_rows()))

    assert [row.file_id for row in folded] == [7, 6, 5, 3]
    assert [row.thread for row in folded] == ["3", "1", "2", "1"]
    assert [row.thread_count for row in folded] == [3, 1, 2, 1]


def test_folding_loses_no_message_from_the_count() -> None:
    rows = mail_rows(_store_rows())

    assert sum(row.thread_count for row in fold_conversations(rows)) == len(rows)


def test_messages_with_no_conversation_are_never_folded_together() -> None:
    """`""` and `NULL` mean "not known", not one conversation they all share."""
    rows = mail_rows([{**row, "conversation": None} for row in _store_rows()])

    assert len(fold_conversations(rows)) == 7


def test_the_list_is_unchanged_until_folding_is_asked_for() -> None:
    rows, note = mail_list(_store_rows())

    assert [row.file_id for row in rows] == [7, 6, 5, 4, 3, 2, 1] and note == ""


def test_the_count_line_still_tells_the_truth_when_folded() -> None:
    """The brief: grouping must not hide results silently, and the existing
    "Showing 500 of 12,431 messages" line must stay correct."""
    rows, note = mail_list(_store_rows(), fold=True)

    assert len(rows) == 4
    assert note + mail_summary(7) == "Folded into 4 conversations  ·  7 messages"
    assert (note + mail_summary(7, page_size=7, total=12_431)).startswith(
        "Folded into 4 conversations  ·  Showing 7 of 12,431 messages — narrow it with")


def test_one_conversation_is_singular_and_nothing_is_nothing() -> None:
    assert mail_list(_store_rows()[:1], fold=True)[1] == "Folded into 1 conversation  ·  "
    assert mail_list([], fold=True) == ([], "")


# --- the view choice -----------------------------------------------------------------

def test_folding_is_off_until_asked_for_and_is_remembered() -> None:
    assert ViewPreferences().group_by_conversation is False
    assert parse_prefs({}, "ui:mail").group_by_conversation is False

    chosen = replace(ViewPreferences(), group_by_conversation=True)

    assert prefs_to_state(chosen, "ui:mail")["ui:mail:conversations"] == "on"
    assert parse_prefs(prefs_to_state(chosen, "ui:mail"), "ui:mail") == chosen


def test_changing_another_preference_does_not_forget_it() -> None:
    chosen = replace(ViewPreferences(), group_by_conversation=True)

    assert replace(chosen, density="compact").group_by_conversation is True


# --- the Search list ------------------------------------------------------------------

def _hit(rank, file_id, path, chunk_id=None):
    return ResultRow(rank=rank, chunk_id=chunk_id or rank * 10, file_id=file_id, path=path,
                     display_path=path, snippet=Snippet("the trip"), explain="", location="",
                     score=1.0 / rank)


DETAILS = {
    1: {"subject": "Trip", "sender": "Dave Smith <dave@acme.com>", "sent_at": TUESDAY,
        "has_attach": 1, "conversation": "<trip@x>"},
    4: {"subject": "Re: trip", "sender": "priya@acme.com", "sent_at": TUESDAY + HOUR,
        "has_attach": 0, "conversation": "<trip@x>"},
    # An attachment: its detail is its parent message's, marked (`tasks.mail_details`).
    9: {"subject": "Trip", "sender": "Dave Smith <dave@acme.com>", "sent_at": TUESDAY,
        "has_attach": 1, "conversation": "<trip@x>", "attachment_of": 1},
    5: {"subject": "Invoice", "sender": "bob@x.org", "sent_at": TUESDAY,
        "has_attach": 0, "conversation": "<invoice@x>"},
}


def test_search_is_unchanged_until_folding_is_asked_for() -> None:
    hits = [_hit(1, 4, "pst://A/E4"), _hit(2, 1, "pst://A/E1"), _hit(3, 5, "pst://A/E5")]

    groups = group_results(hits, details=DETAILS)

    assert [group.file_id for group in groups] == [4, 1, 5]


def test_a_conversations_messages_fold_into_one_search_row() -> None:
    hits = [_hit(1, 4, "pst://A/E4"), _hit(2, 5, "pst://A/E5"), _hit(3, 1, "pst://A/E1"),
            _hit(4, 20, "D:/docs/trip.txt")]

    groups = group_results(hits, details=DETAILS, conversations=True)

    assert [group.file_id for group in groups] == [4, 5, 20], "placed by its best passage"
    assert groups[0].match_count == 2
    assert [row.file_id for row in groups[0].rows] == [4, 1], "every passage is still there"
    assert "2 messages of this conversation matched" in groups[0].folder
    assert "messages" not in groups[1].folder, "a conversation of one says nothing extra"
    assert groups[2].name == "trip.txt", "a file is not mail and is left alone"


def test_the_parent_message_is_shown_when_only_an_attachment_matched() -> None:
    hits = [_hit(1, 9, "pst://A/E1/attachments/form.pdf")]

    group = group_results(hits, details=DETAILS, conversations=True)[0]

    assert group.name == "Dave Smith — Trip"
    assert group.kind == "email"
    assert group.folder == "in the attachment form.pdf"
    assert group.rows[0].path == "pst://A/E1/attachments/form.pdf", "opening it is unchanged"


def test_an_attachment_and_its_own_message_count_as_one_message() -> None:
    hits = [_hit(1, 9, "pst://A/E1/attachments/form.pdf"), _hit(2, 1, "pst://A/E1"),
            _hit(3, 4, "pst://A/E4")]

    group = group_results(hits, details=DETAILS, conversations=True)[0]

    assert group.match_count == 3
    assert "2 messages of this conversation matched" in group.folder


def test_without_folding_an_attachment_row_is_as_it_was() -> None:
    """Results-presentation 5b, untouched: the attachment is the object, its
    message the context."""
    group = group_results([_hit(1, 9, "pst://A/E1/attachments/form.pdf")], details=DETAILS)[0]

    assert group.name == "form.pdf"
    assert group.folder == "from Dave Smith · Trip"


def test_the_page_lookup_carries_the_conversation(store) -> None:
    file_id = add_message(store, path="pst://A/E1", subject="Trip", sender="dave@acme.com",
                          sent_at=TUESDAY, conversation="<trip@x>")

    assert store.messages_for([file_id])[file_id]["conversation"] == "<trip@x>"


# --- previewing an attachment -------------------------------------------------------

def _attachment(store, parent_path="pst://A/E1"):
    file_id = store.upsert_file(f"{parent_path}/attachments/form.pdf", size_bytes=10,
                                mtime_ns=1, status=FileStatus.INDEXED, source_kind="pst_message")
    store.replace_chunks(file_id, [{"ordinal": 0, "text": "Consent for the trip. Sign below."}])
    return file_id


def test_an_attachment_previews_under_the_message_it_is_attached_to(store) -> None:
    from app.ui.preview_loader import load_preview_for

    parent = add_message(store, path="pst://A/E1", subject="School trip",
                         sender="Dave Smith <dave@acme.com>", sent_at=TUESDAY,
                         body="Form attached.", attachments=["form.pdf"],
                         entry_id="00AB", store_path="D:/mail/A.pst", conversation="<trip@x>")
    add_message(store, path="pst://A/E2", subject="Re: School trip", sender="priya@acme.com",
                sent_at=TUESDAY + HOUR, body="Signed.", conversation="<trip@x>")
    attached = _attachment(store)

    preview = load_preview_for(
        SimpleNamespace(file_id=attached, path="pst://A/E1/attachments/form.pdf",
                        name="form.pdf"), store=store)

    mail = preview.meta["mail"]
    assert preview.error is None
    assert mail.card.subject == "School trip" and mail.card.sender_name == "Dave Smith"
    assert mail.attachment == "form.pdf"
    assert mail.body == "Consent for the trip. Sign below.", "the attachment's own text"
    assert "form.pdf" in preview.notice and "attach" in preview.notice
    assert mail.conversation_heading == "2 messages in this conversation"
    assert [line.current for line in mail.conversation] == [True, False]
    assert mail.conversation[0].row.file_id == parent
    assert mail.original.kind == "outlook" and mail.original.entry_id == "00AB"


def test_the_attachment_path_rule_is_the_one_the_search_list_reads() -> None:
    # 2026-10-04, code review: the aliases `tasks._ATTACHMENT_MARKER` and
    # `tasks._attachment_parent_path` were removed; the real names are read.
    from app.core import row_facts
    from app.search.marks import attachment_parent_path
    from app.ui.presenter.mail import ATTACHMENT_MARKER, attachment_of

    assert ATTACHMENT_MARKER == row_facts.ATTACHMENT_MARKER
    for path in ("pst://A/E1/attachments/form.pdf", "pst://A/E1", "/attachments/x",
                 "D:\\docs\\attachments\\form.pdf", ""):
        assert attachment_of(path)[0] == attachment_parent_path(path), path
    assert attachment_of("pst://A/E1/attachments/form.pdf") == ("pst://A/E1", "form.pdf")


def test_an_attachment_whose_message_is_gone_previews_as_before(store) -> None:
    from app.ui.preview_loader import load_preview_for

    attached = _attachment(store, parent_path="pst://A/E404")

    preview = load_preview_for(
        SimpleNamespace(file_id=attached, path="pst://A/E404/attachments/form.pdf"),
        store=store)

    assert "mail" not in preview.meta


# --- the Mail tab, folded -------------------------------------------------------------

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _mailbox(store) -> None:
    for n in range(3):
        add_message(store, path=f"pst://A/T{n}", subject="Trip", sender="dave@acme.com",
                    sent_at=TUESDAY + n * HOUR, body=f"Trip {n}.", conversation="<trip@x>")
    add_message(store, path="pst://A/L", subject="Lunch", sender="bob@x.org",
                sent_at=TUESDAY + 10 * HOUR, body="Lunch?")


def _column(view, key: str) -> int:
    from app.ui.mail_view import COLUMNS

    return [k for k, *_ in COLUMNS].index(key)


def test_the_mail_tab_folds_when_the_view_menu_says_so(qapp, store) -> None:
    from app.ui.mail_view import MailView
    from tests.unit.test_mail_preview_card import pump

    _mailbox(store)
    view = MailView(store)
    try:
        pump()
        assert view.results.rowCount() == 4
        assert view.summary.text().startswith("4 messages")
        assert view.results.isColumnHidden(_column(view, "messages")), "no empty column"

        view.view_button.prefs = replace(view.view_button.prefs, group_by_conversation=True)
        view._prefs_changed(view.view_button.prefs)
        pump()

        assert view.results.rowCount() == 2
        assert view.summary.text().startswith("Folded into 2 conversations  ·  4 messages")
        column = _column(view, "messages")
        assert not view.results.isColumnHidden(column)
        counts = {view.results.item(r, _column(view, "subject")).text():
                  view.results.item(r, column).text() for r in range(2)}
        assert counts == {"Trip": "3", "Lunch": "1"}

        view.view_button.prefs = replace(view.view_button.prefs, group_by_conversation=False)
        view._prefs_changed(view.view_button.prefs)
        pump()

        assert view.results.rowCount() == 4
        assert view.summary.text().startswith("4 messages")
    finally:
        view.shutdown()


def test_a_capped_folded_list_still_says_how_many_messages(qapp, store, monkeypatch) -> None:
    from app.ui import mail_view
    from tests.unit.test_mail_preview_card import pump

    _mailbox(store)
    monkeypatch.setattr(mail_view, "PAGE_SIZE", 3)
    view = mail_view.MailView(store)
    try:
        view.view_button.prefs = replace(view.view_button.prefs, group_by_conversation=True)
        view._run()
        pump()

        assert view.summary.text().startswith(
            "Folded into 2 conversations  ·  Showing 3 of 4 messages — narrow it with")
    finally:
        view.shutdown()


def test_the_view_menu_offers_it_on_mail_and_on_search(qapp) -> None:
    from PyQt6.QtWidgets import QWidget

    from app.ui.view_options import build_menu

    parent = QWidget()
    seen: list = []

    def labels(**kwargs):
        menu = build_menu(parent, ViewPreferences(), columns=[("from", "From")],
                          available=["from"], on_change=seen.append, **kwargs)
        return menu, [action.text() for action in menu.actions()]

    mail_menu, mail = labels(conversations=True)
    _search_menu, search = labels(grouping=True)
    _files_menu, files = labels()

    assert "One row per conversation" in mail
    assert "One row per conversation" in search and "One row per document" in search
    assert "One row per conversation" not in files, "a list of files has none to fold"

    action = next(a for a in mail_menu.actions() if a.text() == "One row per conversation")
    assert not action.isChecked()
    action.trigger()
    assert seen[-1].group_by_conversation is True


def test_folding_brings_the_count_column_back_for_somebody_who_chose_their_columns(qapp) -> None:
    from PyQt6.QtWidgets import QWidget

    from app.ui.view_options import build_menu

    columns = [("from", "From"), ("subject", "Subject"), ("messages", "Messages")]
    seen: list = []
    parent = QWidget()                     # held: the menu dies with its parent
    menu = build_menu(parent, ViewPreferences(columns=("from", "subject")), columns=columns,
                      available=["from", "subject", "messages"], on_change=seen.append,
                      conversations=True)

    next(a for a in menu.actions() if a.text() == "One row per conversation").trigger()

    assert seen[-1].columns == ("from", "subject", "messages")


def test_the_pane_open_button_acts_on_the_message_on_show(qapp, store) -> None:
    """Found building 4c: after a click in the conversation list the message on
    show is not the row selected in the table, and Open acted on the table's."""
    from app.ui.mail_view import MailView
    from tests.unit.test_mail_preview_card import pump

    _mailbox(store)
    view = MailView(store)
    searched: list[str] = []
    view.search_inside_requested.connect(searched.append)
    try:
        pump()
        view.results.selectRow(0)
        shown = next(row for row in view._rows if row.path == "pst://A/T0")

        view.preview.open_requested.emit(shown)
        pump()          # 2026-10-04: by the one open route, on a worker

        assert searched == ["pst://A/T0"], "no Outlook identifier: searched inside"
    finally:
        view.shutdown()
