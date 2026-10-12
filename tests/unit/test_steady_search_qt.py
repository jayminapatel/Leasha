"""UI phase B, a steady Search page, on a real `SearchView`.

Layer: L5

The owner wants Search - the front door - to stop moving under the person using it.
Each test here is one way it moved: the box jumping between the opening page and the
bar, the answer card and the contact strip pushing rows that were already painted, the
notice bar doing the same, and rows that said less than they knew (a missing file only
in the tooltip, the badge carrying a word and an icon, matched words marked in the
passage but not in the name). The Sort control is here because it lives on the bar.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool                                  # noqa: E402
from PySide6.QtWidgets import QApplication                              # noqa: E402

from app.storage.sqlite_store import SqliteStore                        # noqa: E402

pytestmark = pytest.mark.gui


def settle(rounds: int = 30) -> None:
    app = QApplication.instance()
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()


@pytest.fixture
def world(tmp_path, qapp):
    """A real keyword-only engine over three small files, and a view over it."""
    from app.search.engine import SearchEngine
    from app.storage.vector_store import ImageVectorStore, VectorStore
    from app.ui.search_view import SearchView
    from tests.unit.conftest import _NoModel

    store = SqliteStore(tmp_path / "search.db").connect()
    for n, (name, text) in enumerate((("b-boiler.txt", "The boiler quote from Ann."),
                                      ("c-boiler.txt", "Boiler service notes for May."),
                                      ("a-boiler.txt", "A boiler house report."))):
        fid = store.upsert_file(f"C:/work/{name}", parent_dir="C:/work", ext="txt", size_bytes=10,
                                mtime_ns=1_700_000_000_000_000_000 + n * 10**12,
                                status="INDEXED", source_kind="file")
        store.replace_chunks(fid, [{"ordinal": 0, "text": text}])
    vectors = VectorStore(tmp_path / "vec", dim=8)
    images = ImageVectorStore(tmp_path / "vec")
    engine = SearchEngine(store, vectors, _NoModel(), image_vectors=images)
    view = SearchView(engine)
    view.resize(1100, 760)
    view.show()
    settle()
    yield view, engine, store
    view.shutdown()
    view.hide()
    view.deleteLater()
    engine.close()
    vectors.close()
    images.close()
    store.close()


def _search(view, text: str) -> None:
    view.input.setText(text)
    view._interim_timer.stop()
    view._full_timer.stop()
    view.search_now()
    settle()


def _list_top(view) -> int:
    lst = view.results._list
    return lst.mapTo(view, lst.rect().topLeft()).y()


# -- 1. the box stays put ------------------------------------------------------------------

def test_the_box_is_never_moved_between_the_home_page_and_the_bar(world):
    view, _engine, _store = world
    view.input.setText("")
    settle()
    parent, top = view.input.parentWidget(), view.input.mapTo(view, view.input.rect().topLeft()).y()
    view.input.setText("boiler")
    settle()
    assert view.input.parentWidget() is parent
    assert view.input.mapTo(view, view.input.rect().topLeft()).y() == top
    view.input.setText("")
    settle()
    assert view.input.parentWidget() is parent
    assert view.input.mapTo(view, view.input.rect().topLeft()).y() == top


def test_the_home_content_sits_under_the_box(world):
    view, _engine, _store = world
    view.input.setText("")
    settle()
    assert view.home.isVisibleTo(view)
    box_bottom = view.input.mapTo(view, view.input.rect().bottomLeft()).y()
    assert view.home.mapTo(view, view.home.rect().topLeft()).y() > box_bottom
    assert view.input.property("empty_state") is True     # the roomier opening look, by styling


# -- 2. nothing pushes rows already on screen ---------------------------------------------

def test_a_notice_arriving_late_does_not_push_the_rows(world):
    from app.search.engine import Notice

    view, _engine, _store = world
    _search(view, "boiler")
    top = _list_top(view)
    view.notices.show_notices([Notice("NOTICE_SPELLING", "A notice that arrives after the rows.")])
    settle()
    assert not view.notices.isHidden()
    assert _list_top(view) == top
    view.notices.show_notices([])
    settle()
    assert _list_top(view) == top


class _Chat:
    """Chat's controller as the card sees it: an engine, and whether it is busy."""

    def __init__(self, engine):
        self.engine, self._ask = engine, None

    def _engine_now(self):
        return self.engine, 0

    def _adopt(self, *_a):
        pass


@pytest.fixture
def card(world):
    from tests.unit.chat_fakes import FakeChatEngine

    view, _engine, _store = world
    card = view.answer_card
    fake = FakeChatEngine()
    card.connect_chat(_Chat(fake), enabled=lambda: True)
    card.answer_timer.setInterval(10)
    return view, card, fake


QUESTION = "What did we agree with the landlord about the deposit?"


def test_the_answer_card_holds_its_place_from_the_first_keystroke(card, qtbot):
    view, card, _fake = card
    view.input.setText(QUESTION)
    # Reserved at once - before the 1.2 s pause, before any row is painted.
    assert not card.isHidden()
    reserved = card.height() or card.sizeHint().height()
    qtbot.waitUntil(lambda: "protected scheme" in card.body.text(), timeout=5000)
    settle()                                               # the answer landed...
    assert card.height() == reserved == card.maximumHeight()   # ...into the same slot


def test_an_answer_that_never_comes_keeps_the_slot_for_that_query(card, qtbot):
    view, card, fake = card
    from app.chat.types import ChatTurn

    fake.script = lambda _q: ([], ChatTurn("assistant", "", kind="not_found"))
    from app.ui.widgets.answer_card import NO_ANSWER

    view.input.setText(QUESTION)
    reserved = card.height()
    qtbot.waitUntil(lambda: card.body.text() == NO_ANSWER, timeout=5000)
    settle()
    assert fake.calls                                      # it was asked, and said nothing
    assert not card.isHidden() and card.body.text() == NO_ANSWER
    assert card.height() == reserved                       # collapsing would pull the rows up


def test_hide_puts_the_card_away_for_that_query_only(card):
    view, card, _fake = card
    view.input.setText(QUESTION)
    settle()
    assert card.hide_button.toolTip()
    card.hide_button.click()
    assert card.isHidden()
    card.typed(QUESTION)                                  # the same words again: still hidden
    card.submitted()
    settle()
    assert card.isHidden()
    view.input.setText(QUESTION + " And the gas?")
    assert not card.isHidden()                            # a new question gets its card


def test_a_contact_found_after_the_rows_waits_for_the_next_set_of_rows(world):
    from app.ui.widgets.person_card import PersonInfo

    view, _engine, _store = world
    strip = view.person_strip
    _search(view, "boiler")
    top = _list_top(view)
    strip._landed([PersonInfo(1, "Ann Lee")], strip._generation, "boiler")
    settle()
    assert strip.isHidden() and _list_top(view) == top     # held, not pushed in
    view.results._rebuild()                                # the same rows again: still held
    settle()
    assert strip.isHidden()
    view.results.rows_changed.emit([object()])             # rows that move anyway: it moves with them
    settle()
    assert not strip.isHidden() and [c.info.name for c in strip.cards] == ["Ann Lee"]


# -- 3. the status line ----------------------------------------------------------------------

def test_the_status_line_says_documents_and_keeps_the_timing_in_its_tooltip(world):
    view, _engine, _store = world
    _search(view, "boiler")
    # This engine has no model, so the line may carry the meaning-search sentence
    # instead; the count is then the results pane's own summary, as it always was.
    said = (view.status.text(), view.results._summary.text())
    assert "3 documents" in said, said
    assert "result(s)" not in " ".join(said)
    assert "ms" in view.status.toolTip()


# -- 4. one row per real item ---------------------------------------------------------------

def _group_index(view):
    from app.ui.result_delegate import ROLE_PAYLOAD
    from app.ui.presenter import ResultGroup

    model = view.results._model
    for i in range(model.rowCount()):
        if isinstance(model.index(i, 0).data(ROLE_PAYLOAD), ResultGroup):
            return model.index(i, 0)
    raise AssertionError("no group row")


def _paint(view, index):
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    lst = view.results._list
    option = QStyleOptionViewItem()
    lst.initViewItemOption(option)
    option.rect = lst.visualRect(index)
    option.rect.moveTo(0, 0)
    image = QImage(option.rect.size(), QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    try:
        lst.itemDelegate().paint(painter, option, index)
    finally:
        painter.end()


def test_a_missing_file_is_marked_on_the_row_in_the_warning_colour(world):
    from app.ui.result_delegate import ROLE_MISSING
    from app.ui.presenter import MISSING_MARK

    view, _engine, _store = world
    _search(view, "boiler")
    index = _group_index(view)
    view.results._model.itemFromIndex(index).setData(True, ROLE_MISSING)
    word, token = view.results._list.itemDelegate().side_word(index)
    assert (word, token) == (MISSING_MARK, "warning")


def test_the_badge_shows_its_word_and_no_corner_icon(world, monkeypatch):
    import app.ui.result_delegate as delegate

    view, _engine, _store = world
    _search(view, "boiler")

    def no_icons(*_a, **_k):
        raise AssertionError("the badge asked for a corner icon")

    monkeypatch.setattr(delegate, "_icon_for", no_icons, raising=False)
    _paint(view, _group_index(view))


def test_matched_words_are_marked_in_the_name_too(world, monkeypatch):
    import app.ui.result_delegate as delegate

    view, _engine, _store = world
    _search(view, "boiler")
    drawn = []
    real = delegate._draw_run

    def spy(painter, text, *args, **kwargs):
        drawn.append((text, args[2] if len(args) > 2 else None, kwargs.get("mark")))
        return real(painter, text, *args, **kwargs)

    monkeypatch.setattr(delegate, "_draw_run", spy)
    _paint(view, _group_index(view))
    named = [d for d in drawn if d[0].endswith(".txt")]
    assert named, drawn
    text, highlights, mark = named[0]
    assert highlights and text[highlights[0][0]:highlights[0][1]].lower() == "boiler"
    assert mark is not None


# -- 5. the sort control -------------------------------------------------------------------

def test_the_sort_control_sets_the_typed_sort_and_follows_it(world):
    view, _engine, _store = world
    sort = view.sort_button
    assert [a.text() for a in sort.menu().actions()] == ["Best match", "Newest", "Oldest", "Name"]
    view.input.setText("boiler")
    settle()
    sort.choose("name")
    settle()
    assert view.input.text() == "boiler /sort name"
    assert "Name" in sort.text()
    names = [view.results._model.index(i, 0).data(
        __import__("app.ui.result_delegate", fromlist=["ROLE_PAYLOAD"]).ROLE_PAYLOAD)
        for i in range(view.results._model.rowCount())]
    shown = [getattr(n, "name", "") for n in names if getattr(n, "name", "")]
    assert shown == sorted(shown), shown
    view.input.setText("boiler /oldest")
    assert "Oldest" in sort.text()
    sort.choose("")
    assert view.input.text() == "boiler"
    assert "Best match" in sort.text()


# -- the review of PR 74 --------------------------------------------------------------------

def test_choosing_a_sort_leaves_the_spaces_inside_a_quoted_filter_alone():
    from app.ui.presenter.search import with_sort

    assert with_sort('boiler name:"Project  Plan" /newest', "oldest") == 'boiler name:"Project  Plan" /oldest'


def test_typing_over_painted_rows_still_holds_a_card_until_the_new_rows(world):
    """A query edited with rows on screen: those rows are still painted until replaced."""
    from app.ui.widgets.person_card import PersonInfo

    view, _engine, _store = world
    strip = view.person_strip
    _search(view, "boiler")
    strip.typed("boiler q")                                # the person types on; old rows still there
    strip._landed([PersonInfo(1, "Ann Lee")], strip._generation, "boiler q")
    settle()
    assert strip.isHidden()
    view.results.rows_changed.emit([object()])
    settle()
    assert not strip.isHidden()


def test_a_missing_file_says_missing_to_a_screen_reader():
    from types import SimpleNamespace

    from PySide6.QtCore import Qt

    from app.ui.widgets.results_items import result_item

    payload = SimpleNamespace(path="C:/gone/a.pdf", name="a.pdf", file_id=1)
    item = result_item(payload, expanded=False, missing={"C:/gone/a.pdf"}, volumes={}, placeholders=set())
    assert "Missing" in item.data(int(Qt.ItemDataRole.AccessibleTextRole))


def test_a_list_emptied_by_another_action_does_not_keep_the_old_count(world):
    from types import SimpleNamespace

    from app.ui.presenter import status_line

    view, _engine, _store = world
    response = SimpleNamespace(results=[SimpleNamespace(file_id=1)], interim=False, elapsed_ms=5)
    view.status.counted(response, status_line(response))
    assert view.status.text() == "1 document"
    view.status.recount(0)
    assert view.status.text() == "No documents"


def test_a_link_cut_off_by_the_one_line_notice_stays_clickable(world):
    from types import SimpleNamespace

    view, _engine, _store = world
    bar = view.notices
    long = "word " * 80
    notice = SimpleNamespace(code="X", message=long + 'Search <a href="apply:type:pdf">PDF files only</a>?')
    bar.show_notices([notice])
    settle()
    assert "apply:type:pdf" in bar.label.text()               # drawn with its link
    assert not bar.action.isHidden() and "apply:type:pdf" in bar.action.text()
    chosen = []
    bar.chosen.connect(chosen.append)
    bar.action.linkActivated.emit("apply:type:pdf")
    assert chosen == ["apply:type:pdf"]


def test_long_source_names_scroll_with_the_answer_inside_the_slot(card):
    view, card, _fake = card
    assert card.sources.parentWidget() is not None
    assert card.scroll.widget().isAncestorOf(card.sources)
