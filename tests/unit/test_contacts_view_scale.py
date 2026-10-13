"""Order 1k 6b, rule 9: ten thousand contacts without a stall - measured, offscreen.

Layer: L5

A hub seeded with 10,000 contacts (a few accounts, groups, tags), a real page on the real thread
pool. The rows are read on a worker, so what matters to the window is the work its own thread does
when they land and when the views paint: those are the numbers below, each with a bound three to
ten times what the machine this was written on needs, so a slow runner passes and a regression
(a per-row query, a per-row file read, an O(n^2) sort) does not.

Offscreen numbers are not Windows numbers: the real window at 125% scaling on the owner's laptop
is UNVERIFIED here.
"""

from __future__ import annotations

import os
import time
import types

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QElapsedTimer, QTimer, Qt                    # noqa: E402
from PySide6.QtWidgets import QApplication                               # noqa: E402

from app.accounts.store import AccountStore                              # noqa: E402
from app.contacts.card import Card, Field                                # noqa: E402
from app.contacts.hub import ContactFilter, ContactHub                   # noqa: E402
from app.storage.sqlite_store import SqliteStore                         # noqa: E402
from app.ui.contacts import loader                                       # noqa: E402
from app.ui.contacts.page import ContactsPage                            # noqa: E402

from tests.unit.test_contacts_view_world import settle                   # noqa: E402

N = 10_000
NUMBERS: dict[str, float] = {}


def _seed(hub: ContactHub, store, accounts: list[int]) -> None:
    with store.batch():                                  # one transaction: seeding is not what is measured
        for i in range(N):
            c = Card(scalars={"given": f"Given{i}", "family": f"Family{i % 977}",
                              "company": f"Company {i % 200}", "title": "Engineer"})
            c.fields = [Field("email", f"p{i}@c{i % 200}.example", "work", primary=True),
                        Field("phone", f"+44 7700 {900000 + i}", "mobile")]
            if i % 3 == 0:
                c.groups = ["Family"]
            hub.create(c, kept_in=accounts[: 1 + i % 3])
    ids = [r[0] for r in store.conn.execute("SELECT id FROM contacts WHERE id % 7 = 0")]
    hub.tag(ids, "VIP")


@pytest.fixture(scope="module")
def big(tmp_path_factory, qapp):
    tmp = tmp_path_factory.mktemp("ten_thousand")
    store = SqliteStore(tmp / "contacts.db").connect()
    accounts = AccountStore(store)
    ids = [accounts.add("google", f"a{i}@x.example", f"Account {i}") for i in range(3)]
    hub = ContactHub(store)
    _seed(hub, store, ids)
    yield types.SimpleNamespace(store=store, hub=hub, accounts=accounts, page=None)
    store.close()


def _ms(since: float) -> float:
    return (time.perf_counter() - since) * 1000


@pytest.mark.slow
def test_ten_thousand_rows_are_read_on_a_worker_and_land_without_a_stall(big):
    store, hub, accounts = big.store, big.hub, big.accounts
    assert hub.count() == N
    # What the worker does (off the window's thread).
    t0 = time.perf_counter()
    snap = loader.load_snapshot(hub, accounts, ContactFilter(), full=True)
    NUMBERS["worker: read 10,000 rows + sidebar numbers (ms)"] = _ms(t0)
    assert len(snap.rows) == N and snap.counts["all"] == N

    page = ContactsPage(store, hub, accounts)
    page.resize(1200, 760)
    page.show()
    for _ in range(5):                                   # first layout and polish are not the load
        QApplication.instance().processEvents()
    # Heartbeat: a timer that should tick every 10 ms. The longest gap between ticks while the page
    # loads is the longest the window's thread was unavailable - the stall a person would feel.
    gaps: list[float] = []
    clock = QElapsedTimer()
    clock.start()
    last = [clock.elapsed()]

    def tick() -> None:
        now = clock.elapsed()
        gaps.append(now - last[0])
        last[0] = now

    beat = QTimer()
    beat.setInterval(10)
    beat.timeout.connect(tick)
    beat.start()
    landed: list[float] = []
    original = page._landed

    def timed(*a, **k):
        t = time.perf_counter()
        original(*a, **k)
        landed.append(_ms(t))
    page._landed = timed
    t0 = time.perf_counter()
    page.refresh()
    while page._busy and _ms(t0) < 20_000:               # the event loop keeps running while the worker reads
        QApplication.instance().processEvents()
        time.sleep(0.002)                                  # releases the GIL, as the real loop does
    NUMBERS["page: refresh() to rows on screen, wall (ms)"] = _ms(t0)
    beat.stop()
    settle(page)
    assert page.model.rowCount() == N
    NUMBERS["window thread: landing the rows (model + sidebar + editor setup) (ms)"] = max(landed)
    NUMBERS["window thread: longest gap between 10 ms ticks while loading (ms)"] = max(gaps)
    assert len(gaps) > 3, "the heartbeat never ran: nothing was measured"
    assert max(landed) < 500, landed
    assert max(gaps) < 500, max(gaps)
    big.page = page


def test_both_views_paint_and_scroll_ten_thousand_rows(big):
    page = big.page
    page.set_view("list", remember=False)
    t0 = time.perf_counter()
    page.table.grab()
    NUMBERS["list: first paint of the visible rows (ms)"] = _ms(t0)
    t0 = time.perf_counter()
    page.table.scrollToBottom()
    page.table.grab()
    NUMBERS["list: scroll to the last row and paint (ms)"] = _ms(t0)
    assert _ms(t0) < 1500
    page.set_view("cards", remember=False)
    page.show()
    QApplication.instance().processEvents()
    t0 = time.perf_counter()
    page.cards.grab()
    NUMBERS["cards: first paint of the visible cards (ms)"] = _ms(t0)
    t0 = time.perf_counter()
    page.cards.scrollToBottom()
    page.cards.grab()
    NUMBERS["cards: scroll to the last card and paint (ms)"] = _ms(t0)
    assert _ms(t0) < 2500
    page.set_view("list", remember=False)


def test_sorting_and_selecting_everything_are_arithmetic(big):
    page = big.page
    t0 = time.perf_counter()
    page.table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    NUMBERS["list: sort by company (ms)"] = _ms(t0)
    assert _ms(t0) < 1000
    assert page.model.row_at(0).company == "Company 99"
    page.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    t0 = time.perf_counter()
    page.table.selectAll()
    ids = page.selected_ids()
    NUMBERS["list: pick all 10,000 and read them back (ms)"] = _ms(t0)
    assert len(ids) == N and _ms(t0) < 1000
    t0 = time.perf_counter()
    page.bulk.set_rows([page.model.row_for_id(i) for i in ids])
    NUMBERS["bulk bar: take 10,000 picked rows (ms)"] = _ms(t0)
    assert _ms(t0) < 1000
    page.table.clearSelection()


@pytest.mark.slow
def test_narrowing_ten_thousand_is_a_worker_read(big):
    page = big.page
    page.toolbar.filter.setText("Given42")
    page._timer.stop()
    t0 = time.perf_counter()
    page.reload(full=False)
    settle(page, rounds=400)
    NUMBERS["filter 'Given42': typed to rows narrowed, wall (ms)"] = _ms(t0)
    assert 0 < page.model.rowCount() < N
    page.toolbar.filter.clear()
    page._timer.stop()
    page.sidebar.select_key("single_account")
    settle(page, rounds=400)
    assert 0 < page.model.rowCount() < N
    page.sidebar.select_key("all")
    settle(page, rounds=400)
    assert page.model.rowCount() == N


def test_zz_report_the_numbers(big, capsys):
    with capsys.disabled():
        print("\n--- order 1k 6b: 10,000 contacts, offscreen (not Windows numbers) ---")
        for k, v in NUMBERS.items():
            print(f"  {v:9.1f}  {k}")
    assert NUMBERS
