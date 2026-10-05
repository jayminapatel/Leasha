r"""Order 0m section 1b's two remaining backlog items, as real keystrokes and
clicks through the assembled `MainWindow`:

1. **The pop-out against a real photo.** `test_gui_scenarios.py` covers the
   pop-out opening, staying on top and closing, but its fixtures are text; a
   photo is what makes Rotate mean something, and Ctrl+F needs a document with
   words in it.
2. **The "eight-year-old" scenarios of the search-experience order (its §7)**,
   which `test_eight_year_old.py` already proves at the engine
   (`engine.search(...)`). Here the same sentences are typed into the box and
   the assertion is what a child would *see*: a row, a line in the notice bar,
   a chip - never a return value.

Offline Media Scan opens a native drive-browse dialog and stays out of scope
(Rescan/Delete are covered in `test_gui_scenarios.py`).

Every scenario runs with no model at all: the harness's embedder raises on any
call, so nothing here can pass only because a machine happens to have one.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from tests.unit.conftest import gui_pump, gui_row_count, gui_select_row  # noqa: E402

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _names(view) -> list[str]:
    return [row.path.rsplit("/", 1)[-1] for row in view.results._rows]


def _bar(view) -> str:
    """What the notice bar says, when it is showing.

    `isHidden`, not `not isVisible`: the harness never `show()`s the window, so
    `isVisible()` is False for every widget in it, showing or not."""
    return view.notices.label.text() if not view.notices.isHidden() else ""


def _type(qtbot, app, view, text: str) -> None:
    """Clear the box and type `text`, one key at a time, as a person does."""
    view.input.clear()
    gui_pump(app)
    qtbot.keyClicks(view.input, text)


@pytest.fixture(scope="module")
def journeys(gui_mainwindow, tmp_path_factory):
    """The shared window plus a child's homework folder and one real photo."""
    from PIL import Image

    app, window, store, engine = gui_mainwindow

    for name, text, ext, sender in (
        ("volcanoes.docx",
         "My homework about volcanoes. A volcano erupts when magma rises "
         "through a crack in the crust. Mount Etna is in Sicily.", "docx", ""),
        ("spellings.docx",
         "Spelling list for this week: separate, necessary, rhythm.", "docx", ""),
        ("rivers.docx",
         "My homework about rivers and how they carve valleys over time.", "docx", ""),
        ("dad-school-trip.eml",
         "The trip to the science museum is on the fifteenth. Bring a packed lunch.",
         "eml", "dave.smith@acme.com"),
    ):
        file_id = store.upsert_file(
            f"C:/Users/child/{name}", parent_dir="C:/Users/child", ext=ext,
            size_bytes=1, mtime_ns=1, status="INDEXED",
            source_kind="eml" if ext == "eml" else "file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
        if sender:
            store.set_message(file_id, subject="School trip", sender=sender,
                              recipients='["me@acme.com"]', has_attach=0)

    # A landscape photo: 240 wide, 120 high, so a quarter turn is visible as a
    # change of shape, not merely of pixels. Left half red, right half blue,
    # so the turn is real content and not a symmetric blank.
    folder = tmp_path_factory.mktemp("photos")
    photo = folder / "harbour-sunset.jpg"
    image = Image.new("RGB", (240, 120), (200, 30, 30))
    image.paste(Image.new("RGB", (120, 120), (30, 30, 200)), (120, 0))
    image.save(photo, "JPEG")

    photo_id = store.upsert_file(
        photo.as_posix(), parent_dir=folder.as_posix(), ext="jpg",
        size_bytes=photo.stat().st_size, mtime_ns=1, status="INDEXED",
        source_kind="file")
    store.replace_chunks(photo_id, [{"ordinal": 0, "text": "harbour sunset holiday photo"}])

    note = folder / "harbour-notes.txt"
    body = ("Harbour survey notes. The quay wall needs repair.\n"
            "The quay lights failed twice. Repair the quay before winter.\n")
    note.write_text(body, encoding="utf-8")
    note_id = store.upsert_file(
        note.as_posix(), parent_dir=folder.as_posix(), ext="txt",
        size_bytes=len(body), mtime_ns=1, status="INDEXED", source_kind="file")
    store.replace_chunks(note_id, [{"ordinal": 0, "text": body}])

    return app, window, store, engine, photo, note


def _pop_out_first_result(qtbot, app, window, query: str):
    view = window.search_view
    _type(qtbot, app, view, query)
    qtbot.waitUntil(lambda: gui_row_count(view.results) > 0, timeout=4000)
    qtbot.wait(700)
    gui_select_row(view.results, 0)
    gui_pump(app)

    before = len(window._pinned)
    qtbot.mouseClick(view.preview.pop_button, Qt.MouseButton.LeftButton)
    gui_pump(app)
    assert len(window._pinned) == before + 1
    return window._pinned[-1]


def _drawn_size(popped) -> tuple[int, int]:
    pixmap = popped.picture.pixmap()
    return (pixmap.width(), pixmap.height()) if pixmap is not None else (0, 0)


# ---------------------------------------------------------------------------
# pop-out: a real photo, rotated by a real click
# ---------------------------------------------------------------------------

def test_a_popped_out_photo_is_drawn_and_rotates_a_quarter_turn_per_click(journeys, qtbot):
    app, window, *_ = journeys
    popped = _pop_out_first_result(qtbot, app, window, "harbour sunset")
    try:
        qtbot.waitUntil(lambda: popped.rotate.isEnabled() and _drawn_size(popped)[0] > 0,
                        timeout=5000)
        wide, high = _drawn_size(popped)
        assert wide > high, "the photo is landscape before any turn"

        seen = []
        for _ in range(4):
            before = _drawn_size(popped)
            qtbot.mouseClick(popped.rotate, Qt.MouseButton.LeftButton)
            qtbot.waitUntil(lambda b=before: _drawn_size(popped) != b, timeout=5000)
            seen.append(_drawn_size(popped))

        first, second, third, fourth = seen
        assert first[1] > first[0], "one quarter turn: portrait"
        assert second[0] > second[1], "two quarter turns: landscape again"
        assert third[1] > third[0], "three quarter turns: portrait again"
        assert fourth == (wide, high), "four quarter turns is back where it began"
    finally:
        popped.close()
        gui_pump(app)


def test_a_rotation_is_remembered_the_next_time_the_photo_is_popped_out(journeys, qtbot):
    r"""The Rotate tooltip promises "Remembered for this file; the file itself
    is never changed." - so pop it out again after closing and it is still on
    its side, while the file on disk is byte-for-byte what it was."""
    app, window, store, engine, photo, _note = journeys
    before_bytes = photo.read_bytes()

    popped = _pop_out_first_result(qtbot, app, window, "harbour sunset")
    qtbot.waitUntil(lambda: popped.rotate.isEnabled() and _drawn_size(popped)[0] > 0,
                    timeout=5000)
    before = _drawn_size(popped)
    qtbot.mouseClick(popped.rotate, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: _drawn_size(popped) != before, timeout=5000)
    popped.close()
    gui_pump(app)

    again = _pop_out_first_result(qtbot, app, window, "harbour sunset")
    try:
        qtbot.waitUntil(lambda: _drawn_size(again)[0] > 0, timeout=5000)
        wide, high = _drawn_size(again)
        assert high > wide, "still turned a quarter, as it was left"
    finally:
        again.close()
        gui_pump(app)
    assert photo.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# pop-out: Ctrl+F finds the sentence in a real text document
# ---------------------------------------------------------------------------

def test_ctrl_f_in_a_popped_out_document_counts_and_highlights_the_word(journeys, qtbot):
    app, window, *_ = journeys
    popped = _pop_out_first_result(qtbot, app, window, "harbour survey quay")
    try:
        qtbot.waitUntil(lambda: "quay" in popped.text.toPlainText().lower(), timeout=5000)
        assert not popped.find.isVisible()

        popped.activateWindow()
        qtbot.keyClick(popped, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        gui_pump(app)
        assert popped.find.isVisible()

        qtbot.keyClicks(popped.find.box, "quay")
        gui_pump(app)
        assert popped.find.count.text().endswith("of 3"), popped.find.count.text()
        assert len(popped.text.extraSelections()) == 3

        # A word that is not in the document says so, rather than going quiet.
        popped.find.box.clear()
        qtbot.keyClicks(popped.find.box, "zebra")
        gui_pump(app)
        assert popped.find.count.text() == "no matches"
        assert popped.text.extraSelections() == []
    finally:
        popped.close()
        gui_pump(app)


def test_escape_closes_the_find_bar_and_takes_the_highlighting_off(journeys, qtbot):
    app, window, *_ = journeys
    popped = _pop_out_first_result(qtbot, app, window, "harbour survey quay")
    try:
        qtbot.waitUntil(lambda: "quay" in popped.text.toPlainText().lower(), timeout=5000)
        popped.activateWindow()
        qtbot.keyClick(popped, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        qtbot.keyClicks(popped.find.box, "quay")
        gui_pump(app)
        assert popped.text.extraSelections()

        qtbot.keyClick(popped.find.box, Qt.Key.Key_Escape)
        gui_pump(app)
        assert not popped.find.isVisible()
        assert popped.text.extraSelections() == []
    finally:
        popped.close()
        gui_pump(app)


# ---------------------------------------------------------------------------
# the eight-year-old scenarios (search-experience order, section 7), typed
# ---------------------------------------------------------------------------

def _wait_for_bar(qtbot, view, needle: str, timeout: int = 4000) -> None:
    qtbot.waitUntil(lambda: needle in _bar(view), timeout=timeout)


def test_search_is_the_page_she_lands_on_and_the_box_has_a_plain_example(journeys):
    r"""Section 2e: Search is the default tab on launch, and the box says what
    to type in words a child could copy."""
    app, window, *_ = journeys
    assert window.rail.tabText(window.rail.currentIndex()) == "Search"
    assert "Search everything" in window.search_view.input.placeholderText()


def test_she_types_a_misspelled_word_and_finds_her_essay_and_is_told_why(journeys, qtbot):
    r"""2a. "volcanno" is in nobody's index. The essay is on the page anyway,
    the box still says what she typed, and the bar says which word was used."""
    app, window, *_ = journeys
    view = window.search_view
    _type(qtbot, app, view, "volcanno")

    qtbot.waitUntil(lambda: "volcanoes.docx" in _names(view), timeout=4000)
    _wait_for_bar(qtbot, view, "also looked for 'volcano'")
    assert view.input.text() == "volcanno"


def test_she_asks_a_whole_question_with_a_spelling_mistake_in_it(journeys, qtbot):
    r"""The literal acceptance sentence: an eight-year-old finds her homework."""
    app, window, *_ = journeys
    view = window.search_view
    _type(qtbot, app, view, "my homework about volcannos")

    qtbot.waitUntil(lambda: gui_row_count(view.results) > 0, timeout=4000)
    qtbot.wait(700)
    assert _names(view)[0] == "volcanoes.docx"


def test_two_misspelled_words_are_not_dressed_up_as_a_correction(journeys, qtbot):
    r"""Two unknown words is the wrong corpus, not a typo: no "also looked
    for" claim, because the "corrected" query would find what the one working
    word found under a notice saying something was fixed."""
    app, window, *_ = journeys
    view = window.search_view
    _type(qtbot, app, view, "my homwork about volcannos")
    qtbot.wait(900)
    gui_pump(app)

    assert "also looked for" not in _bar(view)
    assert "Nothing here contains 'homwork' or 'volcannos'" in _bar(view)


def test_a_quoted_phrase_with_one_wrong_word_still_lands_and_says_so(journeys, qtbot):
    r"""2b. She quotes the phrase not quite right. The page is not empty, and
    the bar says the phrase was let go of."""
    app, window, *_ = journeys
    view = window.search_view
    _type(qtbot, app, view, '"my homework about big volcanoes"')

    qtbot.waitUntil(lambda: "volcanoes.docx" in _names(view), timeout=4000)
    _wait_for_bar(qtbot, view, "Nothing contains the exact phrase")


def test_a_filter_that_empties_the_page_is_let_go_of_and_named(journeys, qtbot):
    r"""2b again, with a typed filter: the essay is a .docx, she asked for a
    pdf. The essay is shown, and the bar names the filter that was dropped."""
    app, window, *_ = journeys
    view = window.search_view
    _type(qtbot, app, view, "volcanoes type:pdf")

    qtbot.waitUntil(lambda: "volcanoes.docx" in _names(view), timeout=4000)
    _wait_for_bar(qtbot, view, "these ignore it")
    # The filter she typed is still on screen as a chip she can remove.
    assert any("pdf" in label for label in view.chips.labels())


def test_a_sentence_with_a_known_name_applies_that_name_as_a_filter(journeys, qtbot):
    r"""3a, as the owner decided on 2026-09-27: recognised filters are
    *applied*, not only offered. "the email Dave sent about the school trip":
    Dave is a sender in the index and "email" means mail, so the search runs
    as `from dave.smith@acme.com` and mail, each shown as a chip - and the box
    still says exactly what she typed.

    Removing the person chip puts "Dave" back as a word and the filter
    returns to being an offer on the bar; clicking the offer adds the filter
    to the box, as it always did. (This scenario used to assert the offer
    alone - order 0c §3b's "chips, not rewrites", reversed by that decision.)
    """
    from PySide6.QtWidgets import QToolButton

    app, window, *_ = journeys
    view = window.search_view
    sentence = "the email Dave sent about the school trip"
    _type(qtbot, app, view, sentence)

    qtbot.waitUntil(lambda: "from dave.smith@acme.com" in view.chips.labels(), timeout=4000)
    qtbot.waitUntil(lambda: "dad-school-trip.eml" in _names(view), timeout=4000)
    assert "mail" in view.chips.labels()
    assert view.input.text() == sentence
    assert "apply:from:" not in _bar(view)          # applied, so not offered too

    person = next(button for button in view.chips.findChildren(QToolButton, "chip")
                  if button.text().startswith("from dave.smith@acme.com"))
    qtbot.mouseClick(person, Qt.MouseButton.LeftButton)
    gui_pump(app)
    assert "from dave.smith@acme.com" not in view.chips.labels()
    assert view.input.text() == sentence

    _wait_for_bar(qtbot, view, "dave.smith@acme.com")
    # A QLabel link cannot be hit by coordinate without its layout, so the
    # click is the label's own `linkActivated` - the signal Qt emits for one.
    view.notices.label.linkActivated.emit(_offer_href(view))
    gui_pump(app)
    assert view.input.text().startswith(sentence)
    assert "from:dave.smith@acme.com" in view.input.text()


def _offer_href(view) -> str:
    import re

    found = re.search(r'href="(apply:[^"]*dave\.smith@acme\.com[^"]*)"', view.notices.label.text())
    assert found, view.notices.label.text()
    return found.group(1)


def test_a_name_the_index_does_not_know_offers_nothing(journeys, qtbot):
    r"""Precision by construction: a wrong offer costs a click, so no offer
    appears for a name that is not in the index."""
    app, window, *_ = journeys
    view = window.search_view
    _type(qtbot, app, view, "the email Mortimer sent about the school trip")
    qtbot.wait(900)
    gui_pump(app)

    assert "apply:from:" not in view.notices.label.text()
    assert not any(label.startswith("from") for label in view.chips.labels())


def test_enter_on_the_selected_row_opens_her_document(journeys, qtbot, monkeypatch):
    r"""2e: Enter opens the document. Down to the row, Enter, and the window
    is asked to open that path."""
    app, window, *_ = journeys
    view = window.search_view
    opened: list = []
    # 2026-10-04: the window hands the row to the one open route.
    monkeypatch.setattr("app.ui.shell.open_row_async",
                        lambda store, row, reveal=False, **_k: opened.append((row.path, reveal)))

    _type(qtbot, app, view, "volcanoes")
    qtbot.waitUntil(lambda: gui_row_count(view.results) > 0, timeout=4000)
    qtbot.wait(700)

    qtbot.keyClick(view.input, Qt.Key.Key_Down)
    qtbot.keyClick(view.input, Qt.Key.Key_Return)
    gui_pump(app)

    assert opened and opened[-1][0].endswith("volcanoes.docx")
    assert opened[-1][1] is False


def test_clearing_the_box_offers_what_she_searched_for_last(journeys, qtbot):
    r"""2e: an empty, focused box offers her own recent searches, newest first."""
    app, window, *_ = journeys
    view = window.search_view
    _type(qtbot, app, view, "rivers")
    qtbot.waitUntil(lambda: gui_row_count(view.results) > 0, timeout=4000)
    qtbot.wait(700)

    qtbot.keyClick(view.input, Qt.Key.Key_Escape)
    gui_pump(app)
    assert view.input.text() == ""

    def offered() -> bool:
        from PySide6.QtWidgets import QToolButton

        view.saved.refresh()
        view.home.refresh()
        return any("rivers" in b.text()
                   for b in view.home.recent.findChildren(QToolButton))

    qtbot.waitUntil(offered, timeout=4000)


def test_the_idle_timer_runs_the_full_search_even_when_it_fires_early(journeys, monkeypatch):
    r"""Why the scenarios above failed about one run in two: Qt's default timers
    may fire up to 5% early, so the 400ms idle timer went off at 380-399ms,
    the measured gap read "not idle yet", and only the keyword glance ran -
    no spelling help, no notices, nothing in her recent searches. Here the
    timer fires the instant after her last key, which is as early as it gets.
    """
    import time

    app, window, *_ = journeys
    view = window.search_view
    tiers: list[str] = []
    monkeypatch.setattr(view, "_dispatch", tiers.append)
    view._interim_timer.stop()
    view._full_timer.stop()
    view.input.blockSignals(True)
    try:
        view.input.setText("rivers")
    finally:
        view.input.blockSignals(False)
    view._last_keystroke = time.monotonic()

    view._full_timer.timeout.emit()
    assert tiers == ["full"]


def test_every_scenario_here_ran_without_a_model(journeys):
    """The harness's embedder raises on any call; if any scenario above had
    needed a model it would have failed, not passed on a machine that has one."""
    *_, engine, _photo, _note = journeys
    with pytest.raises(RuntimeError):
        engine.embedder.embed("anything")
