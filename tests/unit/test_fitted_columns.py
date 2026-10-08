r"""Settings > What's indexed: columns that fit and can be dragged, lines that fit.

Layer: L5

2026-10-08, the owner, of "Folders to index" and "Mail archives": "the columns
on this page are not sizeable and should autofit by default.. also the buttons
size is big they are getting clipped". Every column is draggable and fitted to
its contents; the path column takes the spare width and elides in the middle;
each line is as tall as its controls, so no button or drop-down is cut off.
See `app/ui/widgets/fitted_tree.py`.
"""

from __future__ import annotations

import pytest

from app.ui.widgets.fitted_tree import PATH_FLOOR_PX, share_widths

LONG = (r"D:\Users\jaymin\Documents\Projects\2019\Clients\Contoso Holdings Limited"
        r"\Correspondence and archived drawings")


# -- the arithmetic, without a screen ---------------------------------------------

def test_with_room_to_spare_the_path_takes_all_of_it():
    natural = {0: 300, 1: 200, 2: 40}
    widths = share_widths(natural, {0: 50, 1: 120, 2: 60}, 1000, stretch=0)
    assert widths == {0: 760, 1: 200, 2: 40}
    assert sum(widths.values()) == 1000


def test_short_of_room_the_controls_keep_their_width_and_the_rest_share():
    natural = {0: 500, 1: 118, 2: 380, 3: 265}
    heading = {0: 60, 1: 80, 2: 50, 3: 110}
    widths = share_widths(natural, heading, 900, stretch=0, squeeze=(2,))
    assert widths[1] == 118 and widths[3] == 265      # never squeezed
    assert sum(widths.values()) == 900                # nothing runs off the side
    assert widths[0] >= PATH_FLOOR_PX and widths[2] >= 100
    assert widths[0] < 500 and widths[2] < 380        # both gave some up


def test_below_the_floors_it_scrolls_rather_than_cut_a_heading():
    natural = {0: 500, 1: 300, 2: 150}
    heading = {0: 60, 1: 80, 2: 140}
    widths = share_widths(natural, heading, 400, stretch=0, squeeze=(2,))
    assert widths[0] == PATH_FLOOR_PX
    assert widths[2] == 140                           # its heading, uncut
    assert widths[1] == 300


def test_a_table_not_laid_out_yet_gets_what_it_needs():
    natural = {0: 300, 1: 200}
    assert share_widths(natural, {}, 0, stretch=0) == natural


# -- the folder list ------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QComboBox, QHeaderView, QPushButton  # noqa: E402

pytestmark = pytest.mark.gui


def _shown_roots(qtbot, roots, width=900):
    from app.ui.widgets.roots_box import RootsBox

    box = RootsBox()
    qtbot.addWidget(box)
    box.set_roots(list(roots))
    box.resize(width, 500)
    box.show()
    qtbot.waitExposed(box)
    box.columns.fit()
    qtbot.wait(20)                  # Qt places the line's widgets on its next pass
    return box


def _controls(tree):
    for row in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(row)
        for column in range(tree.columnCount()):
            cell = tree.itemWidget(item, column)
            if cell is not None:
                yield item, column, cell


def test_every_folder_column_can_be_dragged(qtbot):
    box = _shown_roots(qtbot, [LONG, r"D:\Archive\2009"])
    header = box.tree.header()
    for column in range(header.count()):
        assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
    assert not header.stretchLastSection()


def test_the_folder_column_takes_the_spare_width_and_no_heading_is_cut(qtbot):
    box = _shown_roots(qtbot, [LONG, r"D:\Archive\2009"])
    tree, header = box.tree, box.tree.header()
    widths = [header.sectionSize(c) for c in range(header.count())]
    assert sum(widths) == tree.viewport().width()      # fills the row, no more
    for column in range(header.count()):
        assert header.sectionSize(column) >= header.sectionSizeHint(column)
    # The drop-down's column is as wide as its longest choice.
    combo = tree.itemWidget(tree.topLevelItem(0), 1)
    assert header.sectionSize(1) >= combo.sizeHint().width()
    # The path is cut in the middle, and all of it is in the tooltip.
    assert tree.topLevelItem(0).toolTip(0) == LONG


def test_a_new_folder_refits_the_columns(qtbot):
    box = _shown_roots(qtbot, [r"D:\a"])
    before = box.columns.fit()
    box.tree.header().resizeSection(1, 80)            # a person drags one
    assert box.columns.dragged()
    box.add_root(LONG)
    qtbot.waitUntil(lambda: not box.columns.dragged(), timeout=3_000)
    assert box.tree.header().sectionSize(1) == before[1]


def test_a_drag_survives_a_resize_of_the_window(qtbot):
    box = _shown_roots(qtbot, [r"D:\a", LONG])
    box.tree.header().resizeSection(0, 150)
    box.resize(1000, 500)
    qtbot.wait(50)
    assert box.tree.header().sectionSize(0) == 150


def test_no_control_is_taller_than_its_line(qtbot):
    box = _shown_roots(qtbot, [LONG, r"D:\Archive\2009", r"C:\Users\me\Pictures"])
    tree = box.tree
    seen = 0
    for item, column, cell in _controls(tree):
        line = tree.visualItemRect(item)
        place = cell.geometry()
        assert line.top() <= place.top() and place.bottom() <= line.bottom(), (column, place, line)
        assert cell.height() >= cell.sizeHint().height()      # not squashed either
        for button in cell.findChildren(QPushButton):
            assert button.height() <= line.height()
            assert button.height() >= button.sizeHint().height()
        seen += 1
    assert seen == 9
    assert box.columns.row_height() >= max(
        cell.sizeHint().height() for _i, _c, cell in _controls(tree))


def test_the_index_now_button_is_an_icon_with_words_for_the_tooltip_and_reader(qtbot):
    box = _shown_roots(qtbot, [r"D:\a"])
    button = box.tree.itemWidget(box.tree.topLevelItem(0), 4).findChild(QPushButton)
    assert button.text() == "" and not button.icon().isNull()
    assert button.toolTip().startswith("Index now")
    assert button.accessibleName() == r"Index this folder now: D:\a"
    # Compact: shorter than a page's own buttons, which are 28px in the theme.
    assert button.sizeHint().height() <= 28


def test_the_live_archive_drop_down_shows_all_of_its_longest_choice(qtbot):
    box = _shown_roots(qtbot, [r"D:\a"])
    combo = box.tree.itemWidget(box.tree.topLevelItem(0), 1)
    assert isinstance(combo, QComboBox)
    assert combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToContents
    assert combo.width() >= combo.sizeHint().width()
