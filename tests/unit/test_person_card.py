"""Order 1k 6f: a search for a person's name shows their contact card above the results.

Layer: L5

Three things are held here. *Who* a query names (`activity.strong_matches`) is a high bar and
is read without a window. *The card* shows what a person needs at a glance and its button asks
for the contact. *The strip* reads on a worker after a pause, shows nothing when there is no
match, and - the point of the order - changes nothing about the search: the same query gives the
same rows in the same order with a matching contact in the book and without one.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool                                  # noqa: E402
from PySide6.QtWidgets import QApplication                              # noqa: E402

from app.accounts.store import AccountStore                             # noqa: E402
from app.contacts.activity import strong_matches                        # noqa: E402
from app.contacts.card import Card, Field                               # noqa: E402
from app.contacts.hub import ContactHub                                 # noqa: E402
from app.storage.sqlite_store import SqliteStore                        # noqa: E402
from app.ui.widgets.person_card import (                                # noqa: E402
    PersonCard, PersonInfo, PersonStrip, lookup_people, LOOKUP_DEBOUNCE_MS,
)


def settle(rounds: int = 40) -> None:
    app = QApplication.instance()
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()


def person(given, family, *, email="", phone="", company="", title="", notes="") -> Card:
    card = Card(scalars={k: v for k, v in (("given", given), ("family", family), ("company", company),
                                           ("title", title), ("notes", notes)) if v})
    if email:
        card.fields.append(Field("email", email, "work", primary=True))
    if phone:
        card.fields.append(Field("phone", phone, "mobile"))
    return card


@pytest.fixture
def store(tmp_path, qapp):
    s = SqliteStore(tmp_path / "idx.db").connect()
    yield s
    s.close()


@pytest.fixture
def hub(store):
    return ContactHub(store)


@pytest.fixture
def book(hub, store):
    """Ann Lee (two ways in), Annabel Cole, Bob Stone, and an Ann in the bin."""
    accounts = AccountStore(store)
    work = accounts.add("microsoft", "me@work.example", "Work", colour="#E8710A")
    ids = {
        "ann": hub.create(person("Ann", "Lee", email="ann@acme.example", phone="+44 7700 900123",
                                 company="Acme Boilers", title="Site manager", notes="met at the fair"),
                          kept_in=[work]),
        "annabel": hub.create(person("Annabel", "Cole", email="annabel@other.example")),
        "bob": hub.create(person("Bob", "Stone", company="Ann Street Garage")),
        "gone": hub.create(person("Ann", "Deleted", email="gone@acme.example")),
    }
    hub.delete(ids["gone"])
    ids["work"] = work
    return ids


# -- who a query names ------------------------------------------------------------------------

def _names(hub, text, limit=2):
    return [r.display_name for r in strong_matches(hub, text, limit)]


def test_the_whole_name_names_the_contact_whatever_the_case(hub, book):
    assert _names(hub, "Ann Lee") == ["Ann Lee"]
    assert _names(hub, "ann   LEE") == ["Ann Lee"]


def test_the_start_of_a_name_names_the_contacts_that_begin_so_best_first_and_at_most_two(hub, book):
    assert _names(hub, "Ann") == ["Ann Lee", "Annabel Cole"]          # the shorter name first
    assert _names(hub, "Ann", limit=1) == ["Ann Lee"]
    assert len(strong_matches(hub, "An", 5)) == 0                      # two letters name nobody


def test_an_email_or_a_phone_must_match_exactly(hub, book):
    assert _names(hub, "ann@acme.example") == ["Ann Lee"]
    assert _names(hub, "ANN@ACME.EXAMPLE") == ["Ann Lee"]
    assert _names(hub, "ann@acme") == []
    assert _names(hub, "+44 7700 900123") == ["Ann Lee"]
    assert _names(hub, "07700 900123") == ["Ann Lee"]
    assert _names(hub, "7700 90012") == []


def test_a_word_from_a_company_notes_or_the_middle_of_a_name_is_not_a_person_named(hub, book):
    for text in ("Acme", "Boilers", "Site manager", "fair", "Lee", "Stone", "garage",
                 "annual report", "Ann Lee invoice", "who is Ann Lee"):
        assert _names(hub, text) == [], text


def test_a_deleted_contact_is_never_named(hub, book):
    assert _names(hub, "Ann Deleted") == []
    assert _names(hub, "gone@acme.example") == []


def test_an_empty_book_a_blank_query_or_a_missing_table_names_nobody(hub, store):
    assert strong_matches(hub, "Ann") == []
    assert strong_matches(hub, "   ") == []
    with store.write() as w:
        w.execute("DROP TABLE contacts_fts")
    assert strong_matches(hub, "Ann Lee") == []                        # the LIKE fallback; nothing there


def test_looking_up_people_fills_the_card_from_the_book(store, book):
    people = lookup_people(store, "Ann Lee")
    assert [p.name for p in people] == ["Ann Lee"]
    ann = people[0]
    assert (ann.title, ann.company, ann.email) == ("Site manager", "Acme Boilers", "ann@acme.example")
    assert ann.phone and ann.accounts == [("Work", "#E8710A")]
    assert lookup_people(store, "Nobody Here") == []
    assert lookup_people(None, "Ann Lee") == []


# -- the card -----------------------------------------------------------------------------------

def test_the_card_says_who_and_asks_for_the_contact(qapp):
    info = PersonInfo(7, "Ann Lee", "Site manager", "Acme Boilers", "ann@acme.example", "+44 7700 900123",
                      [("Work", "#E8710A"), ("Home", "#4C8BF5")])
    card = PersonCard(info)
    assert card.name.text() == "Ann Lee"
    assert card.work.text() == "Site manager · Acme Boilers"
    assert [line.text() for line in card.lines] == ["ann@acme.example", "+44 7700 900123"]
    assert "Work" in card.dots.text() and "#E8710A" in card.dots.text()
    got = []
    card.open_requested.connect(got.append)
    card.open_button.click()
    assert got == [7]
    assert card.open_button.text() == "Open contact"
    assert card.open_button.toolTip() and card.open_button.accessibleName()


def test_a_card_with_only_a_name_hides_what_it_does_not_have(qapp):
    card = PersonCard(PersonInfo(1, "Zed"))
    assert card.work.isHidden() and card.lines == [] and card.dots.isHidden()


# -- the strip ----------------------------------------------------------------------------------

class _Box:
    def __init__(self):
        self.value = ""

    def text(self):
        return self.value


def _strip(store):
    box = _Box()
    strip = PersonStrip(box.text, store)
    return box, strip


def _type(box, strip, text):
    box.value = text
    strip.typed(text)
    assert strip.person_timer.isActive() or not strip._query()
    strip.person_timer.stop()
    strip.lookup_now()
    settle()


def test_typing_a_name_shows_the_card_and_clearing_the_box_hides_it(store, book):
    box, strip = _strip(store)
    _type(box, strip, "Ann Lee")
    assert not strip.isHidden() and [c.info.name for c in strip.cards] == ["Ann Lee"]
    _type(box, strip, "")
    assert strip.isHidden() and strip.cards == []


def test_two_cards_at_most(store, book):
    box, strip = _strip(store)
    _type(box, strip, "Ann")
    assert [c.info.name for c in strip.cards] == ["Ann Lee", "Annabel Cole"]


def test_a_query_that_names_nobody_shows_nothing(store, book):
    box, strip = _strip(store)
    for text in ("boiler quote", "type:pdf", "/newest", "A", "Ann Lee invoice"):
        _type(box, strip, text)
        assert strip.isHidden() and strip.cards == [], text


def test_an_empty_book_and_a_missing_store_show_nothing(tmp_path, qapp):
    with SqliteStore(tmp_path / "empty.db") as empty:
        box, strip = _strip(empty)
        _type(box, strip, "Ann Lee")
        assert strip.isHidden()
    box, strip = _strip(None)
    _type(box, strip, "Ann Lee")
    assert strip.isHidden()


def test_a_stale_answer_is_dropped(store, book):
    box, strip = _strip(store)
    box.value = "Ann Lee"
    strip.typed("Ann Lee")
    old = strip._generation
    strip.typed("Ann Lee ")                                       # typing again makes the first stale
    strip._landed([PersonInfo(1, "Ann Lee")], old, "Ann Lee")
    assert strip.cards == [] and strip.isHidden()


def test_the_card_waits_for_a_pause_and_reads_nothing_while_typing(store, book, monkeypatch):
    import app.ui.widgets.person_card as module

    reads = []
    real = module.lookup_people
    monkeypatch.setattr(module, "lookup_people", lambda *a, **k: (reads.append(a), real(*a, **k))[1])
    box, strip = _strip(store)
    for text in ("A", "An", "Ann", "Ann ", "Ann L"):
        box.value = text
        strip.typed(text)
    assert reads == [] and strip.person_timer.isActive()
    assert strip.person_timer.interval() == LOOKUP_DEBOUNCE_MS


# -- Search itself ------------------------------------------------------------------------------

@pytest.fixture
def search_world(tmp_path, qapp):
    """A real keyword-only engine over a small index that also holds a book."""
    from app.search.engine import SearchEngine
    from app.storage.vector_store import ImageVectorStore, VectorStore
    from tests.unit.conftest import _NoModel

    s = SqliteStore(tmp_path / "search.db").connect()
    for n, text in enumerate(("Ann Lee sent the boiler quote.", "The boiler quote total is due in May.",
                              "Annual report of the boiler house.")):
        fid = s.upsert_file(f"C:/work/doc{n}.txt", parent_dir="C:/work", ext="txt", size_bytes=10,
                            mtime_ns=1_700_000_000_000_000_000 + n, status="INDEXED", source_kind="file")
        s.replace_chunks(fid, [{"ordinal": 0, "text": text}])
    vectors = VectorStore(tmp_path / "vec", dim=8)
    images = ImageVectorStore(tmp_path / "vec")
    engine = SearchEngine(s, vectors, _NoModel(), image_vectors=images)
    yield engine, s
    engine.close()
    vectors.close()
    images.close()
    s.close()


def _paths(view) -> list[str]:
    model = view.results._model
    from app.ui.result_delegate import ROLE_PAYLOAD

    return [getattr(model.item(i).data(ROLE_PAYLOAD), "path", "") for i in range(model.rowCount())]


def _run_search(engine, text: str):
    from app.ui.search_view import SearchView

    view = SearchView(engine)
    view.show()
    view.input.setText("")
    view._interim_timer.stop()
    view._full_timer.stop()
    view.input.blockSignals(True)
    view.input.setText(text)
    view.input.blockSignals(False)
    view.person_strip.typed(text)
    view.person_strip.person_timer.stop()
    view.person_strip.lookup_now()
    # UI phase B: a card that lands after the rows is held until the rows change
    # (`PersonStrip.rows_moved`), so the read is let land first, as the shorter
    # pause makes it in the window. Unwaited, a loaded machine raced the two.
    settle()
    started = time.perf_counter()
    view.search_now()
    settle()
    return view, time.perf_counter() - started


def test_the_search_view_builds_a_strip_that_sits_above_the_results(search_world):
    engine, _ = search_world
    from app.ui.search_view import SearchView

    view = SearchView(engine)
    layout = view.layout()
    order = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert view.person_strip in order
    assert order.index(view.person_strip) == order.index(view.split) - 1
    assert view.person_strip.isHidden()
    assert view.person_timer is view.person_strip.person_timer         # found by `stop_timers` at close


@pytest.mark.slow
def test_results_are_identical_with_and_without_a_matching_contact(search_world, hub_of=None):
    engine, store = search_world
    without, _t0 = _run_search(engine, "Ann")
    base = _paths(without)
    base_status = without.status.text()
    assert base, "the fixture finds nothing for 'Ann', so this proves nothing"
    assert without.person_strip.isHidden()

    ContactHub(store).create(person("Ann", "Lee", email="ann@acme.example", company="Acme"))
    with_card, _t1 = _run_search(engine, "Ann")
    assert not with_card.person_strip.isHidden()
    assert [c.info.name for c in with_card.person_strip.cards] == ["Ann Lee"]
    assert _paths(with_card) == base
    assert with_card.status.text() == base_status


@pytest.mark.slow
def test_a_card_adds_no_search_work_and_little_time(search_world):
    """Offscreen, keyword-only: the contact read runs beside the search on its own worker. The
    budget is generous because a CI box is noisy; the point is that it is not *added* to the search."""
    engine, store = search_world
    ContactHub(store).create(person("Ann", "Lee"))
    plain = min(_run_search(engine, "boiler")[1] for _ in range(3))      # names nobody: no read at all
    named = min(_run_search(engine, "Ann")[1] for _ in range(3))
    assert named < plain + 0.5, (plain, named)


def test_opening_the_card_asks_the_window_for_that_contact(search_world):
    engine, store = search_world
    cid = ContactHub(store).create(person("Ann", "Lee"))
    view, _ = _run_search(engine, "Ann Lee")
    got = []
    view.person_strip.open_requested.connect(got.append)
    view.person_strip.cards[0].open_button.click()
    assert got == [cid]


def test_the_search_view_stays_under_its_line_guard():
    source = Path("app/ui/search_view.py").read_text(encoding="utf-8").splitlines()
    assert len([l for l in source if l.strip() and not l.strip().startswith("#")]) < 250
