"""Files, Mail and Code show what a sentence was read as, as removable chips.

Layer: L5 (Qt, offscreen).

**Owner, 1 October 2026:** plain English *"should behave exactly same across
the application"*. The Search tab drew each filter it read from a sentence as a
chip that could be removed; the other tabs said "Read as: ..." in a line of
text that could not be acted on. These pin the shared chip row
(`widgets.chips.list_chips`) on the list tabs.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QToolButton

from app.storage.sqlite_store import SqliteStore
from tests.fixtures import chat_eval as fx
from tests.unit.test_status_column import _pump


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "chips.db").connect()
    fx.load_into(s)
    yield s
    s.close()


def _chips(view) -> list[QToolButton]:
    return [b for b in view.chips.findChildren(QToolButton) if b.isVisibleTo(view.chips)]


def _type(view, text: str) -> None:
    view.input.setText(text)
    view._run()
    _pump()


def test_the_mail_tab_draws_what_it_read_as_chips(qapp, store) -> None:
    from app.ui.mail_view import MailView

    view = MailView(store)
    try:
        _type(view, "emails from chris")
        assert view.chips.labels() == ["mail", "from chris.yates@acme.com"]
        assert {r.sender for r in view._rows} == {"chris.yates@acme.com"}
        assert "Read as" not in view.summary.text()       # the chips say it now
    finally:
        view.shutdown()


def test_removing_a_chip_searches_for_those_words_instead(qapp, store) -> None:
    from app.ui.mail_view import MailView

    view = MailView(store)
    try:
        _type(view, "emails from chris")
        person = next(b for b in _chips(view) if "chris" in b.text())
        person.click()
        _pump()
        assert view.input.text() == "emails from chris"    # the box is never altered
        assert view.chips.labels() == ["mail"]
        assert ("person", "from chris") in view.chips.declined
    finally:
        view.shutdown()


def test_the_files_and_code_tabs_have_the_same_row(qapp, store) -> None:
    from app.ui.code_view import CodeView
    from app.ui.files_view import FilesView
    from app.ui.widgets.chips import ChipRow

    for make in (FilesView, CodeView):
        view = make(store)
        try:
            assert isinstance(view.chips, ChipRow)
        finally:
            view.shutdown()
    QApplication.processEvents()
