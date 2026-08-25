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
