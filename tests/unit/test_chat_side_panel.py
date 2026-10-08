r"""The Chat tab's side panel, mail named by its subject, archives left out. 2026-10-08.

Layer: L5 (the presenter rules and the engine without a display; the widgets with one)

The owner, looking at the Chat tab: "the preview window goes under local sources
that should i think should be its own vertical tab and the source tab viewing
should be able to turn off and on too ... the second attachment looks cluttered".
Chosen with him:

1. two side tabs, Sources and Preview, on a strip at the right edge - click one
   to show it, click the one showing to put the panel away; picking a source
   shows Preview; remembered across restarts;
2. a message is titled by its subject (and sender, and date), never its entry id,
   and a mail archive file itself (`D:\OutlookArchive\2021.pst`) is not a source;
3. the "<id> Keep Remove" chips fold into one "N sources" control.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.chat.context import build_sources, is_mail_archive, source_title
from app.chat.types import ChatTurn, Receipt, SourcesEvent
from app.ui.presenter import chat as presenter
from app.ui.presenter.chat import (
    PREVIEW_TAB, SOURCES_TAB, PanelState, panel_after_click, panel_state_from_text,
    panel_state_text, shelf_count_label,
)

MESSAGE_ID = 2109476
MESSAGE_PATH = f"pst://2024/{MESSAGE_ID}"
SUBJECT = "Re: Deposit return - 14 Elm Road"
SENDER = "Priya Shah <priya@lettings.example>"
META = {MESSAGE_ID: {"file_id": MESSAGE_ID, "subject": SUBJECT, "sender": SENDER,
                     "recipients": "me", "sent_at": 1709290800, "has_attach": 0,
                     "conversation": "c1"}}
ARCHIVE_PATH = r"D:\OutlookArchive\2021.pst"


# ---------------------------------------------------------------------------
# The rules, without a display
# ---------------------------------------------------------------------------

def test_clicking_the_tab_that_shows_puts_the_panel_away_and_any_other_shows_it():
    shown = PanelState(True, SOURCES_TAB, 400)
    assert panel_after_click(shown, SOURCES_TAB) == PanelState(False, SOURCES_TAB, 400)
    assert panel_after_click(shown, PREVIEW_TAB) == PanelState(True, PREVIEW_TAB, 400)
    away = PanelState(False, PREVIEW_TAB, 400)
    assert panel_after_click(away, PREVIEW_TAB) == PanelState(True, PREVIEW_TAB, 400)
    assert panel_after_click(away, SOURCES_TAB) == PanelState(True, SOURCES_TAB, 400)


def test_the_panel_state_is_written_and_read_back_and_rubbish_is_the_default():
    state = PanelState(False, PREVIEW_TAB, 512)
    assert panel_state_from_text(panel_state_text(state)) == state
    for rubbish in ("", None, "open", "open:nowhere:x", "a:b:c:d"):
        read = panel_state_from_text(rubbish)
        assert read.tab in (SOURCES_TAB, PREVIEW_TAB) and read.width >= 220
    assert panel_state_from_text("open:preview:5").width == 220, "never too narrow to use"


def test_the_folded_shelf_says_how_many_and_how_many_are_kept():
    assert shelf_count_label(1) == "1 source"
    assert shelf_count_label(4) == "4 sources"
    assert shelf_count_label(4, 1) == "4 sources, 1 kept"


def test_a_message_source_is_titled_by_its_subject_never_its_entry_id():
    assert source_title(MESSAGE_PATH, META[MESSAGE_ID]) == SUBJECT
    assert source_title(MESSAGE_PATH, {"subject": ""}) == "(no subject)"
    assert source_title("C:/docs/lease.pdf") == "lease.pdf"
    hit = SimpleNamespace(chunk_id=1, file_id=MESSAGE_ID, path=MESSAGE_PATH,
                          text="We will return the deposit.", score=1.0, rank=1,
                          mtime_ns=0, taken_at_ns=0)
    [source] = build_sources([hit], ["deposit"], max_sources=4, window_tokens=4096,
                             metas=META)
    assert source.name == SUBJECT and str(MESSAGE_ID) not in source.name


def test_an_archive_file_is_not_a_source_but_its_messages_are():
    assert is_mail_archive(SimpleNamespace(path=ARCHIVE_PATH, ext="pst"))
    assert is_mail_archive(SimpleNamespace(path="E:/old/backup.OST", ext=""))
    assert is_mail_archive(SimpleNamespace(path="C:/mail/Inbox.mbox", ext="mbox"))
    assert not is_mail_archive(SimpleNamespace(path=MESSAGE_PATH, ext=""))
    assert not is_mail_archive(SimpleNamespace(path=r"C:\mail\Inbox.mbox/123", ext=""))
    assert not is_mail_archive(SimpleNamespace(path="C:/docs/lease.pdf", ext="pdf"))


class _Search:
    """Search that finds the archive file itself, a message in it, and a document."""

    def __init__(self):
        from app.search.engine import SearchResult

        self.results = [
            SearchResult(chunk_id=1, file_id=9, path=ARCHIVE_PATH, text="2021.pst deposit",
                         score=1.0, rank=1, ext="pst"),
            SearchResult(chunk_id=2, file_id=MESSAGE_ID, path=MESSAGE_PATH,
                         text="We will return the full deposit within ten days.",
                         score=0.9, rank=2),
            SearchResult(chunk_id=3, file_id=3, path="C:/docs/tenancy-deposit.docx",
                         text="The deposit is held in a protected scheme.", score=0.8,
                         rank=3, ext="docx", mtime_ns=1_700_000_000_000_000_000),
        ]

    def search(self, query, **_kwargs):
        return SimpleNamespace(results=list(self.results), unmatched=())


class _Store:
    def messages_for(self, file_ids):
        return {i: dict(META[i]) for i in file_ids if i in META}

    def messages_by_path(self, paths):
        return {}

    def get_state(self, key, default=None):
        return default


def test_a_find_answer_counts_what_it_shows_names_the_message_and_drops_the_archive():
    from app.chat.engine import ChatEngine
    from app.chat.testing import FakeLLM
    from tests.unit.chat_env import ask

    engine = ChatEngine(_Search(), _Store(), FakeLLM(router_answer="FIND"), None)
    turn, _events = ask(engine, "find documents about the deposit")
    assert turn.kind == "find", turn.kind
    paths = [r.path for r in turn.result_set]
    assert ARCHIVE_PATH not in paths and MESSAGE_PATH in paths
    assert "2 documents" in turn.text, "the count is what the list shows"
    assert all(r.path != ARCHIVE_PATH for r in turn.receipts)
    named = {r.path: r.name for r in turn.receipts}
    assert named[MESSAGE_PATH] == SUBJECT
    assert turn.details[MESSAGE_ID]["subject"] == SUBJECT
    dated = {r.path: r.mtime_ns for r in turn.receipts}
    assert dated["C:/docs/tenancy-deposit.docx"] == 1_700_000_000_000_000_000


def test_the_details_survive_a_save_and_a_reopen():
    from app.chat import sessions as stored
    from app.ui import chat_sessions

    turn = ChatTurn("assistant", "x", receipts=[Receipt(MESSAGE_ID, MESSAGE_PATH, SUBJECT, "q",
                                                        mtime_ns=5)], details=META)
    for module in (stored, chat_sessions):
        back = module.turn_from_dict(json.loads(json.dumps(module.turn_to_dict(turn))))
        assert back.details[MESSAGE_ID]["subject"] == SUBJECT
        assert back.receipts[0].mtime_ns == 5


def test_a_conversation_saved_before_today_gets_its_messages_real_names():
    """Saved sessions named a message "2109476" on its receipts and its shelf."""
    from app.ui.chat_sessions import ChatSession, name_messages

    session = ChatSession(id="s", turns=[ChatTurn("assistant", "x", receipts=[
        Receipt(MESSAGE_ID, MESSAGE_PATH, str(MESSAGE_ID), "q")])])
    session.shelf.add(MESSAGE_PATH, str(MESSAGE_ID), MESSAGE_ID)
    name_messages(session, _Store())
    assert session.turns[0].receipts[0].name == SUBJECT
    assert session.turns[0].details[MESSAGE_ID]["sender"] == SENDER
    assert session.shelf.items[0].name == SUBJECT
    name_messages(session, object())                       # a store with no mail: unchanged


# ---------------------------------------------------------------------------
# The widgets
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6")


class _StateStore:
    def __init__(self, saved=""):
        self.values = {"ui:chat_panel": saved} if saved else {}

    def get_state(self, key, default=None):
        return self.values.get(key, default)

    def set_state(self, key, value):
        self.values[key] = value


@pytest.fixture()
def view(qapp, qtbot):
    from app.ui.chat_view import ChatView

    built = ChatView()
    qtbot.addWidget(built)
    built.resize(1300, 800)
    built.show()
    qtbot.waitExposed(built)
    yield built
    built.shutdown()


def _pages(view):
    return (not view.sources.list_page.isHidden(), not view.sources.preview.isHidden())


def test_the_strip_shows_one_page_at_a_time_and_the_open_tab_puts_the_panel_away(view, qtbot):
    from PySide6.QtCore import Qt

    tabs = view.side_tabs.buttons
    assert set(tabs) == {SOURCES_TAB, PREVIEW_TAB}
    assert view.panel.state.open and _pages(view) == (True, False), "Sources shows first"
    assert tabs[SOURCES_TAB].isChecked() and view.toggles["sources"].isChecked()

    qtbot.mouseClick(tabs[PREVIEW_TAB], Qt.MouseButton.LeftButton)
    assert _pages(view) == (False, True)
    assert tabs[PREVIEW_TAB].isChecked() and not tabs[SOURCES_TAB].isChecked()
    assert view.toggles["inspector"].isChecked() and not view.toggles["sources"].isChecked()

    tabs[PREVIEW_TAB].click()                                # the one showing: away
    assert view.sources.isHidden() and _pages(view) == (False, False)
    assert not any(t.isChecked() for t in tabs.values())
    assert not view.side_tabs.isHidden(), "the strip stays, to bring the panel back"
    width = view.split.sizes()
    assert width[2] == 0 and width[1] > 900, "the conversation takes the room"

    tabs[SOURCES_TAB].click()
    assert not view.sources.isHidden() and _pages(view) == (True, False)


def test_the_toolbar_toggles_and_ctrl_b_do_the_same(view):
    view.toggles["inspector"].click()
    assert _pages(view) == (False, True)
    view.toggles["inspector"].click()
    assert view.sources.isHidden()
    view.toggles["sources"].click()
    assert _pages(view) == (True, False)
    view.toggle_panel()
    assert view.sources.isHidden()
    view.toggle_panel()
    assert _pages(view) == (True, False), "back on the page it last showed"
    view.toggle_preview()
    assert _pages(view) == (False, True)
    view.toggle_preview()
    assert view.sources.isHidden()


def test_picking_a_source_in_the_answer_or_the_list_opens_the_preview(view, qtbot):
    from tests.unit.chat_fakes import AGREEMENT, LETTER, deposit_turn

    view.show_turns([ChatTurn("user", "deposit?"), deposit_turn()])
    view.panel.hide()
    bubble = view.bubbles.bubbles()[-1]
    bubble.body.receipt_activated.emit(2)                   # the raised 2 in the prose
    assert _pages(view) == (False, True)
    assert view.sources.preview._row is not None and view.sources.preview._row.path == AGREEMENT.path

    view.panel.show_tab(SOURCES_TAB)
    results = view.sources.results
    index = results._model.index(0, 0)
    rect = results._list.visualRect(index)
    from PySide6.QtCore import Qt

    qtbot.mouseClick(results._list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert _pages(view) == (False, True), "a click on a source shows it"
    assert results.current_row().path == LETTER.path

    view.panel.show_tab(SOURCES_TAB)
    view.sources.walk(1)                                    # arrowing from the box: no
    assert _pages(view) == (True, False)


def test_the_panel_is_remembered_and_put_back(view, qtbot):
    from app.ui.state_writes import pool

    store = _StateStore()
    view.set_store(store)
    view.side_tabs.buttons[PREVIEW_TAB].click()
    assert pool().waitForDone(5000)
    assert store.values["ui:chat_panel"].startswith("open:preview:")
    view.side_tabs.buttons[PREVIEW_TAB].click()
    assert pool().waitForDone(5000)
    assert store.values["ui:chat_panel"].startswith("closed:preview:")

    from app.ui.chat_view import ChatView

    again = ChatView()
    qtbot.addWidget(again)
    again.set_store(_StateStore("closed:preview:420"))
    assert again.sources.isHidden() and again.panel.state == PanelState(False, PREVIEW_TAB, 420)
    again.toggle_panel()
    assert _pages(again) == (False, True)

    wide = ChatView()
    qtbot.addWidget(wide)
    wide.set_store(_StateStore("open:sources:420"))
    wide.resize(1300, 800)
    wide.show()
    qtbot.waitUntil(lambda: abs(wide.split.sizes()[2] - 420) <= 2, timeout=3000)
    wide.shutdown()
    again.shutdown()


def test_a_message_in_the_sources_is_drawn_by_sender_and_subject(view):
    receipt = Receipt(MESSAGE_ID, MESSAGE_PATH, SUBJECT, "We will return the deposit.")
    turn = ChatTurn("assistant", "It comes back in ten days [1].", receipts=[receipt],
                    details=META)
    view.show_turns([ChatTurn("user", "deposit?"), turn])
    from app.ui.result_delegate import ROLE_PAYLOAD

    group = view.sources.results._model.index(0, 0).data(ROLE_PAYLOAD)
    assert group.name == f"Priya Shah — {SUBJECT}" or SUBJECT in group.name
    assert str(MESSAGE_ID) not in group.name and group.when_exact


def test_streamed_sources_are_named_before_the_answer_finishes(view):
    receipt = Receipt(MESSAGE_ID, MESSAGE_PATH, SUBJECT, "We will return the deposit.")
    run = view.begin_answer()
    run.event(SourcesEvent((receipt,), details=META))
    from app.chat.types import TokenEvent
    from app.ui.result_delegate import ROLE_PAYLOAD

    run.event(TokenEvent("Ten days [1]."))
    group = view.sources.results._model.index(0, 0).data(ROLE_PAYLOAD)
    assert SUBJECT in group.name


def test_the_shelf_folds_into_one_control_that_names_each_document(view, qtbot):
    from app.ui.widgets.chat_shelf import NAME_WIDTH

    shelf = view.shelf
    assert shelf.button.isHidden() and not shelf.empty.isHidden(), "nothing yet: one quiet line"
    long_name = "A very long subject line about the deposit and the inventory " * 3
    shelf.add_receipt(Receipt(MESSAGE_ID, MESSAGE_PATH, SUBJECT, "q"))
    shelf.add_receipt(Receipt(3, "C:/docs/lease.pdf", "lease.pdf", "q"))
    shelf.add_receipt(Receipt(4, "pst://2024/4", long_name.strip(), "q"))
    assert not shelf.button.isHidden() and shelf.empty.isHidden()
    assert shelf.button.text() == "3 sources"
    assert shelf.popup.isHidden(), "the chips are folded away"
    shelf.open_list()
    qtbot.waitUntil(shelf.popup.isVisible, timeout=2000)
    chips = shelf.chips()
    assert [c.name_button.text() for c in chips[:2]] == [SUBJECT, "lease.pdf"]
    assert all(str(MESSAGE_ID) not in c.name_button.text() for c in chips)
    assert chips[2].name_button.text().endswith("…") and long_name.strip() in chips[2].name_button.toolTip()
    metrics = chips[2].name_button.fontMetrics()
    assert metrics.horizontalAdvance(chips[2].name_button.text()) <= NAME_WIDTH + 2
    assert [c.pin_button.text() for c in chips] == ["Keep"] * 3
    assert [c.remove_button.text() for c in chips] == ["Remove"] * 3

    chips[1].pin_button.click()
    assert shelf.button.text() == "3 sources, 1 kept"
    shelf.chips()[0].remove_button.click()
    assert shelf.button.text() == "2 sources, 1 kept"
    opened = []
    shelf.open_requested.connect(opened.append)
    shelf.chips()[0].name_button.click()
    assert opened == ["C:/docs/lease.pdf"] and shelf.popup.isHidden()


def _painted_columns(results, row: int, monkeypatch) -> tuple[int, int]:
    """`(title left, snippet left)` of one painted row of a results list."""
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    from app.ui import result_delegate
    from app.ui.result_delegate import ROLE_PAYLOAD

    seen: dict = {}
    real = result_delegate._draw_snippet

    def snippet(painter, snip, rect, *args, **kwargs):
        seen["snippet"] = rect.left()
        return real(painter, snip, rect, *args, **kwargs)

    class Recording(QPainter):
        def drawText(self, *args):                                  # noqa: N802 - Qt
            if len(args) == 3 and args[2] == group.name:
                seen["title"] = args[0].left()
            return super().drawText(*args)

    monkeypatch.setattr(result_delegate, "_draw_snippet", snippet)
    index = results._model.index(row, 0)
    group = index.data(ROLE_PAYLOAD)
    option = QStyleOptionViewItem()
    option.rect = results._list.visualRect(index)
    option.font = results._list.font()
    image = QImage(option.rect.right() + 10, option.rect.bottom() + 10, QImage.Format.Format_ARGB32)
    painter = Recording(image)
    try:
        results._list.itemDelegate().paint(painter, option, index)
    finally:
        painter.end()
    return seen["title"], seen["snippet"]


def test_the_matched_words_line_up_with_the_title_not_under_the_icon(view, monkeypatch):
    """The lead, 2026-10-08: the third line of an answer's result row ("deposit")
    started at the row's left edge, under the type badge. It is the shared results
    delegate, so the Search list had the same fault and is fixed with it."""
    from app.search.engine import SearchResult

    hits = [SearchResult(chunk_id=1, file_id=MESSAGE_ID, path=MESSAGE_PATH, text="deposit back",
                         score=1.0, rank=1),
            SearchResult(chunk_id=2, file_id=3, path="C:/docs/tenancy.docx", text="the deposit",
                         score=0.9, rank=2, ext="docx")]
    turn = ChatTurn("assistant", "Here are the 2 documents matching deposit.", kind="find",
                    result_set=hits, details=META)
    view.show_turns([ChatTurn("user", "find the deposit"), turn])
    results = view.bubbles.bubbles()[-1].results
    for row in (0, 1):                                      # a message and a file
        title, snippet = _painted_columns(results, row, monkeypatch)
        assert snippet == title, (row, title, snippet)
        assert title > results._list.visualRect(results._model.index(row, 0)).left() + 20

    from app.ui.results_view import ResultsView                # the Search tab's list too

    search = ResultsView()
    search.resize(700, 400)
    search.show_results(hits, ["deposit"], details=META)
    search.show()
    title, snippet = _painted_columns(search, 1, monkeypatch)
    assert snippet == title
    search.deleteLater()


def test_every_tab_names_itself_and_explains_itself(view):
    for name, tab in view.side_tabs.buttons.items():
        assert tab.toolTip() == presenter.TAB_TIPS[name]
        assert tab.accessibleName() == f"{presenter.TAB_LABELS[name]} panel"
        assert not tab.icon().isNull()
    toggle = view.toggles["sources"]
    assert toggle.accessibleName() == "Sources panel" and toggle.toolTip()
    assert not toggle.icon().isNull() and toggle.isCheckable()


def test_a_theme_change_repaints_the_strip_and_the_shelf(view):
    from app.ui.theme import PALETTES
    from app.ui.view_options import retint_toggles

    retint_toggles(view, PALETTES["dark"])
    assert PALETTES["dark"]["window"] in view.side_tabs.styleSheet()
    assert PALETTES["dark"]["border"] in view.shelf.styleSheet()
    retint_toggles(view, PALETTES["light"])
    assert PALETTES["light"]["window"] in view.side_tabs.styleSheet()
