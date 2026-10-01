r"""Search-experience 3b, at the presenter: the filters the rules recognise in a
sentence become click-to-apply offers on the notice bar.

The regression behind this file: `presenter.chips_for` was built and tested,
but nothing in the window ever called it, so the "plain sentence with a known
name produces the chip" scenario was true of the function and false of the
screen. `test_gui_scenarios_journeys.py` proves the screen; this pins the
logic that decides what the offer says.

Layer: L5 (presenter, no Qt).
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search.translate_rules import Chip
from app.ui.presenter import NOTICE_FILTER_OFFER, filter_offers, notice_line


def test_a_recognised_name_becomes_an_offer_that_applies_the_operator():
    offers = filter_offers([Chip("from", "dave.smith@acme.com", "Dave")],
                           "the email Dave sent about the school trip")
    assert [o.code for o in offers] == [NOTICE_FILTER_OFFER]
    assert 'href="apply:from:dave.smith@acme.com"' in offers[0].message
    assert "from dave.smith@acme.com" in offers[0].message


def test_a_filter_the_person_already_typed_is_not_offered_again():
    assert filter_offers([Chip("from", "dave.smith@acme.com", "Dave")],
                         "school trip from:dave") == []


def test_a_typed_date_answers_both_edges_so_neither_is_offered():
    """`date:` sets `after` and `before` both (order "dates" §1a), so a
    year chip beside it would offer a filter the box already has."""
    chips = [Chip("after", "2016-01-01", "2016"), Chip("before", "2016-12-31", "2016")]
    for typed in ("report 2016 date:2017", "report 2016 /date 2017"):
        assert filter_offers(chips, typed) == [], typed
    assert len(filter_offers(chips, "report 2016")) == 2


def test_no_chips_no_offers():
    assert filter_offers((), "volcanoes") == []
    assert filter_offers(None, "volcanoes") == []


def test_a_value_from_the_index_cannot_inject_markup_into_the_bar():
    offers = filter_offers([Chip("from", 'a"><b>x', "A")], "mail from A")
    assert "<b>" not in offers[0].message
    assert '"><b>' not in offers[0].message


def test_an_offer_reaches_the_bar_as_a_line():
    offers = filter_offers([Chip("type", "pdf", "report")], "the report")
    assert "pdf" in notice_line(offers)


@pytest.fixture()
def store():
    from app.storage.sqlite_store import SqliteStore

    s = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "offers.db").connect()
    file_id = s.upsert_file("C:/Mail/trip.eml", parent_dir="C:/Mail", ext="eml",
                            size_bytes=1, mtime_ns=1, status="INDEXED",
                            source_kind="eml")
    s.replace_chunks(file_id, [{"ordinal": 0, "text": "the trip is on the fifteenth"}])
    s.set_message(file_id, subject="School trip", sender="dave.smith@acme.com",
                  recipients='["me@acme.com"]', has_attach=0)
    yield s
    s.close()


def test_the_worker_body_finds_the_known_sender_and_only_the_known_one(store):
    from app.ui.presenter import filter_offer_notices

    found = filter_offer_notices(store, "the email Dave sent about the school trip")
    assert any("dave.smith@acme.com" in o.message for o in found)
    # "email" is a kind word, so a type offer may appear - but nobody is named.
    unknown = filter_offer_notices(store, "the email Mortimer sent about the trip")
    assert not [o for o in unknown if "apply:from:" in o.message]


def test_the_search_surface_switching_auto_chips_off_removes_the_offers(store):
    from app.ui.presenter import filter_offer_notices

    off = {"auto_chips": False}
    assert filter_offer_notices(store, "the email Dave sent about the school trip", off) == []


def test_a_broken_store_yields_no_offers_rather_than_raising():
    from app.ui.presenter import filter_offer_notices

    class Broken:
        def distinct_values(self, *_a, **_k):
            raise RuntimeError("locked")

    # No index to check a kind word against, so a type offer may still appear;
    # what must not appear is a person, and nothing may raise.
    found = filter_offer_notices(Broken(), "the email Dave sent")
    assert not [o for o in found if "apply:from:" in o.message]


# ---------------------------------------------------------------------------
# Applied, not only offered - owner decision 2026-09-27 ("mail from 2017")
# ---------------------------------------------------------------------------

def test_the_search_tab_applies_what_it_recognises(store):
    from app.search.policy import SEARCH, for_surface
    from app.ui.presenter import auto_filters

    query, applied = auto_filters(store, "mail from 2017", for_surface(SEARCH))
    assert query == "type:mail after:2017-01-01 before:2017-12-31"
    assert [a.label for a in applied] == ["mail", "in 2017"]


def test_a_surface_with_auto_chips_off_runs_the_words_as_typed(store):
    from dataclasses import replace

    from app.search.policy import CODE, for_surface
    from app.ui.presenter import auto_filters

    # 1 October 2026, owner: plain English is read the same way on every tab; the switch
    # is still honoured when somebody turns it off.
    off = replace(for_surface(CODE), auto_chips=False)
    assert auto_filters(store, "mail from 2017", off) == ("mail from 2017", ())


def test_a_broken_store_costs_the_person_filter_not_the_search():
    from app.ui.presenter import auto_filters

    class Broken:
        def distinct_values(self, *_a, **_k):
            raise RuntimeError("locked")

    query, _ = auto_filters(Broken(), "mail from Dave in 2017")
    assert "from:" not in query and "type:mail" in query and "Dave" in query


def test_declines_travel_only_when_the_policy_applies_filters():
    from app.ui.presenter import Tier, search_options

    on = search_options(Tier.FULL, scope="all", rerank=False, surface="search",
                        declined={("date", "from 2017")})
    assert on["declined"] == (("date", "from 2017"),)
    off = search_options(Tier.FULL, scope="all", rerank=False, surface="search",
                         preferences={"auto_chips": False}, declined=set())
    assert "declined" not in off
    # A caller that says nothing about declines gets exactly what it got before.
    assert "declined" not in search_options(Tier.INTERIM, scope="all", rerank=False)


def test_an_applied_filter_is_not_offered_again(store):
    """"Only show results in 2017?" beside a page already limited to 2017 is
    noise; the offer returns once the chip is removed."""
    from app.ui.presenter import auto_filters, filter_offer_notices

    sentence = "the email Dave sent about the school trip"
    _, applied = auto_filters(store, sentence)
    assert any(a.kind == "person" for a in applied)
    offers = filter_offer_notices(store, sentence, None, applied)
    assert not [o for o in offers if "apply:from:" in o.message]
    assert not [o for o in offers if "apply:type:" in o.message]
    declined = tuple(a for a in applied if a.kind != "person")
    offers = filter_offer_notices(store, sentence, None, declined)
    assert [o for o in offers if "apply:from:dave.smith@acme.com" in o.message]


def test_the_search_worker_runs_the_applied_query_and_carries_the_chips(store):
    """**The wiring.** The worker - not the interface thread, because reading
    the sentence asks the store - rewrites the query, keeps the `declined` key
    away from the engine (it has no such argument, and must not), and puts
    the applied filters on the response for the chip row."""
    pytest.importorskip("PyQt6")
    from app.search.engine import SearchResponse
    from app.search.policy import SEARCH, for_surface
    from app.ui.workers import SearchWorker

    class Engine:
        def __init__(self):
            self.store, self.calls = store, []

        def search(self, raw, **options):
            self.calls.append(("search", raw, options))
            return SearchResponse()

        def interim(self, raw, **options):
            self.calls.append(("interim", raw, options))
            return SearchResponse()

    for tier in ("interim", "full"):
        engine = Engine()
        worker = SearchWorker(engine, "mail from 2017", tier=tier, generation=1,
                              scope="all", policy=for_surface(SEARCH), declined=())
        landed = []
        worker.signals.finished.connect(landed.append)
        worker.run()
        _, raw, options = engine.calls[0]
        assert raw == "type:mail after:2017-01-01 before:2017-12-31"
        assert "declined" not in options
        assert [a.label for a in landed[0][1].applied] == ["mail", "in 2017"]

    engine = Engine()                               # no declines key: untouched
    SearchWorker(engine, "mail from 2017", tier="full", generation=1).run()
    assert engine.calls[0][1] == "mail from 2017"
