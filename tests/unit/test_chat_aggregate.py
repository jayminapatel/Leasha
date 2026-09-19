"""Counting is a database question: the number shown is the query's result, always.

Layer: L8b. Work order `202626270611-chat-tab` sections 1d, 4e-4 and 5 (*"aggregate: the
number shown equals the query result, always (property test over generated corpora)"*).
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from hypothesis import given, settings, strategies as st

from app.chat.aggregate import SCOPE_SENTENCE, parse_aggregate, run_aggregate
from app.storage.sqlite_store import SqliteStore
from tests.fixtures import chat_eval as fx
from tests.unit.chat_env import Env


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("agg"))
    yield e
    e.close()


def _stated(text: str) -> int:
    """The count an answer states: its first number that is not a size."""
    for match in re.finditer(r"\d[\d,]*", text):
        if text[match.end():match.end() + 3].strip().startswith(("MB", "KB", "GB", ".")):
            continue
        return int(match.group(0).replace(",", ""))
    raise AssertionError(f"no number in {text!r}")


AGGREGATE_QUESTIONS = [q for q in fx.QUESTIONS if q.outcome == "aggregate"]


@pytest.mark.parametrize("qa", AGGREGATE_QUESTIONS, ids=lambda q: q.id)
def test_the_number_is_the_query_result_and_matches_plain_python(env, qa):
    """Two derivations of the same figure: SQL over the index, and a list
    comprehension over the fixture's own records."""
    spec = parse_aggregate(qa.question, env.store, today=fx.TODAY)
    result = run_aggregate(env.store, spec)
    assert result.count == qa.count, (qa.question, spec.summary())
    assert _stated(result.text) == result.count            # the number IS the result
    for expected in qa.expect_all:
        assert expected.lower() in result.text.lower()


@pytest.mark.parametrize("qa", AGGREGATE_QUESTIONS, ids=lambda q: q.id)
def test_every_aggregate_answer_states_its_scope_in_the_same_breath(env, qa):
    """Work order 4e-4."""
    result = run_aggregate(env.store, parse_aggregate(qa.question, env.store, today=fx.TODAY))
    assert SCOPE_SENTENCE in result.text
    assert result.text.index(SCOPE_SENTENCE) < 200


def test_a_list_says_when_it_is_complete_and_when_it_is_not(env):
    everything = run_aggregate(
        env.store, parse_aggregate("List the emails from Priya", env.store), limit=50)
    assert "That's all 1." in everything.text and len(everything.rows) == 1
    truncated = run_aggregate(
        env.store, parse_aggregate("List the emails from Dave", env.store), limit=3)
    assert "Here are the newest 3" in truncated.text and truncated.count == 6


def test_a_count_of_zero_is_an_answer_and_says_what_zero_means(env):
    spec = parse_aggregate("How many PDFs from 2011 are there?", env.store, today=fx.TODAY)
    result = run_aggregate(env.store, spec)
    assert result.count == 0 and result.text.startswith("0 ")
    assert any("not scanned" in n or "has not been scanned" in n for n in result.notes)


def test_a_type_is_only_assumed_when_the_person_named_one(env):
    """"invoices" are things to find in the text, not a guess at `pdf`."""
    spec = parse_aggregate("How many invoices are there?", env.store)
    assert spec.parsed.ext == () and "invoice" in spec.terms


def test_a_slow_count_is_abandoned_rather_than_holding_the_worker(env, monkeypatch):
    import app.chat.aggregate as aggregate

    monkeypatch.setattr(aggregate, "QUERY_DEADLINE_S", -1.0)
    monkeypatch.setattr(aggregate, "PROGRESS_STEPS", 1)
    spec = parse_aggregate("How many photos do I have?", env.store)
    with pytest.raises(TimeoutError):
        run_aggregate(env.store, spec)


def test_a_person_the_index_does_not_know_is_not_invented(env):
    spec = parse_aggregate("How many emails did Zebediah send?", env.store)
    assert spec.parsed.senders == ()


# --------------------------------------------------------------------------- the property

_EXTS = ["pdf", "docx", "xlsx", "jpg", "txt"]
_FIRST = ["dave", "priya", "chris", "helen", "tomasz"]

_records = st.lists(
    st.tuples(st.sampled_from(_EXTS + ["eml"]), st.sampled_from(_FIRST),
              st.integers(2018, 2025), st.booleans()),
    min_size=0, max_size=30)


@settings(max_examples=25, deadline=None)
@given(records=_records, who=st.sampled_from(_FIRST), year=st.integers(2018, 2025))
def test_over_generated_corpora_the_number_always_equals_the_count(tmp_path_factory, records, who, year):
    """For any corpus, "how many emails did X send in Y" states exactly the number of
    such rows in it, and "how many PDFs" the number of PDFs."""
    folder = tmp_path_factory.mktemp("prop")
    import json
    from datetime import datetime, time as clock

    with SqliteStore(folder / "p.db") as store:
        for index, (ext, first, y, attach) in enumerate(records):
            mail = ext == "eml"
            stamp = int(datetime.combine(date(y, 6, 15), clock(12)).timestamp() * 1e9)
            file_id = store.upsert_file(
                path=f"C:/p/{index}.{ext}", parent_dir="C:/p", ext=ext, size_bytes=10 + index,
                mtime_ns=stamp, content_hash=str(index), status="INDEXED",
                source_kind="eml" if mail else "file")
            store.replace_chunks(file_id, [{"text": f"body {index}", "ordinal": 0}])
            if mail:
                store.set_message(file_id, subject="s", sender=f"{first}.x@acme.com",
                                  recipients=json.dumps(["me@acme.com"]),
                                  has_attach=1 if attach else 0)
        # Every sender must be known to the index or the name is (rightly) not a filter.
        senders = {first for ext, first, _y, _a in records if ext == "eml"}
        expected_mail = sum(1 for ext, first, y, _a in records
                            if ext == "eml" and first == who and y == year)
        expected_pdf = sum(1 for ext, *_ in records if ext == "pdf")

        pdfs = run_aggregate(store, parse_aggregate("How many PDFs do I have?", store))
        assert pdfs.count == expected_pdf == _stated(pdfs.text)

        if who in senders:
            question = f"How many emails did {who.capitalize()} send in {year}?"
            result = run_aggregate(store, parse_aggregate(question, store,
                                                          today=date(2026, 1, 1)))
            assert result.count == expected_mail == _stated(result.text)
