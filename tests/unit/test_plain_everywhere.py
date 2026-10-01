"""Plain English is read the same way on every tab.

Layer: L5 (presenter and worker tasks, no Qt).

**Owner, 1 October 2026:** *"the natural language search should be available on
all search items and should behave exactly same across the application as it
will get confusing"*. Before this, "mail about holiday from maya" applied
`type:mail` on the Search tab, did nothing on the Mail tab (the whole sentence
was ignored, with a note saying so) and searched for the literal words on Files
and Code. These tests pin the one reading all four now share.
"""

from __future__ import annotations

import pytest

from app.search.policy import CODE, FILES, MAIL, SEARCH, for_surface
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.rows import understood_line
from app.ui.presenter.search import auto_filters
from app.ui.tasks import browse_files_typed, browse_messages_typed, read_box
from tests.fixtures import chat_eval as fx


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    s = SqliteStore(tmp_path_factory.mktemp("plain") / "plain.db").connect()
    fx.load_into(s)
    yield s
    s.close()


SENTENCE = "mail about the licence renewal from chris"


def test_every_tab_reads_a_sentence_into_the_same_filters(store):
    expected = auto_filters(store, SENTENCE, for_surface(SEARCH))
    assert "type:mail" in expected[0] and "from:chris.yates@acme.com" in expected[0]
    for surface in (FILES, MAIL, CODE):
        parsed, applied = read_box(store, SENTENCE, surface=surface)
        assert tuple(applied) == tuple(expected[1]), surface
        assert parsed.senders == ("chris.yates@acme.com",), surface


def test_a_lower_case_name_after_from_is_read_as_a_person(store):
    """Nobody types capitals into a search box."""
    query, applied = auto_filters(store, "emails from chris", for_surface(SEARCH))
    assert "from:chris.yates@acme.com" in query
    assert [chosen.kind for chosen in applied] == ["type", "person"]


def test_a_lower_case_word_without_from_stays_a_word(store):
    query, applied = auto_filters(store, "budget chris", for_surface(SEARCH))
    assert query == "budget chris" and applied == ()


def test_the_mail_tab_narrows_by_what_the_message_says(store):
    page = browse_messages_typed(store, SENTENCE, limit=500)
    subjects = {row["subject"] for row in page["rows"]}
    assert subjects == {"Licence renewal quote"}
    assert page["words"] == "licence renewal"


def test_words_no_message_holds_leave_the_mail_tab_empty_not_ignored(store):
    page = browse_messages_typed(store, "mail about volcanoes from chris", limit=500)
    assert page["rows"] == []


def test_the_mail_tab_says_how_much_mail_the_index_holds(store):
    page = browse_messages_typed(store, "", limit=500)
    assert page["in_index"] == store.count_messages() > 0
    assert len(page["rows"]) == page["in_index"]


def test_status_filters_the_mail_tab(store):
    assert len(browse_messages_typed(store, "/status indexed", limit=500)["rows"]) == \
        store.count_messages()
    assert browse_messages_typed(store, "/status failed", limit=500)["rows"] == []


def test_status_filters_the_files_tab_the_same_way(store):
    indexed = browse_files_typed(store, "/status indexed", limit=500)
    assert indexed["rows"] and all(row["status"] == "INDEXED" for row in indexed["rows"])
    assert browse_files_typed(store, "/status skipped", limit=500)["rows"] == []


def test_the_files_tab_says_how_many_files_the_index_holds(store):
    page = browse_files_typed(store, "", limit=500)
    assert page["in_index"] == store.count_listed_files() == len(page["rows"])


def test_the_summary_says_what_the_box_was_read_as_and_the_index_size(store):
    page = browse_messages_typed(store, SENTENCE, limit=500)
    line = understood_line(page["applied"], page["words"], in_index=page["in_index"],
                           noun="messages")
    assert line.startswith("Read as: mail · from chris.yates@acme.com · "
                           "containing 'licence renewal'")
    assert line.endswith(f"{page['in_index']:,} messages in the index")


def test_nothing_read_says_only_the_index_size():
    assert understood_line((), "", in_index=1234, noun="files") == "1,234 files in the index"
    assert understood_line() == ""
