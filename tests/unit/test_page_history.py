"""Back and forward between pages (IA-10, 2026-10-14). Qt-free.

Layer: L5 (presenter half).
"""

from __future__ import annotations

from app.ui.presenter.page_history import CAP, PageHistory


def test_back_and_forward_walk_the_pages_visited():
    h = PageHistory()
    for page in ("Search", "Mail", "Files"):
        assert h.visit(page)
    assert h.back() == ("Mail", "") and h.back() == ("Search", "")
    assert h.back() is None and not h.can_go_back()
    assert h.forward() == ("Mail", "") and h.forward() == ("Files", "")
    assert h.forward() is None and not h.can_go_forward()


def test_somewhere_new_after_going_back_drops_the_forward_places():
    h = PageHistory()
    for page in ("Search", "Mail", "Files"):
        h.visit(page)
    h.back()
    h.visit("Code")
    assert h.places() == [("Search", ""), ("Mail", ""), ("Code", "")]
    assert not h.can_go_forward()


def test_the_page_already_in_front_is_not_recorded_twice():
    h = PageHistory()
    h.visit("Photos")
    assert not h.visit("Photos", "naming")
    assert h.places() == [("Photos", "naming")]


def test_the_sub_state_left_is_the_one_come_back_to():
    h = PageHistory()
    h.visit("Settings", "Appearance")
    h.leave("Accounts")
    h.visit("Mail")
    assert h.back() == ("Settings", "Accounts")


def test_the_oldest_places_are_forgotten_past_the_cap():
    h = PageHistory(cap=5)
    for n in range(12):
        h.visit(f"P{n}")
    assert len(h.places()) == 5 and h.current == ("P11", "")
    assert h.places()[0] == ("P7", "")
    assert CAP >= 20


def test_an_empty_page_name_is_ignored():
    h = PageHistory()
    assert not h.visit("")
    assert h.current is None and h.back() is None


def test_the_contact_picked_is_a_sub_state_that_reads_back():
    from app.ui.presenter.page_history import contact_of, contact_sub

    assert contact_sub(42) == "contact:42" and contact_of("contact:42") == 42
    assert contact_sub(None) == "" and contact_sub(0) == "" and contact_sub("x") == ""
    for other in ("", "naming", "Accounts", "contact:", "contact:abc", "contact:-3"):
        assert contact_of(other) is None


def test_back_returns_to_the_contact_the_page_was_left_on():
    from app.ui.presenter.page_history import contact_of, contact_sub

    h = PageHistory()
    h.visit("Search")
    h.visit("Contacts", contact_sub(7))
    h.visit("Mail")
    h.visit("Contacts", contact_sub(9))
    assert contact_of(h.back()[1]) is None                  # Mail
    assert h.back() == ("Contacts", "contact:7")
    assert h.forward() == ("Mail", "") and h.forward() == ("Contacts", "contact:9")
