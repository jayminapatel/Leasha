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
