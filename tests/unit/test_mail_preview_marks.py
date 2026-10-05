r"""Order 0y section 4b: the searched words, highlighted, with F3 and Shift+F3.

Layer: L5

> **4b** The searched words highlighted in the body, with next and previous (F3
> and Shift+F3), exactly as the file preview does.

**What was there before this.** The file preview had Ctrl+F - a find box that
highlights what is typed into it and steps with Enter - and nothing else: no
searched word was highlighted on its own, and F3 did nothing anywhere
(`grep Key_F3 app/` was empty). So "exactly as the file preview does" is kept by
building it once, in the pane, for both: a text file and a message are
highlighted by the same code, with the same keys, in the find box's own colour.

* Which characters are a searched word is decided without Qt
  (`presenter.snippets.term_spans`), by the rule the result snippets use.
* `widgets/search_marks.py` paints them and steps between them.
* The Mail tab's words are its `/subject` value and anything typed that the
  list could not filter on (`presenter.mail.mail_terms`).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.search.commands import expand_slashes
from app.search.query import parse_query
from app.ui.presenter.mail import mail_terms
from app.ui.presenter.snippets import term_spans

BODY = "The trip is on Friday.\n\nIf the trip is cancelled, the trips in May still run."


# --- which characters, without Qt ---------------------------------------------------

def test_every_occurrence_of_a_searched_word_is_found() -> None:
    spans = term_spans(BODY, ["trip"])

    assert [BODY[a:b] for a, b in spans] == ["trip", "trip", "trip"]
    assert spans[0] == (4, 8)


def test_it_is_the_rule_the_result_snippets_use() -> None:
    """A word is matched from its start, in any case - never inside another word."""
    assert [a for a, _b in term_spans("Strip the TRIP tripwire", ["trip"])] == [10, 15]


def test_several_words_and_a_phrase() -> None:
    spans = term_spans("the school trip and the school", ["school trip", "school"])

    assert [("the school trip and the school")[a:b] for a, b in spans] == ["school trip", "school"]


def test_nothing_searched_is_nothing_highlighted() -> None:
    assert term_spans(BODY, []) == []
    assert term_spans(BODY, ["", "  "]) == []
    assert term_spans("", ["trip"]) == []


def test_positions_count_the_way_qt_counts() -> None:
    r"""Qt positions are UTF-16 units; an emoji is two of them and one Python
    character. Without the correction every highlight after one is a character
    early - on exactly the messages people send each other."""
    text = "\U0001F600 trip"

    assert term_spans(text, ["trip"]) == [(2, 6)]
    assert term_spans(text, ["trip"], utf16=True) == [(3, 7)]


def _parsed(text: str):
    return parse_query(expand_slashes(text))


def test_the_mail_tabs_searched_words_are_its_subject_and_what_was_typed() -> None:
    assert mail_terms(_parsed("/subject trip")) == ["trip"]
    assert mail_terms(_parsed("coat /from dave")) == ["coat"]
    assert mail_terms(_parsed("/from dave")) == []
    assert mail_terms(None) == []


# --- painted, and stepped through ---------------------------------------------------

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.preview_loader import KIND_TEXT, Preview  # noqa: E402
from app.ui.widgets.preview import PreviewPane  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _pane(terms, body=BODY, *, mail=False) -> PreviewPane:
    pane = PreviewPane()
    pane.terms_provider = lambda: terms
    pane.show()
    pane.show_row(SimpleNamespace(file_id=7, path="D:/x/note.txt", name="note.txt"))
    pane._timer.stop()
    meta = {}
    if mail:
        from tests.unit.test_mail_preview_card import CARD
        from app.ui.preview_loader import MailPreview

        meta = {"mail": MailPreview(file_id=7, card=CARD, body=body, copy_header="From: x\n\n")}
    pane._rendered(Preview(kind=KIND_TEXT, body=body, path="D:/x/note.txt", meta=meta),
                   pane._generation)
    return pane


def _selected(pane) -> tuple[int, str]:
    cursor = pane.text.textCursor()
    return cursor.selectionStart(), cursor.selectedText()


def test_the_searched_word_is_highlighted_in_a_message(qapp) -> None:
    pane = _pane(["trip"], mail=True)

    marks = pane.text.extraSelections()

    assert [m.cursor.selectedText() for m in marks] == ["trip", "trip", "trip"]
    assert pane.marks.count == 3
    pane.close()


def test_a_text_file_is_highlighted_by_the_same_code(qapp) -> None:
    pane = _pane(["trip"])

    assert len(pane.text.extraSelections()) == 3
    pane.close()


def test_f3_goes_to_the_next_and_wraps(qapp) -> None:
    pane = _pane(["trip"], mail=True)

    seen = []
    for _ in range(4):
        pane.marks.next()
        seen.append(_selected(pane))

    assert [text for _pos, text in seen] == ["trip"] * 4
    assert [pos for pos, _text in seen] == [4, 31, 54, 4], "third, then back to the first"
    pane.close()


def test_shift_f3_goes_back_and_wraps(qapp) -> None:
    pane = _pane(["trip"], mail=True)

    pane.marks.previous()
    last = _selected(pane)[0]
    pane.marks.previous()

    assert (last, _selected(pane)[0]) == (54, 31)
    pane.close()


def test_the_keys_are_f3_and_shift_f3(qapp) -> None:
    pane = _pane(["trip"])

    assert pane.marks.next_key.key() == QKeySequence(Qt.Key.Key_F3)
    assert pane.marks.previous_key.key() == QKeySequence("Shift+F3")
    pane.marks.next_key.activated.emit()
    assert _selected(pane) == (4, "trip")
    pane.marks.previous_key.activated.emit()
    assert _selected(pane)[0] == 54
    pane.close()


def test_the_pane_says_where_it_is(qapp) -> None:
    pane = _pane(["trip"], mail=True)

    assert pane.marks.position() == "3 matches"
    pane.marks.next()
    assert pane.marks.position() == "1 of 3"
    pane.close()


def test_nothing_searched_highlights_nothing_and_f3_is_harmless(qapp) -> None:
    pane = _pane([])

    pane.marks.next()
    pane.marks.previous()

    assert pane.text.extraSelections() == []
    assert pane.marks.position() == ""
    pane.close()


def test_a_word_after_an_emoji_is_highlighted_where_it_is(qapp) -> None:
    pane = _pane(["trip"], body="\U0001F600 the trip")

    assert [m.cursor.selectedText() for m in pane.text.extraSelections()] == ["trip"]
    pane.close()


def test_the_highlight_does_not_outlive_its_message(qapp) -> None:
    pane = _pane(["trip"], mail=True)

    pane.show_row(SimpleNamespace(path="D:/x/other.txt", name="other.txt"))
    pane._timer.stop()

    assert pane.text.extraSelections() == []
    assert pane.marks.count == 0
    pane.close()


def test_the_find_box_takes_f3_while_it_is_open_and_gives_the_words_back(qapp) -> None:
    """Ctrl+F is the person's own search; F3 follows it while it is up, and the
    searched words are highlighted again when it is closed."""
    pane = _pane(["trip"], mail=True)

    pane.find.focus()
    pane.find.box.setText("Friday")
    assert [m.cursor.selectedText() for m in pane.text.extraSelections()] == ["Friday"]
    pane.marks.next()
    assert _selected(pane)[1] == "Friday"

    pane.find.dismissed.emit()

    assert [m.cursor.selectedText() for m in pane.text.extraSelections()] == ["trip"] * 3
    pane.close()


def test_highlighting_never_edits_the_document(qapp) -> None:
    pane = _pane(["trip"], mail=True)
    pane.marks.next()

    assert pane.text.toPlainText() == BODY
    assert not pane.text.document().isModified()
    pane.close()


def test_the_search_tabs_words_reach_its_pane(qapp) -> None:
    """`attach_preview` reads the words from the list it is attached to."""
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QWidget

    from app.ui.widgets.preview import attach_preview

    class Results(QWidget):
        selected = Signal(object)
        explain_context = staticmethod(lambda: (["trip", "coat"], None))

    results = Results()
    pane, _split = attach_preview(results, lambda _r: None, lambda _e: None)

    assert list(pane.terms_provider()) == ["trip", "coat"]
    results.explain_context = None
    assert list(pane.terms_provider()) == []


def test_a8_a_message_opened_from_the_mail_tab_has_its_word_highlighted(qapp, tmp_path) -> None:
    """Acceptance A8, on the Mail tab: `/subject trip`, a message previewed,
    "trip" highlighted and F3 moving between the hits."""
    from app.storage.sqlite_store import SqliteStore
    from app.ui.mail_view import MailView
    from tests.unit.test_mail_preview_card import (
        TUESDAY, add_message, preview_now, pump,
    )

    with SqliteStore(tmp_path / "index.db") as store:
        add_message(store, path="pst://Archive/E1", subject="School trip",
                    sender="dave@acme.com", sent_at=TUESDAY, body=BODY)
        view = MailView(store)
        try:
            view.input.setText("/subject trip")
            # 2026-09-30: typing started the 120ms debounce, and when it fired
            # during the waits below it ran the filter a second time, refilled
            # the list and cleared the marks just counted. Seen as this test
            # failing (0 matches) only when run straight after a Search test.
            view._timer.stop()
            view._run()
            pump()
            preview_now(view.preview, view._rows[0])

            pane = view.preview
            assert pane.marks.count == 3
            assert "3 matches" in pane.match_note.text() and "F3" in pane.match_note.text()
            pane.marks.next()
            pane.marks.next()
            assert _selected(pane) == (31, "trip")
            assert pane.match_note.text().startswith("2 of 3")
        finally:
            view.shutdown()
