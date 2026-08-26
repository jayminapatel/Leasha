"""Which columns, how tight, how big - and the rules that stop it going wrong.

Layer: L5

Three settings that answer one question: how much do I want on screen at once.

The interesting cases are all failure cases. A table with no columns is
indistinguishable from a broken one. A column of blanks reads as a broken index
rather than as absent data. A preference that outlives the data it was about
must not permanently hide a column that has since come back. None of those are
obvious from the feature description, and all three are cheap to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.ui.view_options import (
    DENSITIES,
    FONT_RANGE,
    Density,
    ViewPreferences,
    available_columns,
    load_prefs,
    parse_prefs,
    prefs_to_state,
    row_height_for,
    save_prefs,
    visible_columns,
)

ORDER = ["from", "to", "date", "subject", "attach", "size"]
PAIRS = [(key, key) for key in ORDER]


@dataclass
class Row:
    """A stand-in for MailRow: any object with the attributes."""
    from_: str = ""
    to: str = ""
    date: str = ""
    subject: str = ""
    attach: str = ""
    size: str = ""


def row(**values):
    r = Row()
    for key, value in values.items():
        setattr(r, key, value)
    return r


# ---------------------------------------------------------------------------
# A column is offered when the data can fill it
# ---------------------------------------------------------------------------

def test_a_column_with_data_is_available():
    rows = [row(subject="Invoice"), row(subject="")]
    assert "subject" in available_columns(rows, PAIRS)


def test_a_column_that_is_empty_everywhere_is_not_offered():
    """A column of blanks takes width from the ones that matter and reads as a
    broken index. Some PST exports really do carry no To line at all."""
    rows = [row(subject="Invoice"), row(subject="Receipt")]
    assert "to" not in available_columns(rows, PAIRS)


def test_one_row_with_data_is_enough():
    rows = [row(), row(), row(attach="Yes")]
    assert "attach" in available_columns(rows, PAIRS)


def test_always_offered_columns_survive_an_empty_result():
    """A mail list without a sender is not a mail list. Filtering down to one
    blank-subject message must not take the column away from the next filter."""
    assert available_columns([], PAIRS, always=("from", "subject")) == ("from", "subject")


def test_availability_works_on_mappings_too():
    assert "subject" in available_columns([{"subject": "hi"}], PAIRS)


def test_zero_and_false_count_as_empty():
    """A size column of zeroes and an attachment column of Falses are both
    columns of nothing, whatever their type says."""
    assert available_columns([row(size=0), row(size=False)], [("size", "size")]) == ()


# ---------------------------------------------------------------------------
# What actually gets drawn
# ---------------------------------------------------------------------------

def test_no_preference_shows_everything_available():
    assert visible_columns(ViewPreferences(), ORDER, ORDER) == tuple(ORDER)


def test_a_preference_narrows_it():
    prefs = ViewPreferences(columns=("from", "subject"))
    assert visible_columns(prefs, ORDER, ORDER) == ("from", "subject")


def test_canonical_order_is_kept_whatever_order_was_saved():
    """A table whose columns reshuffle as you toggle them is disorienting, and
    the order was chosen to be scanned."""
    prefs = ViewPreferences(columns=("size", "from", "date"))
    assert visible_columns(prefs, ORDER, ORDER) == ("from", "date", "size")


def test_a_chosen_column_with_no_data_is_not_drawn():
    prefs = ViewPreferences(columns=("from", "to"))
    assert visible_columns(prefs, ORDER, ("from", "subject")) == ("from",)


def test_a_column_comes_back_on_its_own_when_the_data_does():
    """**The reason the preference and the drawing are separate.**

    Hiding "To" because a mailbox had no recipients must not require somebody
    to remember, six months later, to turn it back on - they will not, and they
    will conclude the feature is broken.
    """
    prefs = ViewPreferences()          # never touched the menu
    assert "to" not in visible_columns(prefs, ORDER, ("from", "subject"))
    assert "to" in visible_columns(prefs, ORDER, ("from", "to", "subject"))


def test_a_table_is_never_drawn_with_no_columns_at_all():
    """Indistinguishable from a broken one, and there is no way back through
    the same menu."""
    prefs = ViewPreferences(columns=("nothing-real",))
    assert visible_columns(prefs, ORDER, ORDER) == tuple(ORDER)


def test_the_last_column_cannot_be_switched_off():
    prefs = ViewPreferences(columns=("from",))
    assert prefs.with_column("from", False, order=ORDER).columns == ("from",)


def test_toggling_on_from_the_default_keeps_the_others():
    """The default is "everything", so turning one *on* must not be read as
    "only this one"."""
    prefs = ViewPreferences()
    result = prefs.with_column("size", True, order=ORDER)
    assert result.columns == tuple(ORDER)


def test_toggling_off_from_the_default_drops_only_that_one():
    result = ViewPreferences().with_column("size", False, order=ORDER)
    assert result.columns == tuple(k for k in ORDER if k != "size")


def test_toggling_a_column_leaves_the_other_settings_alone():
    prefs = ViewPreferences(density=Density.COMPACT, font_pt=13)
    result = prefs.with_column("size", False, order=ORDER)
    assert (result.density, result.font_pt) == (Density.COMPACT, 13)


# ---------------------------------------------------------------------------
# Row height
# ---------------------------------------------------------------------------

def test_compact_is_shorter_than_normal():
    assert row_height_for(Density.COMPACT, 20) < row_height_for(Density.NORMAL, 20)


def test_height_follows_the_font_so_bigger_text_is_not_clipped():
    """Derived from font metrics rather than a fixed pixel count: a compact row
    at 16pt must not cut the descenders off."""
    assert row_height_for(Density.COMPACT, 30) > row_height_for(Density.COMPACT, 12)


def test_an_unknown_density_falls_back_rather_than_raising():
    assert row_height_for("enormous", 20) == row_height_for(Density.NORMAL, 20)


def test_a_nonsense_font_height_still_gives_a_usable_row():
    assert row_height_for(Density.COMPACT, 0) >= 14


# ---------------------------------------------------------------------------
# Persistence - which must never be the reason the window will not open
# ---------------------------------------------------------------------------

def test_a_round_trip_survives():
    prefs = ViewPreferences(columns=("from", "date"), density=Density.COMPACT, font_pt=12)
    assert parse_prefs(prefs_to_state(prefs, "ui:mail"), "ui:mail") == prefs


def test_an_empty_store_gives_the_defaults():
    assert parse_prefs({}, "ui:mail") == ViewPreferences()


@pytest.mark.parametrize("value", ["", "abc", "999", "-4", None])
def test_a_corrupt_font_size_is_ignored_not_raised(value):
    """Read while the window is being built. A bad row in a settings table must
    never be the reason the application will not open."""
    prefs = parse_prefs({"ui:mail:font_pt": value}, "ui:mail")
    assert prefs.font_pt == 0


def test_a_font_size_outside_the_range_falls_back_to_the_system_font():
    assert parse_prefs({"ui:mail:font_pt": "40"}, "ui:mail").font_pt == 0
    assert parse_prefs({"ui:mail:font_pt": str(FONT_RANGE[1])}, "ui:mail").font_pt == FONT_RANGE[1]


def test_an_unknown_density_is_ignored():
    assert parse_prefs({"ui:mail:density": "spacious"}, "ui:mail").density == Density.NORMAL


def test_the_prefixes_keep_the_lists_apart():
    """Files and Mail want different layouts; one key would make them fight."""
    state = {**prefs_to_state(ViewPreferences(("name",)), "ui:files"),
             **prefs_to_state(ViewPreferences(("from",)), "ui:mail")}
    assert parse_prefs(state, "ui:files").columns == ("name",)
    assert parse_prefs(state, "ui:mail").columns == ("from",)


class BrokenStore:
    def all_state(self):
        raise RuntimeError("database is locked")

    def set_states(self, _values):
        raise RuntimeError("database is locked")


def test_a_locked_database_opens_with_defaults_rather_than_not_opening():
    assert load_prefs(BrokenStore(), "ui:mail") == ViewPreferences()


def test_a_failed_save_is_reported_not_raised():
    """The change is already on screen. Failing to persist a column preference
    must not interrupt what somebody was doing."""
    assert save_prefs(BrokenStore(), "ui:mail", ViewPreferences()) is False


def test_a_working_save_says_so():
    saved: dict = {}

    class Store:
        def set_states(self, values):
            saved.update(values)

    assert save_prefs(Store(), "ui:mail", ViewPreferences(("from",), Density.COMPACT, 11))
    assert saved["ui:mail:columns"] == "from"
    assert saved["ui:mail:density"] == Density.COMPACT
    assert saved["ui:mail:font_pt"] == "11"


def test_there_are_exactly_two_densities_and_compact_is_one():
    """The names are persisted, so renaming one silently resets everybody's
    preference to the default."""
    names = {name for name, _label, _mult in DENSITIES}
    assert names == {Density.COMPACT, Density.NORMAL}


# ---------------------------------------------------------------------------
# Grouping and scores (work order §5)
# ---------------------------------------------------------------------------

def test_grouping_is_on_by_default():
    """Chunk-level rows are the complaint this answers: a long PDF matching in
    five places took five of the top ten rows."""
    assert ViewPreferences().group_by_document is True


def test_scores_are_off_by_default():
    """"Why is this here" is where trust comes from and must stay reachable -
    but it does not need to be the second thing the eye lands on, on every row,
    forever. It moved to the tooltip and the menu."""
    assert ViewPreferences().show_scores is False


def test_the_flat_list_is_still_available():
    """Somebody comparing two passages of the same document wants them side by
    side. Taking that away would be a different complaint."""
    assert ViewPreferences(group_by_document=False).group_by_document is False


def test_both_survive_a_round_trip():
    prefs = ViewPreferences(("a",), Density.COMPACT, 12,
                            group_by_document=False, show_scores=True)
    assert parse_prefs(prefs_to_state(prefs, "ui:results"), "ui:results") == prefs


@pytest.mark.parametrize(("stored", "expected"), [
    ("on", True), ("true", True), ("1", True), ("yes", True),
    ("off", False), ("false", False), ("0", False), ("no", False),
])
def test_a_flag_reads_the_spellings_people_write(stored, expected):
    """These are hand-editable rows in a settings table. Accepting only one
    spelling turns a reasonable edit into a silent revert to the default."""
    assert parse_prefs({"ui:x:scores": stored}, "ui:x").show_scores is expected


def test_a_nonsense_flag_falls_back_to_the_default_rather_than_off():
    """`False` is a real setting, not an error value - so a corrupt row must
    restore the *default*, which for grouping is on."""
    assert parse_prefs({"ui:x:group": "banana"}, "ui:x").group_by_document is True


def test_toggling_a_column_leaves_grouping_and_scores_alone():
    prefs = ViewPreferences(group_by_document=False, show_scores=True)
    result = prefs.with_column("size", False, order=["name", "size"])
    assert result.group_by_document is False
    assert result.show_scores is True


# ---------------------------------------------------------------------------
# The View menu must never quietly reset a preference it was not asked about
# ---------------------------------------------------------------------------

def test_the_menu_never_builds_preferences_positionally():
    """U5, and the reason it is a source check rather than a click test.

    `ViewPreferences(prefs.columns, n, prefs.font_pt)` enumerates fields by
    position, so every field after the third is silently dropped - changing the
    row height turned grouping back on and discarded "show why each result
    matched". The bug is not in any one call; it is the shape of the call, and
    it comes back the moment somebody adds a field.

    `replace(prefs, density=n)` cannot have this bug: it says which field it is
    changing and carries the rest, whatever they are.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "app" / "ui" / "view_options.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if name == "ViewPreferences" and node.args:
            offenders.append(node.lineno)

    assert not offenders, (
        "view_options.py builds ViewPreferences with positional arguments at "
        f"line(s) {offenders}. Use dataclasses.replace(prefs, field=value) so "
        "fields nobody mentioned are carried rather than reset to their defaults."
    )


def test_every_field_survives_a_change_to_any_other_field():
    """Whatever fields the dataclass grows, changing one keeps the rest.

    Written over `fields()` rather than over a list of names, so a field added
    later is covered without anybody remembering to extend this test.
    """
    from dataclasses import fields, replace

    #: A value that differs from the default for each kind, so "carried" and
    #: "reset to default" cannot look the same.
    def other_than(value):
        if isinstance(value, bool):
            return not value
        if isinstance(value, int):
            return value + 3
        if isinstance(value, tuple):
            return ("name",)
        return "compact" if value != "compact" else "normal"

    changed = {f.name: other_than(getattr(ViewPreferences(), f.name))
               for f in fields(ViewPreferences)}
    prefs = ViewPreferences(**changed)

    for field in fields(ViewPreferences):
        updated = replace(prefs, **{field.name: getattr(ViewPreferences(), field.name)})
        for other in fields(ViewPreferences):
            if other.name == field.name:
                continue
            assert getattr(updated, other.name) == changed[other.name], (
                f"changing {field.name} lost {other.name}"
            )


# --- the crash that stopped the window opening ------------------------------
#
# `MainWindow._apply_theme` sets a stylesheet on the top-level window. Qt
# re-polishes every child, font metrics change, header sections resize, and
# `sectionResized` fires from inside Qt's own layout. `remember_widths` saved
# from there, saving calls `on_change`, and `on_change` re-applies the view -
# re-entering a layout that had not finished. The process died in C++ during
# `MainWindow.__init__`: no Python exception, no traceback, no run-log footer,
# no window. Diagnosed only after enabling faulthandler, whose stack read
# `__init__ -> _apply_theme -> resized`.
#
# `_apply_widths` guards the recursion it starts itself with the APPLYING flag.
# It cannot guard this one, which arrives with the flag clear.


def test_nothing_connects_to_sectionresized_during_construction():
    """**The fix, and it took bisecting to find.**

    Three attempts at what the slot *does* changed nothing - the last of them
    made its first statement read a Python bool, which cannot fault. Skipping
    the `connect()` entirely was the experiment that settled it: with no
    connection the window opens.

    So the fault is in invoking a Python slot from `sectionResized` while
    `setStyleSheet` re-polishes the widget tree. The connection must be made
    from the event loop, after construction, not during it.

    Asserted on the source rather than by driving Qt: the failure is a native
    crash, so a test that reproduces it takes the runner down instead of
    failing.
    """
    import inspect

    from app.ui import view_options

    body = inspect.getsource(view_options.remember_widths)
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )

    connect_at = code.index("sectionResized.connect")
    defer_at = code.index("singleShot")
    assert defer_at < connect_at, (
        "sectionResized is connected during construction. Qt then invokes the "
        "slot from inside setStyleSheet's polish, and the process dies in C++ "
        "with no Python exception."
    )
    assert "def listen(" in code, "the connection is not deferred behind a callback"


def test_a_header_resize_is_never_saved_from_inside_the_signal():
    """Belt and braces: even once connected, the save is deferred.

    `remember_width` calls `on_change`, which re-applies the whole view. Doing
    that from inside `sectionResized` re-enters a layout that has not finished -
    the recursion `_apply_widths` documents and guards with APPLYING.
    """
    import inspect

    from app.ui import view_options

    body = inspect.getsource(view_options.remember_widths)
    handler = body.split("def resized(")[1]
    code = "\n".join(
        line for line in handler.splitlines()
        if not line.strip().startswith("#")
    )

    assert "singleShot" in handler
    assert "button.remember_width(" not in code, (
        "the signal handler still calls remember_width directly; deferring the "
        "call is the whole fix"
    )


def test_the_deferred_save_survives_a_table_that_has_gone_away():
    """A queued callback outlives the widget that queued it.

    A tab closing between the resize and the next event-loop turn leaves the
    C++ side deleted, and PyQt raises RuntimeError on touch. Saving a column
    width is not worth a traceback.
    """
    import inspect

    from app.ui import view_options

    body = inspect.getsource(view_options.remember_widths)
    record = body.split("def record(")[1].split("def resized(")[0]

    assert "RuntimeError" in record


def test_the_preview_toggle_is_offered_on_every_list_not_only_search():
    """**Reported: "the option in the view menu is only on the main search tab".**

    "One row per document" and "Show why each result matched" are about ranked
    search results, so they live behind `grouping`. The preview pane sat in that
    same block by accident - so Files, Mail and Code each built a preview pane,
    wired it up, and offered no way to switch it on.
    """
    import inspect

    from app.ui import view_options

    source = inspect.getsource(view_options.build_menu)
    grouping_block = source.split("if grouping:")[1].split("\n    if columns:")[0]

    assert "Preview pane" not in grouping_block, (
        "the preview toggle is inside the grouping branch, so it only appears "
        "on the search tab"
    )
    assert "Preview pane" in source, "the toggle has gone missing entirely"


def test_the_search_only_options_stay_search_only():
    """The other half: moving preview out must not drag these with it.

    A table of files has one row per file already, so offering to group it would
    be offering nothing.
    """
    import inspect

    from app.ui import view_options

    source = inspect.getsource(view_options.build_menu)
    grouping_block = source.split("if grouping:")[1].split("\n    if columns:")[0]

    assert "One row per document" in grouping_block
    assert "Show why each result matched" in grouping_block


# -- no column may take the whole row ----------------------------------------
#
# The complaint that produced this: the Files tab's Name column arrives wide
# enough to push everything else off the right-hand side, and once a width has
# been dragged and saved it stays there for good.

def test_the_cap_is_a_share_of_the_table():
    from app.ui.view_options import MAX_COLUMN_SHARE, column_cap

    assert column_cap(1000) == int(1000 * MAX_COLUMN_SHARE)
    assert column_cap(2000) == int(2000 * MAX_COLUMN_SHARE)


def test_a_narrow_window_falls_back_to_the_floor():
    """40% of a very narrow table is a few dozen pixels, which truncates every
    value to an ellipsis - the same list, unusable for a different reason."""
    from app.ui.view_options import MIN_COLUMN_CAP_PX, column_cap

    assert column_cap(200) == MIN_COLUMN_CAP_PX
    assert column_cap(120) == MIN_COLUMN_CAP_PX


def test_an_unlaid_out_table_is_not_capped():
    """A widget reports a width of zero before it is shown. Capping against
    that would pin every column to the floor before the window appears."""
    from app.ui.view_options import column_cap

    assert column_cap(0) == 0
    assert column_cap(-50) == 0
    assert column_cap(None) == 0
    assert column_cap("wide") == 0


def test_the_cap_leaves_room_for_the_other_columns():
    """The property that matters, stated as a property: whatever the width,
    one column can never take so much that the rest cannot be read."""
    from app.ui.view_options import column_cap

    for width in (400, 800, 1280, 1920, 3840):
        assert column_cap(width) < width, width


# --- fitting, and the column that could never keep a width ------------------

COLUMNS_3 = [("name", "Name"), ("path", "Folder"), ("size", "Size")]
AVAILABLE_3 = ("name", "path", "size")


def _table(app, *, text: str = "value"):
    """A laid-out three-column table with something in every cell."""
    from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem

    table = QTableWidget(3, 3)
    table.setHorizontalHeaderLabels([heading for _key, heading in COLUMNS_3])
    for row in range(3):
        for column in range(3):
            table.setItem(row, column, QTableWidgetItem(text))
    table.resize(900, 300)
    table.show()
    app.processEvents()
    return table


def _qt():
    import pytest

    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None
    return app


def test_fit_columns_to_contents_actually_measures_again():
    r"""**The menu item did nothing at all, and the condition is why.**

    `_apply_widths` gated its re-measure on `not FITTED or prefs.widths`. "Fit
    columns to contents" clears `widths`, so once `FITTED` was set the whole
    condition was false and the only line that fits anything was skipped. The
    preference vanished and the columns stayed exactly as dragged - measured on
    a real table as `[250, 47, 588]` before and `[250, 47, 588]` after.

    Fitting is now an explicit request that clears the flag, rather than a state
    the restore code has to infer from an empty tuple.
    """
    from app.ui.view_options import apply_to_table, button as view_button

    app = _qt()
    table = _table(app, text="tiny")
    saved: list = []
    chooser = view_button(
        None, None, "", columns=COLUMNS_3, table=table,
        on_change=lambda prefs: (
            saved.append(prefs),
            apply_to_table(table, prefs, columns=COLUMNS_3, available=AVAILABLE_3),
        ),
    )
    chooser.remember_width("name", 250)
    apply_to_table(table, chooser.prefs, columns=COLUMNS_3, available=AVAILABLE_3)
    assert table.columnWidth(0) == 250

    chooser.refit()

    assert chooser.prefs.widths == ()
    assert table.columnWidth(0) < 250, (
        "Fit columns to contents cleared the saved width and left the column "
        "at the size it was dragged to")


def test_the_menu_calls_the_fit_callback_rather_than_a_plain_change():
    """The flag lives on the table, which the menu cannot reach - so a menu
    given no `on_fit` would clear the preference and fit nothing, which is the
    bug above wearing a different hat."""
    from app.ui.view_options import ViewPreferences, build_menu

    app = _qt()
    from PyQt6.QtWidgets import QWidget

    parent = QWidget()                       # held: a temporary is collected
    asked: list = []
    menu = build_menu(
        parent, ViewPreferences(widths=(("name", 250),)), columns=COLUMNS_3,
        available=AVAILABLE_3, on_change=lambda prefs: asked.append("change"),
        on_fit=lambda: asked.append("fit"),
    )
    fit = [a for a in menu.actions() if "Fit columns" in a.text()]
    assert fit, "the menu no longer offers Fit columns to contents"
    fit[0].trigger()

    assert asked == ["fit"]


def test_the_last_column_can_keep_a_width_somebody_dragged():
    r"""**`setStretchLastSection` owns the last column outright.**

    Qt recomputes it on every layout, so a width dragged there was overwritten
    within the same repaint and a saved one was overwritten on restore. The
    column simply refused to keep a size - and on a table whose last column is
    the one worth widening, that is the whole of "it does not remember my
    columns".

    Stretching is the right default and the wrong override, so it now holds
    only until somebody takes control.
    """
    from app.ui.view_options import ViewPreferences, apply_to_table

    app = _qt()
    table = _table(app)

    apply_to_table(table, ViewPreferences(), columns=COLUMNS_3, available=AVAILABLE_3)
    assert table.horizontalHeader().stretchLastSection(), (
        "a table nobody has dragged should still fill the width")

    dragged = ViewPreferences(widths=(("size", 300),))
    apply_to_table(table, dragged, columns=COLUMNS_3, available=AVAILABLE_3)

    assert not table.horizontalHeader().stretchLastSection()
    assert table.columnWidth(2) == 300


def test_a_dragged_width_survives_a_relaunch(tmp_path):
    r"""The whole chain, in the order it happens: drag, save, close, reopen,
    fill, restore. Every link was verified separately and the report was still
    *"the column width resets every launch"* - so the chain is asserted whole.

    The mouse button is held for the duration, because that is what
    `remember_widths` uses to tell a drag from a fit and no offscreen test can
    hold one down for real.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from app.storage.sqlite_store import SqliteStore
    from app.ui.view_options import apply_to_table, button as view_button

    app = _qt()
    held = staticmethod(lambda: Qt.MouseButton.LeftButton)
    original = QApplication.mouseButtons
    QApplication.mouseButtons = held
    try:
        database = tmp_path / "index.db"
        first = _table(app)
        with SqliteStore(database) as store:
            chooser = view_button(None, store, "ui:files", columns=COLUMNS_3,
                                  on_change=lambda _p: None, table=first)
            apply_to_table(first, chooser.prefs, columns=COLUMNS_3,
                           available=AVAILABLE_3)
            app.processEvents()              # the deferred connect()
            first.horizontalHeader().resizeSection(1, 320)
            app.processEvents()              # the deferred record()
            assert dict(chooser.prefs.widths).get("path") == 320

        second = _table(app)
        with SqliteStore(database) as store:
            reopened = view_button(None, store, "ui:files", columns=COLUMNS_3,
                                   on_change=lambda _p: None, table=second)
            apply_to_table(second, reopened.prefs, columns=COLUMNS_3,
                           available=AVAILABLE_3)

        assert dict(reopened.prefs.widths).get("path") == 320
        assert second.columnWidth(1) == 320
    finally:
        QApplication.mouseButtons = original


def test_columns_are_measured_once_per_table_not_once_per_fill(monkeypatch):
    r"""**The other half of the fitting condition, and a cost rather than a bug.**

    `_apply_widths` used to re-measure whenever `prefs.widths` was non-empty -
    so the moment anybody dragged a single column, every subsequent fill
    re-fitted every column. On a debounced list that is a full re-layout per
    keystroke, it fights the drag that produced the preference in the first
    place, and the comment above it records that it eventually took the process
    down inside Qt's own layout code.

    Counting the calls rather than the pixels: the widths afterwards are
    identical either way, which is exactly why this went unnoticed.
    """
    from app.ui.view_options import ViewPreferences, apply_to_table

    app = _qt()
    table = _table(app)
    calls: list[int] = []

    # **`monkeypatch`, not a try/finally around a class attribute.** Reading
    # `type(table).resizeColumnsToContents` off a `QTableWidget` yields the
    # unbound `QTableView` method, and assigning that back onto `QTableWidget`
    # leaves a binding that raises `first argument of unbound method must have
    # type 'QTableView'` in every later test that builds one. `monkeypatch`
    # deletes an attribute it added rather than restoring what it read.
    monkeypatch.setattr(
        type(table), "resizeColumnsToContents",
        lambda self: calls.append(1), raising=False)

    dragged = ViewPreferences(widths=(("name", 250),))
    for _ in range(4):
        apply_to_table(table, dragged, columns=COLUMNS_3, available=AVAILABLE_3)

    assert len(calls) == 1, (
        f"measured {len(calls)} times for four fills - a saved width must not "
        f"turn every redraw into a full re-fit")
