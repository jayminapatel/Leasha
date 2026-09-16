r"""Offline Media §3c: the Files tab's volume picker.

Layer: L5

The `/on` operator itself was already built and proven end to end in
`test_offline_media.py` (order 202626270513 §3c's first half, closed
2026-09-15). What was missing was a control: this covers the two Qt-free
text decisions the picker is built from - what the box's rows say, and what
picking one does to whatever is already typed - so they are checked without
a display, the same split every other value-menu decision in this project
follows.
"""

from __future__ import annotations

from app.ui.presenter import ALL_LOCATIONS, set_volume_filter, volume_picker_options


class _Count:
    def __init__(self, value, count, exact=True):
        self.value = value
        self.count = count
        self.exact = exact


# ---------------------------------------------------------------------------
# volume_picker_options - what the box's rows say
# ---------------------------------------------------------------------------

def test_all_locations_is_always_first_and_means_no_filter():
    options = volume_picker_options([])
    assert options[0] == (ALL_LOCATIONS, "")


def test_every_catalogued_volume_becomes_a_row_with_its_count():
    values = [_Count("Projects 2019", 41205), _Count("Old WD", 372)]
    options = volume_picker_options(values)

    labels = [label for label, _value in options]
    raw = [value for _label, value in options]
    assert "Projects 2019" in labels[1]
    assert "41,205" in labels[1]
    assert raw == ["", "Projects 2019", "Old WD"]


def test_a_volume_with_zero_files_is_still_offered():
    """The order's own §3c note: "every catalogued volume is offered,
    including one just Scanned with nothing indexed from it yet"."""
    options = volume_picker_options([_Count("Fresh Scan", 0)])
    assert any(value == "Fresh Scan" for _label, value in options)


def test_a_blank_value_is_skipped_rather_than_offered_as_a_row():
    options = volume_picker_options([_Count("", 5), _Count("Real Name", 5)])
    raw = [value for _label, value in options]
    assert raw == ["", "Real Name"]


def test_the_raw_value_survives_a_name_with_punctuation():
    """The label is formatted (padded, a count appended); the raw value
    travels apart from it so `set_volume_filter` never has to parse a name
    back out of the display text - see that function's own docstring."""
    options = volume_picker_options([_Count("Drive - 2019 (old)", 3)])
    assert options[1][1] == "Drive - 2019 (old)"


# ---------------------------------------------------------------------------
# set_volume_filter - what picking one does to the box
# ---------------------------------------------------------------------------

def test_picking_a_volume_appends_a_quoted_on_filter_to_empty_text():
    assert set_volume_filter("", "Projects 2019") == 'on:"Projects 2019"'


def test_picking_a_volume_appends_after_whatever_is_already_typed():
    assert set_volume_filter("invoice", "Old WD") == 'invoice on:"Old WD"'


def test_a_bare_single_word_name_is_not_quoted_unnecessarily():
    """`as_typed_value`'s own rule - quoting is for names the tokenizer would
    otherwise split, not a rule applied unconditionally."""
    assert set_volume_filter("", "OldWD") == "on:OldWD"


def test_picking_all_locations_drops_the_filter_entirely():
    assert set_volume_filter('invoice on:"Old WD"', "") == "invoice"


def test_re_picking_replaces_rather_than_stacking_a_second_filter():
    """A person who changes their mind in the picker must not end up with
    two `on:` terms ANDed together - no file can be on two volumes at once,
    so that would silently mean "nothing"."""
    first = set_volume_filter("invoice", "Old WD")
    second = set_volume_filter(first, "Projects 2019")
    assert second.count("on:") == 1
    assert "Projects 2019" in second
    assert "Old WD" not in second


def test_every_alias_the_parser_accepts_is_replaced_not_just_on():
    """`volume:`/`drive:` are the same field as `on:` in `query.py`'s own
    alias table - a person who typed one by hand and then used the picker
    must not end up with two terms fighting over it."""
    assert "volume:" not in set_volume_filter('volume:"Old WD"', "New One")
    assert "drive:" not in set_volume_filter("drive:OldWD", "New One")


def test_replacing_leaves_the_rest_of_the_query_untouched():
    before = 'type:pdf on:"Old WD" quarterly report'
    after = set_volume_filter(before, "Projects 2019")
    assert "type:pdf" in after
    assert "quarterly report" in after
    assert "Old WD" not in after
    assert '"Projects 2019"' in after
