r"""The seven adoptions, driven as somebody would drive them.

Layer: L5. The adoptions order's last test item: *a pytest-qt scenario per
item (0m convention); all new strings pass the plain-words/tooltip rules.*

**What "the 0m convention" means here, and what it does not.** Work order 0m
(`WORKORDER-202626270547-test-automation.md`) is HELD, and its own status line
says the per-order convention continues while it is: *each order writes
pytest-qt scenarios for its own acceptance sentences* — that convention lives
in the other orders, not in 0m. So this file writes those scenarios against
the widgets this order actually shipped, with `qtbot` and real key presses.
It deliberately builds none of 0m's own items: no `tools/grab_ui.py`, no
shared `MainWindow` harness fixture, no `gui` marker, no goldens. The one
scenario that needs a whole window — §7a's `leasha://` link — is appended to
`test_window_opens.py`, which already owns the single live `MainWindow` this
process is allowed to build.

**Real widgets, real events, and what is honestly out of reach.** Every test
below constructs the widget the person touches and drives it the way Qt would:
a right-click that goes through `customContextMenuRequested`, `keyClicks` into
a `QLineEdit`, a Tab that has to survive `QLineEdit`'s own focus handling. Two
of this order's items have no view to drive, and their own notes say so:
§1a's expandable row affordance was never built, and §3's save/rename/delete
dialog does not exist. Those are covered where they live — `test_why_result.py`
and `test_saved_searches.py` — and the scenarios here cover the surfaces that
do exist rather than pretending otherwise.

**The plain-words sweep at the bottom reuses the guards this project already
has** rather than inventing a third: `test_search_policy.SURFACE_JARGON` and
`test_index_tuning_acceptance.JARGON` are imported, not copied, so a word
added to either list is enforced here on the next run.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt                                      # noqa: E402
from PyQt6.QtGui import QGuiApplication                          # noqa: E402
from PyQt6.QtWidgets import QMenu                                # noqa: E402

from app.search.engine import SearchResult                       # noqa: E402
from app.ui.presenter import (                                   # noqa: E402
    MEANING_MARKER, cell_location, group_subtitle, why,
)
from app.ui.result_delegate import ROLE_PAYLOAD                  # noqa: E402
from app.ui.results_view import ResultsView                      # noqa: E402

_MTIME = 1_700_000_000_000_000_000


def _result(**changes) -> SearchResult:
    fields = dict(chunk_id=1, file_id=1, path="C:/work/safety report.pdf",
                  text="the safety report for the pump station", score=0.6,
                  rank=0, ext="pdf", mtime_ns=_MTIME, sources=(0,))
    fields.update(changes)
    return SearchResult(**fields)


def _shown(qtbot, results, terms=("safety",)) -> ResultsView:
    """A real `ResultsView`, sized and populated, ready to be looked at."""
    view = ResultsView()
    qtbot.addWidget(view)
    view.resize(720, 240)
    view.show_results(list(results), list(terms))
    return view


def _row_point(view: ResultsView, row: int):
    """Where a right-click on `row` lands, in the coordinates the signal uses.

    `customContextMenuRequested` delivers a point relative to the *widget*;
    `visualRect` answers in viewport coordinates. Mapping back is what makes
    this a click on the first row rather than a click near it - the bug
    `file_menu.viewport_point` exists for, driven from the other end.
    """
    index = view._model.index(row, 0)
    centre = view._list.visualRect(index).center()
    return view._list.viewport().mapTo(view._list, centre)


# ---------------------------------------------------------------------------
# §1a/§1b — "Why this result?"
# ---------------------------------------------------------------------------

def test_right_clicking_a_result_offers_why_it_is_here_and_copies_it(qtbot, monkeypatch):
    r"""**The acceptance sentence, as a gesture**: a user can ask any result
    "why are you here?" and get the answer.

    The affordance that ships is the right-click menu's *Why this result?*
    (`results_view._on_context_menu`), and this drives it end to end: a real
    context-menu request on a real row, the real menu `file_menu.build_menu`
    builds from it, the real action triggered, and the system clipboard read
    back afterwards.

    Only `QMenu.exec` is replaced, because it blocks on a modal popup and
    nothing else here would run until somebody clicked it. Everything the
    menu is - which actions exist, what they carry, what triggering one does -
    stays real.

    **What this cannot cover, stated rather than implied**: §1a's expandable
    line in the row itself was never built (that item's own note says so), so
    `presenter.why_result`'s plain-words sentences have no widget to be driven
    through. They are covered in `test_why_result.py`.
    """
    view = _shown(qtbot, [_result()])
    QGuiApplication.clipboard().setText("something else entirely")

    seen: list = []
    monkeypatch.setattr(QMenu, "exec", lambda self, *a, **k: seen.append(self))

    view._list.customContextMenuRequested.emit(_row_point(view, 0))

    assert seen, "right-clicking a result put no menu on screen"
    labels = [action.text() for action in seen[0].actions()]
    assert "Why this result?" in labels, labels

    asking = next(a for a in seen[0].actions() if a.text() == "Why this result?")
    asking.trigger()

    copied = QGuiApplication.clipboard().text()
    assert copied and copied != "something else entirely"
    assert copied == why(view._rows[0]), (
        "the menu must carry the row's own recorded reasons, not a second "
        "sentence written where it is displayed")


# ---------------------------------------------------------------------------
# §2a — match-type indication
# ---------------------------------------------------------------------------

def test_a_meaning_only_row_says_so_where_a_person_reads_it(qtbot):
    """§2a: a hit with none of the typed words in it carries the marker on
    the line under its name - the row that otherwise reads as a mistake."""
    view = _shown(qtbot, [_result(sources=(1,))])
    group = view._model.item(0).data(ROLE_PAYLOAD)

    assert MEANING_MARKER in group_subtitle(group)


def test_a_keyword_row_is_not_painted_the_same_as_a_meaning_only_one(qtbot):
    r"""**The marker has to reach the pixels, not only the string.**

    Two identical results - same name, same folder, same date, same snippet
    and the same highlighted words - differing in nothing but which lane found
    them. If the marker is drawn, the two renders cannot be the same image;
    if the delegate ever stops asking for the subtitle, they will be, and this
    goes red without anybody having to describe what the row looks like.
    """
    keyword = _shown(qtbot, [_result(sources=(0,))]).grab().toImage()
    meaning = _shown(qtbot, [_result(sources=(1,))]).grab().toImage()

    assert not keyword.isNull() and keyword.size() == meaning.size()
    assert keyword != meaning, (
        "a meaning-only result is drawn exactly like a keyword one - the "
        "marker never reached the painted row")


# ---------------------------------------------------------------------------
# §6a — cell-level spreadsheet locators
# ---------------------------------------------------------------------------

def test_a_spreadsheet_hit_shows_its_sheet_and_cell_where_somebody_hovers(qtbot):
    r"""§6a: *results and snippets for spreadsheet hits show "Sheet 'Q3' ·
    near D14"*.

    Read off the live model's `ToolTipRole` - which is what a person actually
    gets when they hover a row - rather than off the presenter function that
    wrote it, so the wiring between the two is what is under test.
    """
    view = _shown(qtbot, [_result(path="C:/work/costs.xlsx", ext="xlsx",
                                  label="Q3!D14")])
    tip = view._model.item(0).data(int(Qt.ItemDataRole.ToolTipRole))

    assert cell_location("Q3!D14") in tip, tip
    assert "page" not in tip.lower(), (
        "a spreadsheet must not fall back to the sheet index - 'page 3' in a "
        "forty-thousand-row workbook is true and useless")


def test_a_document_that_is_not_a_spreadsheet_claims_no_cell(qtbot):
    """The locator is only ever shown where the extractor could know it."""
    view = _shown(qtbot, [_result()])
    tip = view._model.item(0).data(int(Qt.ItemDataRole.ToolTipRole))

    assert "Sheet" not in tip and "near" not in tip


# ---------------------------------------------------------------------------
# §3a/§3b — saved searches, run from the box
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def saved_engine():
    """A real engine over two real documents, keyword-only - the same recipe
    `test_mini_search.py` uses, for the same reason: nothing here should pay
    for two ONNX models to find out whether a box works."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "saved.db").connect()
    for name, text in (
        ("invoice-april.txt", "invoice for the april delivery"),
        ("survey.txt", "the leeds site survey"),
    ):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext="txt", size_bytes=1,
            mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

        def embed_all(self, _t):
            raise RuntimeError("no model")

    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()
    store.close()


def test_typing_a_saved_name_and_pressing_enter_runs_the_search_it_stands_for(
        qtbot, saved_engine):
    r"""§3a's acceptance sentence, as keystrokes: *keep your favourite
    questions one click away*.

    The whole path is real - the search is saved into the store, re-read by
    the worker that fills the box's list, typed as `saved:invoices`, expanded
    between the keystroke and the search, and answered by a real engine. Run
    means **re-execute live**, so what comes back is the document the stored
    *query* finds, not a stored list of anything.

    The saved scope rides with it: saving a search while looking at one scope
    and getting everything back on replay is the one way this feature would
    become untrustworthy.
    """
    from app.ui.search_view import SearchView

    saved_engine.store.save_search("invoices", "invoice", scope="documents")

    view = SearchView(saved_engine)
    qtbot.addWidget(view)
    view.set_scope("all")
    view.saved.refresh()
    qtbot.waitUntil(lambda: bool(view.saved.all), timeout=5000)

    qtbot.keyClicks(view.input, "saved:invoices")
    qtbot.keyClick(view.input, Qt.Key.Key_Return)

    qtbot.waitUntil(lambda: bool(view.results._rows), timeout=8000)
    try:
        assert [row.path for row in view.results._rows] == \
            ["C:/work/invoice-april.txt"]
        assert view.current_scope() == "documents", (
            "a saved search carries the scope it was saved with")
    finally:
        view.shutdown()


def test_typing_part_of_a_saved_name_offers_it_instead_of_killing_the_window(
        qtbot, saved_engine):
    r"""§3a: *saved searches … in the `/` menu (`/saved <name>` — values with
    counts via the existing machinery)*.

    **This is the scenario that found a live crash**, and it is written the
    way it was found: type a colon and then part of a value, which is what
    anybody does. `command_popup._deliver` filtered the fetched values with
    `value.lower()`, and the values arrive as `ValueCount`s wherever the index
    had a count for one - so the filter raised `AttributeError` inside a
    worker's `finished` slot. PyQt calls `qFatal()` on an unhandled exception
    in a slot, so on Windows that is the window disappearing while somebody
    types. It needed nothing but an index with a count in it, and no test had
    ever typed a partial value into a real box.

    Also the sharp end of "values with counts": the fetched half of the menu
    is what carries the count, and it is the half that was never exercised.
    """
    from app.ui.search_view import SearchView

    saved_engine.store.save_search("invoices", "invoice", scope="documents")

    view = SearchView(saved_engine)
    qtbot.addWidget(view)
    view.saved.refresh()
    try:
        qtbot.keyClicks(view.input, "saved:inv")
        qtbot.waitUntil(lambda: "invoices" in view.commands._values, timeout=5000)
    finally:
        view.shutdown()


def test_a_name_nobody_saved_is_left_exactly_as_it_was_typed(qtbot, saved_engine):
    """`expand_saved`'s rule, driven through the box: silently deleting part
    of somebody's query is the one behaviour that makes a search box
    impossible to trust. It searches for those words and finds nothing."""
    from app.ui.search_view import SearchView

    view = SearchView(saved_engine)
    qtbot.addWidget(view)
    try:
        qtbot.keyClicks(view.input, "saved:nothing-by-this-name")
        assert view.saved.expand(view.input.text()) == "saved:nothing-by-this-name"
    finally:
        view.shutdown()


# ---------------------------------------------------------------------------
# §4a — selection-to-search, and §5a — the chips
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mixed_engine():
    """Files, code and mail, all answering one word - §5a needs more than one
    kind in the result set before a chip is worth showing at all."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "chips.db").connect()
    for name, ext, text in (
        ("alpha.txt", "txt", "widget assembly notes"),
        ("beta.txt", "txt", "widget shipping schedule"),
        ("gamma.py", "py", "def widget(): pass"),
    ):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext=ext, size_bytes=1,
            mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    message_id = store.upsert_file(
        "pst://msg/1", parent_dir="pst://msg", size_bytes=1, mtime_ns=1,
        status="INDEXED", source_kind="pst_message")
    with store.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(file_id, subject, sender, recipients, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, "Widget delivery", "chris@example.com",
             json.dumps([]), 0, 0),
        )
    store.replace_chunks(
        message_id, [{"ordinal": 0, "text": "widget delivery confirmation"}])

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

        def embed_all(self, _t):
            raise RuntimeError("no model")

    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()
    store.close()


def _mini(qtbot, engine):
    from app.ui.widgets.mini_search import MiniSearch

    box = MiniSearch(engine)
    qtbot.addWidget(box)
    return box


def test_a_prefill_is_selected_so_one_keystroke_replaces_it(qtbot, mixed_engine):
    r"""§4a: *it pre-fills the box - selected, so one keystroke replaces it.*

    Both halves, driven: the text arrives (as it does from the worker that
    reads the foreground selection), and then a single real key press is sent
    to the box. If the selection were not made, that keystroke would append
    and somebody who summoned the box to type something else would be fighting
    text they never asked for.
    """
    box = _mini(qtbot, mixed_engine)
    box.summon()
    box.offer_prefill("last quarter's figures")

    assert box.box.text() == "last quarter's figures"
    assert box.box.selectedText() == "last quarter's figures"

    qtbot.keyClicks(box.box, "w")
    assert box.box.text() == "w"
    box.dismiss()


def test_a_prefill_never_lands_on_top_of_a_real_keystroke(qtbot, mixed_engine):
    """The read is asynchronous, so the answer can arrive after somebody has
    started typing. What is on screen wins - this is an offer, not a
    command."""
    box = _mini(qtbot, mixed_engine)
    box.summon()
    qtbot.keyClicks(box.box, "wid")
    box.offer_prefill("last quarter's figures")

    assert box.box.text() == "wid"
    box.dismiss()


def test_tab_cycles_the_chips_and_asks_the_engine_nothing_more(qtbot, mixed_engine):
    r"""§5a, as the person does it: type, then press Tab.

    **Tab is the whole trap.** A plain `QLineEdit` answers a Tab key press
    itself, underneath anything `keyPressEvent` on the frame could intercept -
    so this presses it into the box, exactly where Qt would deliver it, rather
    than calling the event filter directly.

    **Zero extra engine calls, counted.** Cycling is a filter over the response
    already in hand; the counter below is what would notice the day somebody
    "fixes" it by searching again per chip.
    """
    box = _mini(qtbot, mixed_engine)
    box.summon()

    calls = {"n": 0}
    real_search = mixed_engine.search

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real_search(*args, **kwargs)

    mixed_engine.search = counted
    try:
        qtbot.keyClicks(box.box, "widget")
        qtbot.waitUntil(lambda: box.list.count() > 0, timeout=8000)
        qtbot.waitUntil(lambda: bool(box._chip_kinds), timeout=8000)

        counts = dict(box._chip_counts)
        assert counts == {"files": 2, "code": 1, "mail": 1}, counts
        assert [b.text() for b in box._chip_buttons] == \
            ["2 files", "1 email", "1 code result"]
        assert sum(counts.values()) == len(box._all_groups), (
            "the chips must count the result set already in hand")

        after_search = calls["n"]
        seen = []
        for _ in range(len(box._chip_kinds) + 1):
            qtbot.keyClick(box.box, Qt.Key.Key_Tab)
            seen.append(box._active_chip)

        assert seen == [*box._chip_kinds, None], seen
        assert calls["n"] == after_search, (
            "cycling the chips called the engine again - it is a filter over "
            "the rows already fetched, not a second search")

        box._select_chip("mail")
        assert len(box._rows) == 1 and box._rows[0].kind == "email"
        assert calls["n"] == after_search
    finally:
        mixed_engine.search = real_search
        box.dismiss()


def test_one_kind_of_result_gets_no_chip_row_at_all(qtbot, mixed_engine):
    """One chip repeating the count the list above it already shows is noise,
    not an answer - the reason a chip exists is a choice between kinds."""
    box = _mini(qtbot, mixed_engine)
    box.summon()
    qtbot.keyClicks(box.box, "assembly")
    qtbot.waitUntil(lambda: box.list.count() > 0, timeout=8000)

    assert not box.chips.isVisible()
    assert box._chip_buttons == []
    box.dismiss()


# ---------------------------------------------------------------------------
# The plain-words sweep — the existing guards, over this order's strings
# ---------------------------------------------------------------------------

from tests.unit.test_index_tuning_acceptance import JARGON as TUNING_JARGON  # noqa: E402
from tests.unit.test_search_policy import SURFACE_JARGON                     # noqa: E402

#: The two lists this project already enforces, joined - **imported, never
#: retyped.** The tuning screen's words and the search surfaces' words are
#: both this codebase's vocabulary rather than anybody's, and a string added
#: by this order has to survive both.
DENIED = tuple(TUNING_JARGON) + tuple(SURFACE_JARGON)


def _new_strings() -> dict:
    """Every string the seven adoptions put in front of somebody, by name.

    Built from the code rather than pasted, so a reworded sentence is swept
    on the next run without anybody remembering to update a list here.
    """
    from app.search import policy
    from app.search.commands import ACTIONS
    from app.ui.presenter import VALUE_NOUNS, why_result
    from app.ui.widgets.mini_search import CHIP_ORDER, chip_label

    decorated = SearchResult(
        chunk_id=1, file_id=1, path="C:/work/essay.docx", rank=1, score=0.5,
        text="my homework about volcanoes", sources=(0, 1), mtime_ns=time.time_ns(),
        declares=True, recency=1.0)

    found = {"match marker": MEANING_MARKER,
             "cell locator": cell_location("Q3!D14")}
    # §1: the plain register - the tab an eight-year-old uses. The technical
    # register is allowed its detail by §1b and is checked in
    # `test_why_result.py`, which is where the register rule lives.
    for index, line in enumerate(why_result(decorated, None, opens=3)):
        found[f"why-result line {index}"] = line
    for bucket in CHIP_ORDER:
        for count in (1, 2):
            found[f"chip {bucket} x{count}"] = chip_label(bucket, count)
    for command in ACTIONS:
        found[f"/{command.name} summary"] = command.summary
        found[f"/{command.name} hint"] = command.value_hint
    found["saved-search noun"] = VALUE_NOUNS.get("saved", "")
    for name, label, help_text in policy.BEHAVIOURS:
        if name == "explain_results":
            found["the switch's label"] = label
            found["the switch's sentence"] = help_text
    return found


def _offenders(strings: dict) -> list:
    return [f"{where}: {word}" for where, text in strings.items()
            for word in DENIED if word in str(text).lower()]


def test_every_string_these_seven_features_added_speaks_english():
    r"""**The order's own words**: *all new strings pass the plain-words
    rules.*

    The same deny-list mechanism the tuning screen and the behaviour switches
    already use, pointed at what this order wrote - not a second mechanism
    that could quietly disagree with either.
    """
    strings = _new_strings()

    assert len(strings) >= 12, "the sweep stopped finding this order's strings"
    assert not _offenders(strings), (
        "these use this codebase's vocabulary rather than English:\n  "
        + "\n  ".join(_offenders(strings)))


def test_the_sweep_would_notice_a_string_in_this_codebases_language():
    """A guard that has never rejected anything is a guard nobody has tested.
    The house style from `test_tooltips.py`, applied to this one."""
    assert _offenders({"a made-up chip": "3 embedding vector hits"})
    assert not _offenders({"a real chip": "3 emails"})


def test_the_mini_search_chips_are_controls_that_explain_themselves(
        qtbot, mixed_engine):
    r"""The tuning screen's guard - *no control speaks this codebase's
    language*, and every control says what pressing it does - applied to the
    live chip buttons §5a added.

    `test_tooltips.py` proves statically that a tooltip is *set*; this reads
    the built widget and checks what it says. The chip that was found with no
    tooltip at all was found by that static guard, and this is the half that
    would notice a tooltip full of jargon.
    """
    box = _mini(qtbot, mixed_engine)
    box.summon()
    qtbot.keyClicks(box.box, "widget")
    qtbot.waitUntil(lambda: bool(box._chip_buttons), timeout=8000)

    shown = {}
    for button in box._chip_buttons:
        assert button.toolTip(), f"{button.objectName()} says nothing about itself"
        shown[f"{button.objectName()} label"] = button.text()
        shown[f"{button.objectName()} tooltip"] = button.toolTip()

    assert not _offenders(shown), _offenders(shown)
    box.dismiss()
