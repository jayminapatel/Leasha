"""One answer, said once: the Chat tab's shelf, sources, notes and panel. 2026-10-12.

Layer: L5 (the presenter rules and the engine without a display; the widgets with one)

An audit of the Chat tab found one FIND answer showing the same files in four
places - the shelf (every receipt, up to twelve, past the engine's five), the
Sources tab (every uncited receipt poured in), the prose ("That's all N.") and the
inline list ("That's all - N results.") - with the side panel open on the first
run before anything was asked. It also found the engine's explanation notes ("No
AI model is running ...") never reaching the screen on a finished answer, a hover
over an older answer's [n] showing the newest answer's passage, the web prompt's
focus on Allow (a stray Enter sends a search), and every hit with no file id
collapsing into one source. The owner: "do the recommended to make it world class
and friendly and do it all".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.chat.context import build_sources
from app.chat.types import ChatTurn, Receipt, ShelfEvent
from app.ui.presenter.chat import PanelState, closing_line


def _receipts(count: int) -> list[Receipt]:
    return [Receipt(300 + i, f"C:/docs/report-{i}.pdf", f"report-{i}.pdf", f"quote {i}", "")
            for i in range(count)]


class _Shelf:
    def __init__(self):
        self.added: list[str] = []

    def add_receipt(self, receipt):
        self.added.append(receipt.path)


class _Sources:
    def __init__(self):
        self.added: list[int] = []

    def add(self, number, _receipt):
        self.added.append(number)

    def clear(self):
        self.added.clear()

    def set_details(self, _details):
        return None


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


# -- 1 and 6a: a list answer's shelf and Sources are what the engine chose ----------

def test_a_find_answer_shelves_only_what_the_engine_sent_and_leaves_sources_to_its_list(qapp):
    from app.ui.widgets.chat_answer_run import AnswerRun
    from app.ui.widgets.chat_bubbles import AnswerBubble

    receipts = _receipts(12)
    shelf, sources = _Shelf(), _Sources()
    run = AnswerRun(AnswerBubble(), sources, shelf)
    for receipt in receipts[:5]:                      # the engine's _FIND_SHELF
        run.event(ShelfEvent(receipt))
    turn = ChatTurn("assistant", "Here are the 12 documents matching reports. ",
                    receipts=receipts, result_set=[SimpleNamespace(path=r.path) for r in receipts],
                    kind="find")
    run.finish(turn)
    assert shelf.added == [r.path for r in receipts[:5]], "five chips, not twelve"
    assert sources.added == [], "the inline list is this answer's source list"


def test_a_written_answer_still_puts_what_it_cites_on_the_shelf(qapp):
    from app.ui.widgets.chat_answer_run import AnswerRun
    from app.ui.widgets.chat_bubbles import AnswerBubble
    from tests.unit.chat_fakes import AGREEMENT, LETTER, deposit_turn

    shelf, sources = _Shelf(), _Sources()
    AnswerRun(AnswerBubble(), sources, shelf).finish(deposit_turn())
    assert shelf.added == [LETTER.path, AGREEMENT.path]
    assert sources.added == [1, 2]


# -- 2: the engine's explanation reaches a finished answer -------------------------

def test_a_finished_answer_says_why_it_is_quoted_and_not_what_it_searched_for():
    from app.chat.engine import _NO_MODEL_NOTE, _QUOTED_NOTE

    quoted = ChatTurn("assistant", "x", kind="answer", notes=["Searched for: deposit", _QUOTED_NOTE])
    assert closing_line(quoted) == _QUOTED_NOTE
    assert closing_line(ChatTurn("assistant", "x", notes=[_NO_MODEL_NOTE])) == _NO_MODEL_NOTE
    plain = ChatTurn("assistant", "x", kind="find", result_set=[1],
                     notes=["Searched for: deposit", "Searched for: bond"])
    assert closing_line(plain) == "", "the search log is not a closing line"
    assert closing_line(quoted, stopped=True) != _QUOTED_NOTE, "stopped still wins"


# -- 3: a hover in an older answer shows that answer's passage ---------------------

def test_hovering_a_number_in_an_older_answer_shows_its_own_passage(view):
    from tests.unit.chat_fakes import LETTER, deposit_turn

    other = Receipt(900, "C:/docs/boiler.pdf", "boiler.pdf", "The boiler was serviced in May.", "")
    view.show_turns([ChatTurn("user", "deposit?"), deposit_turn(),
                     ChatTurn("user", "boiler?"),
                     ChatTurn("assistant", "Serviced in May [1].", receipts=[other])])
    older = view.bubbles.bubbles()[1]
    older.body.receipt_hovered.emit(1)
    assert "protected scheme" in view.sources.passage.text(), LETTER.quote
    assert "boiler" not in view.sources.passage.text()


# -- 4: Enter on the web prompt does not send anything -----------------------------

def test_the_web_prompt_gives_its_focus_to_skip(view, qtbot):
    run = view.begin_answer()
    run.bubble.ask_web("tenancy deposit rules")
    qtbot.waitUntil(lambda: run.bubble.web_skip.isVisible())
    assert run.bubble.focusWidget() is run.bubble.web_skip


# -- 5: hits with no file id stay separate sources ---------------------------------

def test_results_with_no_file_id_are_not_one_source():
    hits = [SimpleNamespace(chunk_id=i + 1, file_id=None, path=f"C:/notes/{name}.txt",
                            text=f"{name} deposit", page=None, label="", score=1.0)
            for i, name in enumerate(("one", "two", "three"))]
    sources = build_sources(hits, ["deposit"], max_sources=6, window_tokens=4096)
    assert [s.path for s in sources] == [h.path for h in hits]
    assert all(s.file_id is None for s in sources)


def test_a_find_answer_counts_hits_with_no_file_id_one_each():
    from app.chat.engine import ChatEngine
    from app.chat.testing import FakeLLM
    from app.search.engine import SearchResult
    from tests.unit.chat_env import ask

    hits = [SearchResult(chunk_id=i + 1, file_id=0, path=f"C:/notes/deposit-{i}.txt",
                         text="the deposit", score=1.0 - i / 10, rank=i + 1, ext="txt")
            for i in range(3)]
    search = SimpleNamespace(search=lambda _q, **_k: SimpleNamespace(results=list(hits), unmatched=()))
    store = SimpleNamespace(messages_for=lambda _ids: {}, messages_by_path=lambda _p: {},
                            get_state=lambda _k, default=None: default)
    turn, _events = ask(ChatEngine(search, store, FakeLLM(router_answer="FIND"), None),
                        "find documents about the deposit")
    assert turn.kind == "find" and len(turn.result_set) == 3
    assert "3 documents" in turn.text


# -- 6b: the count is said once, by the list ---------------------------------------

def test_a_find_answer_leaves_that_s_all_to_its_list():
    from app.chat.engine import ChatEngine
    from app.chat.testing import FakeLLM
    from tests.unit.chat_env import ask
    from tests.unit.test_chat_side_panel import _Search, _Store

    turn, _events = ask(ChatEngine(_Search(), _Store(), FakeLLM(router_answer="FIND"), None),
                        "find documents about the deposit")
    assert turn.kind == "find" and "2 documents" in turn.text
    assert "That's all" not in turn.text, "the inline list ends 'That's all - 2 results.'"


# -- 6c: the side panel waits to be asked for --------------------------------------

def test_the_side_panel_starts_away_and_a_citation_opens_it_at_preview(view):
    from tests.unit.chat_fakes import AGREEMENT, deposit_turn

    assert PanelState().open is False
    assert view.sources.isHidden(), "the conversation has the room until a source is wanted"
    view.show_turns([ChatTurn("user", "deposit?"), deposit_turn()])
    view.bubbles.bubbles()[-1].body.receipt_activated.emit(2)
    assert not view.sources.isHidden() and not view.sources.preview.isHidden()
    assert view.sources.preview._row.path == AGREEMENT.path
