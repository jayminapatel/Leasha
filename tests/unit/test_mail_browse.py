"""Mail as a table: the query, and the formatting.

Layer: L1 and L5

A mailbox is scanned in columns and read newest first. Ranking one by relevance
puts an eight-year-old thread above this morning's, which is why this is a
browser over `messages` rather than a search over `chunks_fts`.

The formatting half runs over every row of a mailbox that may hold two hundred
thousand messages written by a decade of different clients. One malformed field
must cost that row its column, never the table.
"""

from __future__ import annotations

import json
import time

import pytest

from app.storage.sqlite_store import SqliteStore
from app.ui.presenter import (
    format_address,
    format_recipients,
    format_sent,
    mail_filters,
    mail_rows,
)

DAY = 86_400


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def add(store, *, path, sender="", to=(), subject="", sent_at=0,
        attach=False, size=1000):
    file_id = store.upsert_file(
        path=path, size_bytes=size, mtime_ns=sent_at * 1_000_000_000 or 1,
        source_kind="pst_message",
    )
    with store.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(file_id, subject, sender, recipients, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, subject, sender, json.dumps(list(to)), sent_at, int(attach)),
        )
    return file_id


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------

def test_newest_first(store):
    add(store, path="a", subject="old", sent_at=1000)
    add(store, path="b", subject="new", sent_at=9000)
    assert [r["subject"] for r in store.browse_messages()] == ["new", "old"]


def test_from_matches_part_of_an_address(store):
    """`/from dave` must find `dave.smith@acme.com`. Exact `IN` matching was a
    real bug here, and it made the filter look broken to anyone who did not
    know the full address by heart."""
    add(store, path="a", sender="dave.smith@acme.com", subject="hit")
    add(store, path="b", sender="priya@acme.com", subject="miss")
    assert [r["subject"] for r in store.browse_messages(sender="dave")] == ["hit"]


def test_to_matches_inside_the_recipient_list(store):
    add(store, path="a", to=["priya@acme.com", "sam@acme.com"], subject="hit")
    add(store, path="b", to=["dave@acme.com"], subject="miss")
    assert [r["subject"] for r in store.browse_messages(recipient="priya")] == ["hit"]


def test_subject_is_a_substring_too(store):
    add(store, path="a", subject="Q3 invoice draft")
    assert store.browse_messages(subject="invoice")


def test_attachment_yes_and_no_are_different_questions(store):
    add(store, path="a", subject="with", attach=True)
    add(store, path="b", subject="without", attach=False)
    assert [r["subject"] for r in store.browse_messages(has_attachment=True)] == ["with"]
    assert [r["subject"] for r in store.browse_messages(has_attachment=False)] == ["without"]
    assert len(store.browse_messages()) == 2, "not asking is a third state"


def test_a_date_range_is_inclusive_at_the_start_and_exclusive_at_the_end(store):
    add(store, path="a", subject="before", sent_at=999)
    add(store, path="b", subject="on", sent_at=1000)
    add(store, path="c", subject="after", sent_at=2000)
    found = [r["subject"] for r in store.browse_messages(after=1000, before=2000)]
    assert found == ["on"]


def test_filters_combine(store):
    add(store, path="a", sender="dave@x", subject="yes", attach=True)
    add(store, path="b", sender="dave@x", subject="no", attach=False)
    add(store, path="c", sender="sam@x", subject="no", attach=True)
    found = store.browse_messages(sender="dave", has_attachment=True)
    assert [r["subject"] for r in found] == ["yes"]


def test_a_percent_sign_is_searched_for_not_treated_as_a_wildcard(store):
    """Otherwise `/subject 100%` matches every message in the mailbox, which
    looks like the filter being ignored."""
    add(store, path="a", subject="100% off")
    add(store, path="b", subject="nothing like it")
    assert len(store.browse_messages(subject="100%")) == 1


def test_an_underscore_is_literal_too(store):
    add(store, path="a", subject="report_final")
    add(store, path="b", subject="reportXfinal")
    assert len(store.browse_messages(subject="report_final")) == 1


def test_the_limit_is_honoured(store):
    for index in range(10):
        add(store, path=f"m{index}", sent_at=index)
    assert len(store.browse_messages(limit=3)) == 3


def test_messages_without_a_date_sort_last_rather_than_first(store):
    """A NULL date sorting to the top would put every unparseable message above
    everything real, which is the opposite of useful."""
    add(store, path="a", subject="dated", sent_at=5000)
    add(store, path="b", subject="undated", sent_at=0)
    assert [r["subject"] for r in store.browse_messages()][0] == "dated"


def test_counting_is_separate_from_listing(store):
    add(store, path="a")
    add(store, path="b")
    assert store.count_messages() == 2


# ---------------------------------------------------------------------------
# The formatting
# ---------------------------------------------------------------------------

def test_a_display_name_beats_an_address():
    """In a column of thirty rows the name distinguishes them; the domain is
    usually the same for all thirty."""
    assert format_address('"Dave Smith" <dave@acme.com>') == "Dave Smith"


def test_a_bare_address_is_left_alone():
    assert format_address("dave@acme.com") == "dave@acme.com"


def test_an_address_with_no_name_part_keeps_the_address():
    assert format_address("<dave@acme.com>") == "<dave@acme.com>"


def test_recipients_are_summarised_rather_than_flooding_the_column():
    value = json.dumps([f"p{i}@x.com" for i in range(9)])
    assert format_recipients(value).endswith("+7")


def test_two_recipients_are_both_named():
    assert format_recipients(json.dumps(["a@x", "b@x"])) == "a@x, b@x"


def test_malformed_recipients_cost_the_row_its_column_not_the_table():
    """Ten years of mail clients wrote this field. One of them wrote something
    that is not JSON, and that message must still appear in the list."""
    assert format_recipients("[not json at all") == "[not json at all"
    assert format_recipients(None) == ""
    assert format_recipients(12345) == "12345"


def test_a_recent_date_shows_the_time_and_an_old_one_shows_the_year():
    """A column is scanned for a boundary between March and April. "5 weeks
    ago" reads well for one file and badly down a sorted column."""
    now = time.time()
    recent = format_sent(now - 2 * DAY, now=now)
    old = format_sent(now - 800 * DAY, now=now)
    assert ":" in recent, "recent mail needs the time of day to separate a busy afternoon"
    assert ":" not in old and len(old.split()) == 3, "old mail needs the year"


def test_a_missing_date_is_blank_not_1970():
    assert format_sent(0) == ""
    assert format_sent(None) == ""
    assert format_sent("rubbish") == ""


def test_an_empty_subject_says_so():
    """Blank looks like a rendering fault. Saying so does not - and an empty
    subject is both common and meaningful."""
    assert mail_rows([{"subject": ""}])[0].subject == "(no subject)"


def test_rows_carry_sortable_originals_as_well_as_formatted_text():
    """"3 KB" and "10 KB" sort the wrong way as text, and a date column sorted
    alphabetically is worse than one that does not sort at all."""
    row = mail_rows([{"sent_at": 1700000000, "size_bytes": 2048}])[0]
    assert row.sent_at == 1700000000
    assert row.size_bytes == 2048
    assert row.size != "2048"


def test_a_row_with_nothing_in_it_does_not_raise():
    assert mail_rows([{}])[0].sender == ""


# ---------------------------------------------------------------------------
# The `/` commands, reused rather than reinvented
# ---------------------------------------------------------------------------

def parsed(text):
    from app.search.commands import expand_slashes
    from app.search.query import parse_query
    return parse_query(expand_slashes(text))


def test_slash_commands_become_store_filters():
    """The whole reason the Mail tab could reuse the search box's dropdown: the
    parser already produces exactly the columns `messages` has."""
    filters = mail_filters(parsed("/from dave /to priya /subject invoice"))
    assert filters == {"sender": "dave", "recipient": "priya", "subject": "invoice"}


def test_has_attachment_travels_through():
    assert mail_filters(parsed("/has attachment"))["has_attachment"] is True


def test_dates_become_epoch_seconds():
    filters = mail_filters(parsed("/after 2024-01-01"))
    assert isinstance(filters["after"], int) and filters["after"] > 1_600_000_000


def test_an_absent_filter_is_absent_rather_than_none():
    """`browse_messages` treats None as "did not ask"; passing it explicitly
    would work, but a dict of Nones hides which filters are real."""
    assert mail_filters(parsed("")) == {}


def test_free_text_is_not_smuggled_in_as_a_filter():
    """This tab never touches chunk text. Accepting words here would produce an
    empty table for a query that looks perfectly reasonable - the view says so
    instead."""
    assert mail_filters(parsed("quarterly report")) == {}


# ---------------------------------------------------------------------------
# Mail metadata for a page of search results
# ---------------------------------------------------------------------------

def test_messages_for_returns_metadata_by_file_id(store):
    first = add(store, path="a", subject="Licence renewal", sender="chris@acme.com")
    add(store, path="b", subject="Other")
    found = store.messages_for([first])
    assert found[first]["subject"] == "Licence renewal"


def test_messages_for_omits_files_that_are_not_messages(store):
    """Most search results are documents. A file with no `messages` row must be
    absent rather than an error."""
    message = add(store, path="a", subject="Hi")
    document = store.upsert_file(path="doc.pdf", size_bytes=1, mtime_ns=1,
                                 source_kind="file")
    found = store.messages_for([message, document])
    assert set(found) == {message}


def statements(store):
    """Count SQL actually sent to SQLite.

    `sqlite3.Connection.execute` cannot be monkeypatched - it is read-only - and
    counting calls to a wrapper would prove nothing about what reached the
    database anyway. `set_trace_callback` is the real thing.
    """
    seen: list[str] = []
    store.conn.set_trace_callback(seen.append)
    return seen


def test_messages_for_is_one_query_for_the_whole_page(store):
    """**The number that matters.** The search box runs on a debounce, so a
    per-row lookup over ten results is ten queries per keystroke - fifty at the
    fetch depth grouping needs. That is the shape of slowness that gets blamed
    on the search itself."""
    ids = [add(store, path=f"m{i}", subject=f"s{i}") for i in range(10)]

    seen = statements(store)
    try:
        store.messages_for(ids)
    finally:
        store.conn.set_trace_callback(None)

    assert len(seen) == 1, f"expected one query for ten results, made {len(seen)}"


def test_messages_for_an_empty_page_asks_nothing(store):
    """A page of pure documents must not cost a round trip at all."""
    seen = statements(store)
    try:
        assert store.messages_for([]) == {}
    finally:
        store.conn.set_trace_callback(None)
    assert seen == []
