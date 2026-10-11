"""Order 1k 6p, 7, D6/10k, D7.1: the built features a person can now reach.

Layer: L5

The More-tools menu and the right-click open the clean-up and enrich doors; "Deleted from <account>"
offers Delete here too / Put it back; a clash held under "Ask me every time" shows both values in
words with Keep Leasha's / Use <account>'s (through `AccountService.resolve_conflict`), and a clash
the newest edit decided says which value won. The hint under "Kept in" says when a change is written.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QToolButton   # noqa: E402

from app.ui.contacts import logic, openers                                     # noqa: E402

from tests.unit.test_contacts_view_world import card, settle, world           # noqa: E402,F401


def _texts(widget):
    return [l.text() for l in widget.findChildren(QLabel)]


def _button(widget, words):
    return next(b for b in widget.findChildren(QPushButton) if b.text() == words)


@pytest.fixture
def doors(monkeypatch):
    seen = []
    monkeypatch.setattr(openers, "_loaded", True)
    saved = dict(openers.OPENERS)
    for name in ("cleanup", "enrich", "duplicates"):
        openers.register(name, lambda parent, ctx, n=name: seen.append((n, list(ctx.selected_ids))))
    yield seen
    openers.OPENERS.clear()
    openers.OPENERS.update(saved)


# ------------------------------------------------------------------ 6p / 7: the doors
def test_the_more_tools_menu_opens_cleanup_enrich_and_duplicates(world, doors):
    cid = world.add(card("Ann", "Lee", email="ann@acme.example"))
    page = world.page()
    more = page.toolbar.more_button
    assert isinstance(more, QToolButton) and more.accessibleName() == "More tools" and more.toolTip()
    assert not more.icon().isNull()
    acts = page.toolbar.more_actions
    assert [a.text() for a in acts.values()][0] == "Clean up contacts…"
    assert all(a.toolTip() for a in acts.values())
    acts["cleanup"].trigger()
    acts["duplicates"].trigger()
    page.select_ids([cid])
    settle(page)
    assert acts["enrich"].text() == "Enrich this contact…"
    acts["enrich"].trigger()
    assert [d[0] for d in doors] == ["cleanup", "duplicates", "enrich"]
    assert doors[-1][1] == [cid]


def test_enrich_says_what_it_will_run_on(world):
    a = world.add(card("Ann", "Lee"))
    b = world.add(card("Bob", "Bell"))
    page = world.page()
    assert page.toolbar.more_actions["enrich"].text() == "Enrich the contacts shown…"
    page.select_ids([a, b])
    settle(page)
    assert page.toolbar.more_actions["enrich"].text() == "Enrich selection…"


def test_the_right_click_menu_offers_enrich(world, doors, monkeypatch):
    cid = world.add(card("Ann", "Lee", email="ann@acme.example"))
    page = world.page()
    page.select_ids([cid])
    settle(page)
    from app.ui.contacts import quick
    monkeypatch.setattr(quick, "open_menu", lambda menu, pos: None)
    menu = quick.show_menu(page, page.mapToGlobal(page.rect().center()))
    act = next(a for a in menu.actions() if a.text().startswith("Enrich"))
    assert act.text() == "Enrich this contact…"
    act.trigger()
    assert doors[-1] == ("enrich", [cid])


def test_the_real_cleanup_and_enrich_doors_are_registered():
    openers.ensure_tools_loaded()
    assert openers.is_registered("cleanup") and openers.is_registered("enrich")


# ------------------------------------------------------------------ D7.1: deleted elsewhere
def _deleted(world):
    cid = world.add(card("Ann", "Lee", email="ann@x.example"), kept=[world.a_home, world.a_work])
    world.hub.flag_deleted_remote(cid, world.a_work)
    return cid


def test_a_contact_deleted_elsewhere_names_the_account_and_offers_both_answers(world):
    cid = _deleted(world)
    page = world.page()
    page.sidebar.select_key("deleted_elsewhere")
    settle(page)
    page.select_ids([cid])
    settle(page)
    review = page.editor.review
    assert not review.isHidden()
    assert "Deleted from Work" in _texts(review)
    for words in ("Delete here too", "Put it back"):
        b = _button(review, words)
        assert b.toolTip() and b.accessibleName()


def test_put_it_back_writes_the_contact_to_that_account_again(world):
    cid = _deleted(world)
    page = world.page()
    page.show_contact(cid)
    settle(page)
    _button(page.editor.review, "Put it back").click()
    settle(page)
    assert world.hub.link(cid, world.a_work).state == "pending_create"
    assert page.editor.review.isHidden()


def test_delete_here_too_asks_then_bins_the_contact(world, monkeypatch):
    cid = _deleted(world)
    page = world.page()
    page.show_contact(cid)
    settle(page)
    monkeypatch.setattr(page, "confirm", lambda *a, **k: False)
    _button(page.editor.review, "Delete here too").click()
    settle(page)
    assert world.hub.contact_meta(cid)["deleted_at"] is None            # declined: nothing happened
    asked = []
    monkeypatch.setattr(page, "confirm", lambda title, detail, yes: asked.append(yes) or True)
    _button(page.editor.review, "Delete here too").click()
    settle(page)
    assert asked == ["Delete here too"]
    assert world.hub.contact_meta(cid)["deleted_at"] is not None


# ------------------------------------------------------------------ 10k / D6: the clashes
class _Service:
    def __init__(self, hub):
        self.hub = hub
        self.calls = []

    def resolve_conflict(self, cid, account_id, choose):
        self.calls.append((cid, account_id, choose))
        self.hub.acknowledge_conflicts(cid)
        return None


def test_conflict_units_and_values_read_in_plain_words():
    assert logic.unit_words("s:company") == "Company"
    assert logic.unit_words("f:phone:mobile:447700900123") == "Phone (mobile)"
    assert logic.unit_words("f:email:ann@x.example") == "Email"
    assert logic.unit_words("g:family") == "Group"
    assert logic.value_words("s:company", '"Acme"') == "Acme"
    assert logic.value_words("f:phone:mobile:1", '["mobile", "07700 900123", [], false]') == "07700 900123 (mobile)"
    assert logic.value_words("s:company", "null") == "nothing (removed)"


def test_a_held_clash_shows_both_values_and_the_two_buttons(world):
    service_cid = world.add(card("Ann", "Lee", company="Acme"), kept=[world.a_work])
    world.hub.add_conflict(service_cid, world.a_work, "s:company", "Acme", "Acme Ltd", "held")
    world.hub.add_conflict(service_cid, world.a_work, "f:phone:mobile:1", ["mobile", "07700 900123", [], False],
                           ["mobile", "07700 900999", [], False], "held")
    service = _Service(world.hub)
    page = world.page(service=service)
    page.show_contact(service_cid)
    settle(page)
    review = page.editor.review
    text = "\n".join(_texts(review))
    assert 'Company: Leasha has "Acme"; Work has "Acme Ltd".' in text
    assert "Phone (mobile)" in text and "07700 900999" in text
    assert "covers all 2" in text
    assert not page.editor.acknowledge.isVisibleTo(page.editor)       # the choice is the buttons
    keep, use = _button(review, "Keep Leasha's"), _button(review, "Use Work's")
    assert keep.toolTip() and use.accessibleName()
    use.click()
    settle(page)
    assert service.calls == [(service_cid, world.a_work, "remote")]
    assert page.editor.review.isHidden()                               # refreshed: nothing is held now


def test_keep_leasha_s_sends_local(world):
    cid = world.add(card("Ann", "Lee", company="Acme"), kept=[world.a_work])
    world.hub.add_conflict(cid, world.a_work, "s:company", "Acme", "Acme Ltd", "held")
    service = _Service(world.hub)
    page = world.page(service=service)
    page.show_contact(cid)
    settle(page)
    _button(page.editor.review, "Keep Leasha's").click()
    settle(page)
    assert service.calls == [(cid, world.a_work, "local")]


def test_without_a_service_the_choice_changes_nothing_and_says_so(world):
    cid = world.add(card("Ann", "Lee", company="Acme"), kept=[world.a_work])
    world.hub.add_conflict(cid, world.a_work, "s:company", "Acme", "Acme Ltd", "held")
    page = world.page()
    told = []
    page.notice.connect(told.append)
    page.show_contact(cid)
    settle(page)
    _button(page.editor.review, "Keep Leasha's").click()
    settle(page)
    assert told and "Nothing was changed" in told[-1]
    assert len(world.hub.open_conflicts(cid)) == 1


def test_a_clash_the_newest_edit_decided_says_which_value_won(world):
    cid = world.add(card("Ann", "Lee", company="Acme Ltd"), kept=[world.a_work])
    world.hub.add_conflict(cid, world.a_work, "s:company", "Acme", "Acme Ltd", "remote")
    page = world.page()
    page.show_contact(cid)
    settle(page)
    ed = page.editor
    assert not ed.banner.isHidden() and not ed.acknowledge.isHidden()
    assert ed.banner_text.text() == "Changed in two places. The newest edit won; the other is kept in History."
    assert ed.review.isHidden()
    detail = ed.banner_detail.text()
    assert 'Company: kept "Acme Ltd" (from Work)' in detail and '"Acme" from Leasha, is kept in History' in detail


# ------------------------------------------------------------------ the service and the hint
def test_the_service_wrapper_claims_the_account_and_passes_the_choice(tmp_path, monkeypatch):
    from tests.unit.accounts_ui_fakes import make_service
    store, svc = make_service(tmp_path)
    seen = []
    monkeypatch.setattr(svc.engine, "resolve_conflict", lambda c, a, ch: seen.append((c, a, ch)) or "out")
    assert svc.resolve_conflict(1, 2, "local") == "out" and seen == [(1, 2, "local")]
    assert not svc.is_running(2)                                       # released afterwards
    svc._claim([(2, "contacts")])
    assert svc.resolve_conflict(1, 2, "remote") is None and len(seen) == 1   # mid-sync: refused, not queued
    with pytest.raises(ValueError):
        svc.resolve_conflict(1, 2, "newest")
    store.close()


def test_the_lazy_service_delegates_resolve_conflict(monkeypatch):
    from app.ui import account_runtime
    got = []

    class _Svc:
        def resolve_conflict(self, *a):
            got.append(a)
            return "ok"

    monkeypatch.setattr(account_runtime, "get_service", lambda shell: _Svc())
    assert account_runtime.LazyService(object()).resolve_conflict(3, 4, "local") == "ok" and got == [(3, 4, "local")]


def test_the_kept_in_hint_says_it_is_written_at_the_next_sync(world):
    page = world.page()
    hints = [t for t in _texts(page.editor) if t.startswith("Where this contact is stored.")]
    assert hints and "at the next sync (or Sync now)" in hints[0] and "writes it there now" not in hints[0]
