r"""What an empty search box offers when you click into it.

Layer: L5. Search-experience §2e's last piece, and the surface Adoptions §3
was waiting on.

**One dropdown, three modes.** A `QLineEdit` can only sensibly own one popup —
two would race to appear on the same keystroke and the loser would flicker — so
the `/` menu grew a third mode rather than growing a rival. Command mode,
value mode, and this: recent searches, then saved ones.

**A row here is a whole question, not a fragment of one.** The other two modes
complete the word under the cursor; this replaces the box and runs it, because
somebody choosing a search they have run before has finished asking.

**Off-able, because it is a list of the person's own questions.** Everything
else this application shows is about their files. This is about them, on a
screen other people can see, so "off" is one checkbox rather than an argument.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt                               # noqa: E402
from PyQt6.QtGui import QFocusEvent                               # noqa: E402
from PyQt6.QtWidgets import QApplication                         # noqa: E402

from app.search.saved import SavedSearch, ordered                # noqa: E402
from app.ui.first_contact import (                               # noqa: E402
    RECENT_HEADING, SAVED_HEADING, rows_for, sections,
)
from app.ui.widgets.search_bar import build_input                # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def box(qapp):
    """A real search box over a real store, with a history and a saved search."""
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "fc.db").connect()
    for query in ("leeds site survey", "pump commissioning"):
        store.log_search(query, filters=None, hits=3, elapsed_ms=10,
                         rerank_on=False)
    store.save_search("invoices", "type:pdf from:accounts", "documents")

    submitted: list = []
    line_edit, popup, saved = build_input(
        None, lambda *_a: None, lambda: submitted.append(line_edit.text()),
        store=store)
    # The two fetches are workers; deliver them here so the test is about what
    # the box does with the answers rather than about thread timing.
    saved._took(ordered(store.saved_searches()))
    saved._took_recent(rows_for(store.recent_searches(limit=48)))

    yield line_edit, popup, saved, submitted, store
    store.close()


def _focus_empty(qapp, line_edit, popup,
                 reason=Qt.FocusReason.MouseFocusReason):
    """Click into an empty box, the way a person does. Returns the rows.

    **A real `QFocusEvent` with a real reason**, not a bare `QEvent`, because
    the box now reads `event.reason()` to tell a click apart from the window
    simply opening (see `_OffersOnFocus`) - a bare `QEvent(FocusIn)` has no
    `.reason()` at all and would raise, which a real Qt-dispatched focus
    event never does.
    """
    line_edit.setText("")
    qapp.sendEvent(line_edit, QFocusEvent(QEvent.Type.FocusIn, reason))
    return [popup._model.item(row).text()
            for row in range(popup._model.rowCount())]


# ---------------------------------------------------------------------------
# What it shows
# ---------------------------------------------------------------------------

def test_an_empty_focused_box_offers_recent_then_saved(qapp, box):
    r"""**The acceptance sentence, driven through the widget.** Everything
    below tests one joint; this is the whole thing: focus an empty box and
    look at what is in front of you."""
    line_edit, popup, _saved, _submitted, _store = box

    rows = _focus_empty(qapp, line_edit, popup)
    assert rows[0] == RECENT_HEADING
    assert SAVED_HEADING in rows
    assert rows.index(RECENT_HEADING) < rows.index(SAVED_HEADING)
    assert "pump commissioning" in rows
    assert any("invoices" in row for row in rows)


def test_the_newest_search_is_first(qapp, box):
    """Newest first is what §2e asks for and what somebody expects: the thing
    you were doing a minute ago is the thing you are most likely still doing."""
    line_edit, popup, _saved, _submitted, _store = box

    rows = _focus_empty(qapp, line_edit, popup)
    assert rows.index("pump commissioning") < rows.index("leeds site survey")


def test_a_box_with_something_in_it_offers_nothing(qapp, box):
    r"""**Empty *and* focused.** Popping a list of old searches over the
    query somebody is part-way through typing would be the feature actively
    getting in the way."""
    line_edit, popup, _saved, _submitted, _store = box

    line_edit.setText("half a quest")
    qapp.sendEvent(line_edit, QFocusEvent(
        QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason))
    assert not popup.offering


def test_the_window_opening_does_not_offer_anything(qapp, box):
    r"""**The bug, reported live: "a dropdown by default" on launch.**

    Qt hands the first focusable widget in tab order a `FocusIn` with
    `ActiveWindowFocusReason` when a window is shown - and the search box
    usually is that widget, since Search is the default tab (§2e). Before
    this test existed, `_OffersOnFocus` answered every `FocusIn` alike, so
    an empty box with real history popped its dropdown open the instant the
    window appeared, before anybody had touched it. Same for
    `OtherFocusReason`, the programmatic catch-all a plain `.setFocus()`
    with no reason produces.
    """
    line_edit, popup, _saved, _submitted, _store = box

    for reason in (Qt.FocusReason.ActiveWindowFocusReason,
                  Qt.FocusReason.OtherFocusReason):
        line_edit.setText("")
        popup.show_all()                      # back to command mode first
        qapp.sendEvent(line_edit, QFocusEvent(QEvent.Type.FocusIn, reason))
        assert not popup.offering, f"{reason} must not open the offers dropdown"


def test_a_box_with_nothing_to_offer_opens_nothing(qapp):
    """A dropdown that pops open empty on every click is worse than one that
    never opens at all."""
    line_edit, popup, _saved = build_input(None, lambda *_a: None,
                                           lambda: None, store=None)
    _focus_empty(qapp, line_edit, popup)
    # **Not "the model is empty".** A `CommandPopup` fills itself with the
    # whole filter list the moment it is built, so the model is never empty -
    # the question is whether *this* mode took over, and `offering` is the
    # only honest way to ask it.
    assert not popup.offering


# ---------------------------------------------------------------------------
# What choosing one does
# ---------------------------------------------------------------------------

def test_choosing_a_past_search_runs_it(qapp, box):
    r"""**It replaces the box and submits.** The other two modes complete the
    word under the cursor; a row here is a finished question, and `setText`
    alone emits no `textEdited`, so nothing else would ever have run it."""
    line_edit, popup, _saved, submitted, _store = box

    rows = _focus_empty(qapp, line_edit, popup)
    popup.activated[str].emit("leeds site survey")
    assert line_edit.text() == "leeds site survey"
    assert submitted == ["leeds site survey"]
    assert RECENT_HEADING in rows


def test_choosing_a_saved_search_inserts_the_reference(qapp, box):
    r"""`saved:invoices`, not the query it stands for — *a smart folder, not a
    snapshot*, one level down. A frozen copy in the box would run something
    that is no longer the thing that was saved."""
    line_edit, popup, _saved, submitted, _store = box

    rows = _focus_empty(qapp, line_edit, popup)
    popup.activated[str].emit(next(row for row in rows if "invoices" in row))
    assert line_edit.text() == "saved:invoices"
    assert submitted == ["saved:invoices"]


def test_a_heading_is_read_and_never_chosen(qapp, box):
    """It is a label sitting in a list of choices, which is the one thing a
    list of choices must not do by accident."""
    line_edit, popup, _saved, submitted, _store = box

    _focus_empty(qapp, line_edit, popup)
    popup.activated[str].emit(RECENT_HEADING)
    assert line_edit.text() == ""
    assert submitted == []


def test_the_slash_menu_still_works_afterwards(qapp, box):
    r"""**The regression this mode could most easily cause.** Three modes on
    one popup means three ways to leave a stale one behind - and a `/` menu
    that stopped working after somebody clicked into the box once would be
    reported as "the filters are gone"."""
    line_edit, popup, _saved, _submitted, _store = box

    _focus_empty(qapp, line_edit, popup)
    line_edit.setText("/ty")
    line_edit.textEdited.emit("/ty")
    assert not popup.offering
    rows = [popup._model.item(row).text()
            for row in range(popup._model.rowCount())]
    assert rows and rows[0].split()[0] == "/type"


def test_the_value_menu_still_works_afterwards(qapp, box):
    line_edit, popup, _saved, _submitted, _store = box

    _focus_empty(qapp, line_edit, popup)
    popup.set_values("type", ["pdf", "docx"])
    assert popup.value_of == "type"
    assert not popup.offering


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

class _Off:
    search_offer_recent = False


class _On:
    search_offer_recent = True


def test_switching_it_off_empties_the_recent_half():
    r"""Off-able, per §2e. **The saved half is not a history** — it is a list
    somebody curated on purpose, so the switch that hides what you happened to
    type does not also hide what you deliberately kept."""
    recent = rows_for([{"query": "leeds"}], _Off())
    assert recent == ()
    assert rows_for([{"query": "leeds"}], _On()) == ("leeds",)


def test_the_switch_reaches_the_box(qapp, box):
    line_edit, popup, saved, _submitted, store = box

    saved.set_settings(_Off())
    saved._took_recent(rows_for(store.recent_searches(limit=48), _Off()))
    rows = _focus_empty(qapp, line_edit, popup)
    assert RECENT_HEADING not in rows
    assert SAVED_HEADING in rows, "saved searches are not a history"


def test_the_setting_is_declared_shown_and_read():
    r"""The three legs this codebase requires of every setting, and the
    fourth - that something consumes the value - is `first_contact.rows_for`,
    proved by the test above.
    """
    from app.core.config import SETTING_KEYS, Settings
    from app.core.settings_registry import SETTINGS

    assert "SEARCH_OFFER_RECENT" in SETTING_KEYS
    entry = next(s for s in SETTINGS if s.key == "SEARCH_OFFER_RECENT")
    assert entry.default is True and entry.kind == "bool"
    # **The declared default, not `load_settings()`.** That reads a real
    # `.env`, and the one on the owner's machine names a Windows path this
    # platform refuses - a test about a default should not depend on whose
    # machine it runs on. (`Settings` is a pydantic model, so the field table
    # is `model_fields` rather than `__dataclass_fields__`.)
    assert Settings.model_fields["search_offer_recent"].default is True


def test_the_control_exists_and_is_named_for_the_registry():
    """`test_settings_reachable` greps for the literal; this builds it."""
    from app.ui.widgets.search_behaviour_box import SearchBehaviourBox

    built = SearchBehaviourBox()
    assert built.offer_recent.objectName() == "SEARCH_OFFER_RECENT"

    # A box built with no settings starts every control unchecked and waits
    # for `load` - the same as the seven beside it. What matters is that the
    # value travels both ways.
    built.load(_On())
    assert built.values()["SEARCH_OFFER_RECENT"] is True
    built.load(_Off())
    assert built.values()["SEARCH_OFFER_RECENT"] is False
    built.restore_defaults()
    assert built.values()["SEARCH_OFFER_RECENT"] is True


def test_it_is_not_an_eighth_search_behaviour():
    r"""**Deliberately outside the policy grid.** The seven behaviours are
    per-surface contracts about what a search may *do*, and
    `test_policy_reaches_the_engine` requires each of them to change the
    answer that comes back. This changes no answer - it decides whether an
    empty box offers you your own past questions - so an eighth row would
    have been the same word in all four columns and a guard nobody could
    satisfy honestly.
    """
    from app.search.policy import BEHAVIOURS, SearchPolicy

    assert "offer_recent" not in {name for name, _l, _h in BEHAVIOURS}
    assert not hasattr(SearchPolicy(), "offer_recent")


# ---------------------------------------------------------------------------
# The pieces underneath
# ---------------------------------------------------------------------------

def test_a_history_already_reduced_to_strings_survives_a_second_pass():
    r"""The fetch runs `rows_for` once, off the interface thread, and hands
    back plain text; the dropdown then hands that same list back in when it
    redraws. A second pass has to be the identity rather than a crash."""
    assert rows_for(["leeds", "pumps"]) == ("leeds", "pumps")
    assert sections(["leeds"], [])[0][0] == RECENT_HEADING


def test_the_box_reads_neither_list_from_the_store_itself():
    r"""`test_no_store_call_outside_a_worker` is the rule; this is the same
    rule at the level of this feature. A box being *focused* is not a moment
    to wait on a database, and the first version of §2e was caught doing
    exactly that."""
    source = (pathlib.Path(__file__).resolve().parents[2]
              / "app" / "ui" / "saved_box.py").read_text(encoding="utf-8")
    for call in ("_store.recent_searches(", "_store.saved_searches("):
        assert call not in source, f"saved_box.py calls {call} on the UI thread"


def test_the_search_view_stayed_under_its_line_guard():
    r"""**This item took two lines out of the view rather than adding any.**
    `SavedSearches` moved into `build_input`, where it belongs - the object is
    only ever reached through the box. Recorded as a test because the guard is
    the reason the move happened.
    """
    view = (pathlib.Path(__file__).resolve().parents[2]
            / "app" / "ui" / "search_view.py").read_text(encoding="utf-8")
    code = [line for line in view.splitlines()
            if line.strip() and not line.strip().startswith("#")]
    assert len(code) < 250, f"search_view.py is {len(code)} lines"


def test_saved_searches_offered_here_are_the_same_ones_the_menu_offers(box):
    """One list, two surfaces. Two would be two ways for the same box to
    disagree with itself about what somebody saved."""
    _line_edit, _popup, saved, _submitted, store = box

    offered = {insert for _label, insert in saved.sections()[-1][1]}
    assert offered == {SavedSearch(**{k: row[k] for k in
                                      ("name", "query", "scope", "run_count")}
                                   ).as_token()
                       for row in store.saved_searches()}
