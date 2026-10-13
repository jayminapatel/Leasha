r"""Settings > Search > Your words, pressed for real.

Layer: L7

Order `smarter-answers` 4d. A word typed and added is in the table and in the
index; picked and removed, it is gone from both. Every write is on a worker.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

pytestmark = pytest.mark.gui


@pytest.mark.slow
def test_a_word_is_added_shown_and_removed(gui_mainwindow, qtbot):
    _app, window, store, _engine = gui_mainwindow
    box = window.settings_view.my_words
    for phrase, _meaning, _source in store.my_words():
        store.remove_my_word(phrase)
    box.set_store(store)
    qtbot.waitUntil(lambda: box.table.rowCount() == 0, timeout=3000)

    qtbot.keyClicks(box.phrase, "Mum")
    qtbot.keyClicks(box.meaning, "Jane Example")
    qtbot.mouseClick(box.add, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: box.words() == [("Mum", "Jane Example")], timeout=3000)
    assert store.my_words() == [("Mum", "Jane Example", "settings")]
    assert "now means Jane Example" in box.status.text()
    assert not box.remove.isEnabled()                    # nothing picked yet

    box.table.selectRow(0)
    assert box.remove.isEnabled() and box.phrase.text() == "Mum"
    qtbot.mouseClick(box.remove, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: box.table.rowCount() == 0, timeout=3000)
    assert store.my_words() == []


def test_an_empty_word_is_refused_without_writing(gui_mainwindow, qtbot):
    _app, window, store, _engine = gui_mainwindow
    box = window.settings_view.my_words
    box.phrase.clear()
    box.meaning.setText("Jane Example")
    qtbot.mouseClick(box.add, Qt.MouseButton.LeftButton)
    assert box.status.text() == "Type the word and what it means."
