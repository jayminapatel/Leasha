r"""The one right-click menu builder (`app/ui/widgets/menus.py`), UI phase B.

Layer: L5

The owner: every screen should feel like one app. Before this, each list built
its own `QMenu`: most lines had no icon, the action a double-click runs could
be anywhere in the menu, no line showed its key, and the popup was placed two
different ways - one of them a header's height above the pointer.

The first half checks the builder's rules on plain widgets. The second half is
the guard, in the spirit of `test_button_system.py`: every menu a right-click
opens in `app/ui` - outside `contacts/`, `contacts_tools/` and `accounts/`,
which another branch owns - goes through the builder, and every line in it has
an icon.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt               # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QKeyEvent      # noqa: E402
from PySide6.QtWidgets import (                             # noqa: E402
    QApplication, QMenu, QPushButton, QTableWidget, QTableWidgetItem, QWidget,
)

from app.ui.widgets import menus                            # noqa: E402
from app.ui.widgets.menus import Item, build, glyph_for     # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "app" / "ui"
#: Review DS-6 (2026-10-14): the branch that owned these has landed, so the guard sees them too.
NOT_OURS: tuple[str, ...] = ()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _lines(menu: QMenu) -> list:
    return [action for action in menu.actions() if not action.isSeparator()]


# ---------------------------------------------------------------------------
# The builder's rules
# ---------------------------------------------------------------------------

def test_every_line_has_an_icon_and_the_default_is_first_and_bold(qapp) -> None:
    parent = QWidget()
    menu = build(parent, [
        Item("Copy path", lambda: None, group="copy", shortcut="Ctrl+C"),
        Item("Show in folder", lambda: None, group="open"),
        Item("Open", lambda: None, group="open", default=True, shortcut="Return"),
        Item("Pin", lambda: None, group="remember"),
        Item("More like this", lambda: None),
    ])
    lines = _lines(menu)
    assert [a.text() for a in lines] == [
        "Open", "Show in folder", "More like this", "Copy path", "Pin"]
    assert all(not a.icon().isNull() for a in lines), [a.text() for a in lines]
    assert menu.defaultAction() is lines[0] and lines[0].font().bold()
    assert not any(a.font().bold() for a in lines[1:])
    assert menu.property(menus.BUILT_PROPERTY) is True


def test_groups_are_parted_by_separators_and_the_destructive_line_comes_last(qapp) -> None:
    parent = QWidget()
    menu = build(parent, [
        Item("Forget this person…", lambda: None, group="danger", shortcut="Del"),
        Item("Name this person…", lambda: None, group="act", shortcut="F2", default=True),
        Item("Manage the faces in this pile…", lambda: None),
    ])
    kinds = ["-" if a.isSeparator() else a.text() for a in menu.actions()]
    assert kinds == ["Name this person…", "Manage the faces in this pile…", "-",
                     "Forget this person…"]


def test_a_line_shows_its_key_but_an_open_menu_keeps_its_keys_for_itself(qapp) -> None:
    """Shown, never bound: Enter in an open menu chooses the highlighted line, not "Open"."""
    parent = QWidget()
    fired = []
    menu = build(parent, [Item("Open", lambda: fired.append("open"), group="open",
                               default=True, shortcut="Return")])
    [line] = _lines(menu)
    assert line.shortcut().toString() == "Return" and line.isShortcutVisibleInContextMenu()
    override = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Return,
                         Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(menu, override)
    assert override.isAccepted()
    assert fired == []


def test_a_line_with_no_icon_anywhere_is_refused(qapp) -> None:
    parent = QWidget()
    with pytest.raises(ValueError, match="no icon"):
        build(parent, [Item("Frobnicate the widget", lambda: None)])


def test_words_never_change_and_a_line_runs_its_action(qapp) -> None:
    parent = QWidget()
    ran = []
    menu = build(parent, [Item("Find copies of this…", lambda: ran.append(1))])
    [line] = _lines(menu)
    assert line.text() == "Find copies of this…"
    line.trigger()
    assert ran == [1]


def test_the_icon_table_agrees_with_the_button_table_and_every_glyph_ships() -> None:
    """Rule 1: the same action is the same picture as a menu line and as a button."""
    from app.ui.widgets.buttons import BUTTONS
    from app.ui.widgets.icons import ICON_NAMES, icon_file

    for words, glyph in list(menus.MENU_ICONS.items()) + list(menus.MENU_PREFIXES.items()):
        assert glyph in ICON_NAMES and icon_file(glyph) is not None, words
        if words in BUTTONS:
            assert BUTTONS[words][0] == glyph, (
                f"{words!r} is {glyph} in a menu and {BUTTONS[words][0]} on a button")
    # Words a button already has are not repeated: the button's icon is used.
    assert glyph_for("Show in folder") == BUTTONS["Show in folder"][0]
    assert glyph_for("Open") == BUTTONS["Open"][0]


# ---------------------------------------------------------------------------
# One way to place the popup
# ---------------------------------------------------------------------------

def _table() -> QTableWidget:
    table = QTableWidget(12, 2)
    for row in range(12):
        table.setItem(row, 0, QTableWidgetItem(f"row {row}"))
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    table.resize(320, 320)
    table.show()
    QApplication.processEvents()
    return table


def test_a_right_click_on_a_list_arrives_in_viewport_coordinates(qapp) -> None:
    """Qt's documented exception for QAbstractScrollArea, checked rather than assumed.

    A real context-menu event on the viewport, over row 2: the signal's point is
    the viewport's own, so `rowAt` finds row 2 with it as it is - and the old
    `viewport_point` mapping found the row above.
    """
    table = _table()
    try:
        centre = table.visualRect(table.model().index(2, 0)).center()
        got = []
        table.customContextMenuRequested.connect(got.append)
        QApplication.sendEvent(table.viewport(), QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, centre, table.viewport().mapToGlobal(centre)))
        assert got == [centre]
        assert table.rowAt(got[0].y()) == 2
        assert menus.global_point(table, got[0]) == table.viewport().mapToGlobal(centre)
        assert table.viewport().mapTo(table, QPoint(0, 0)).y() > 0, (
            "no header above the rows: this would not tell the two conventions apart")
    finally:
        table.deleteLater()


def test_a_plain_widget_maps_through_itself(qapp) -> None:
    button = QPushButton("x")
    button.resize(80, 30)
    button.show()
    try:
        assert menus.global_point(button, QPoint(4, 5)) == button.mapToGlobal(QPoint(4, 5))
    finally:
        button.deleteLater()


def test_the_old_widget_mapping_helper_is_gone() -> None:
    """`viewport_point` mapped an already-viewport point a second time."""
    from app.ui.widgets import file_menu

    assert not hasattr(file_menu, "viewport_point")
    offenders = [str(path.relative_to(ROOT)) for path in _ui_files()
                 if "viewport_point" in path.read_text(encoding="utf-8")]
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# The guard: every right-click menu goes through the builder
# ---------------------------------------------------------------------------

def _ui_files() -> list[Path]:
    return [path for path in sorted(UI.rglob("*.py"))
            if not any(part in NOT_OURS for part in path.relative_to(UI).parts)]


#: Reacts to a right-click.
_RIGHT_CLICK = re.compile(r"customContextMenuRequested|QEvent\.Type\.ContextMenu|def contextMenuEvent")
#: Builds a menu by hand.
_HAND_BUILT = re.compile(r"QMenu\(|\.addMenu\(|\.addSeparator\(|\.addAction\(")

#: **The menus that are not a right-click on a row, and why.** Counted, so a new
#: hand-built menu in one of these files is caught as surely as one anywhere else.
NOT_A_RIGHT_CLICK: dict[str, tuple[int, str]] = {
    "app/ui/shell.py": (10, "the window's menu bar"),
    "app/ui/tray.py": (13, "the system tray's own menu, the taskbar's convention"),
    "app/ui/view_options.py": (10, "the View menu: tick boxes and a size row, a setting not an action; "
                                   "the table's header opens it too"),
    "app/ui/widgets/model_picker.py": (2, "a drop-down of models"),
    "app/ui/widgets/search_bar.py": (6, "the More button's drop-down and the scope menu"),
    "app/ui/widgets/side_panel.py": (1, "a line added to the View menu"),
    "app/ui/widgets/photo_page_parts.py": (9, "the Photos tab's View menu"),
}


@pytest.mark.slow
def test_no_file_builds_a_right_click_menu_by_hand() -> None:
    builder = UI / "widgets" / "menus.py"
    found: dict[str, int] = {}
    for path in _ui_files():
        if path == builder:
            continue
        text = path.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines()
                         if not line.strip().startswith("#"))
        count = len(_HAND_BUILT.findall(code))
        relative = path.relative_to(ROOT).as_posix()
        if relative in NOT_A_RIGHT_CLICK or (count and _RIGHT_CLICK.search(code)):
            found[relative] = count
        elif "QMenu(" in code:
            found[relative] = count
    expected = {path: count for path, (count, _why) in NOT_A_RIGHT_CLICK.items()}
    assert found == expected, (
        "a right-click menu built by hand - build it with app/ui/widgets/menus.py "
        "(or file_menu.build_menu for a file); a menu that is not a right-click "
        "goes in NOT_A_RIGHT_CLICK with its reason")


# ---------------------------------------------------------------------------
# Review DS-6 (2026-10-14): a button's drop-down is built by the builder too
# ---------------------------------------------------------------------------

#: **Every hand-built `QMenu` left in `app/ui`, and why** - counted per file, across the
#: whole tree (contacts and accounts included). Anything else goes through `menus.attach`,
#: `menus.kept` or `menus.build`.
QMENU_ALLOWED: dict[str, tuple[int, str]] = {
    "app/ui/tray.py": (2, "the system tray's own menu and its Pause submenu: the taskbar's "
                          "convention, drawn by Windows, not the window's theme"),
    "app/ui/view_options.py": (1, "the View menu: tick boxes in titled sections and a Size row "
                                  "(a widget), a setting not an action"),
    "app/ui/widgets/photo_page_parts.py": (2, "the Photos tab's View menu, and the copy the "
                                              "window's View menu borrows"),
    "app/ui/widgets/search_bar.py": (1, "the Search toolbar's More menu: it holds the toolbar's own "
                                        "switches (shared QActions) and the model submenu"),
    "app/ui/widgets/model_picker.py": (1, "ModelMenu, the More menu's submenu of models, a QMenu "
                                          "subclass that rebuilds its ticked choices"),
}

_QMENU = re.compile(r"\bQMenu\(|\(QMenu\)")


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.strip().startswith("#"))


def test_no_menu_anywhere_is_hand_built_outside_the_allowlist() -> None:
    builder = UI / "widgets" / "menus.py"
    found = {}
    for path in sorted(UI.rglob("*.py")):
        if path == builder:
            continue
        count = len(_QMENU.findall(_code(path)))
        if count:
            found[path.relative_to(ROOT).as_posix()] = count
    expected = {path: count for path, (count, _why) in QMENU_ALLOWED.items()}
    assert found == expected, (
        "a menu built by hand - a button's drop-down is menus.attach(button, items), a menu "
        "a view pops up itself is menus.kept(parent, items); only QMENU_ALLOWED may stay")


def test_attach_gives_a_button_a_built_menu_with_an_icon_on_every_line(qapp) -> None:
    button = QPushButton("Tick")
    ran = []
    menu = menus.attach(button, [Item("All", lambda: ran.append("all"), icon="list-checks"),
                                 Item("None", lambda: ran.append("none"), icon="list-x")])
    assert button.menu() is menu and menu.property(menus.BUILT_PROPERTY) is True
    assert [a.text() for a in _lines(menu)] == ["All", "None"], "the order given"
    assert all(not a.icon().isNull() for a in _lines(menu))
    _lines(menu)[1].trigger()
    assert ran == ["none"]


def test_a_right_click_built_later_leaves_an_attached_menu_alone(qapp) -> None:
    button = QPushButton("More")
    menu = menus.attach(button, [Item("Open", lambda: None)])
    build(button, [Item("Pin", lambda: None)])
    build(button, [Item("Pin", lambda: None)])   # the second tidies away the first's leftovers
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert button.menu() is menu and _lines(menu)[0].text() == "Open"


def test_refill_changes_the_lines_and_lines_finds_them_by_key(qapp) -> None:
    button = QPushButton("Accept all")
    menu = menus.attach(button)
    assert _lines(menu) == []
    menus.refill(menu, [Item("Everyone (2)", lambda: None, icon="list-checks"),
                        Item("Jason (2)", lambda: None, icon="user", group="remember", key="jason")])
    kinds = ["-" if a.isSeparator() else a.text() for a in menu.actions()]
    assert kinds == ["Everyone (2)", "-", "Jason (2)"]
    assert menus.lines(menu)["jason"].text() == "Jason (2)"


def test_choices_are_ticked_one_at_a_time(qapp) -> None:
    button = QPushButton("Sort")
    menu = menus.attach(button, [Item(w, lambda: None, icon="list", choice=True, checked=(w == "A"))
                                 for w in ("A", "B", "C")])
    found = menus.lines(menu)
    assert found["A"].isChecked() and found["A"].isCheckable()
    found["C"].trigger()
    assert found["C"].isChecked() and not found["A"].isChecked()
    menus.refill(menu, [Item("D", lambda: None, icon="list", choice=True, checked=True)])
    assert menus.lines(menu)["D"].isChecked()


def test_a_drop_down_follows_a_theme_switch_when_it_opens(qapp, monkeypatch) -> None:
    """Icons are pictures: a menu built before a theme switch kept the old colour."""
    button = QPushButton("Tick")
    menu = menus.attach(button, [Item("All", lambda: None, icon="list-checks")])
    painted = []
    monkeypatch.setattr(menus, "_colours", lambda: {"text_dim": "#123456"})
    real = menus._paint
    monkeypatch.setattr(menus, "_paint", lambda g, t, grp, c: painted.append(c["text_dim"]) or real(g, t, grp, c))
    menu.aboutToShow.emit()
    assert painted == ["#123456"]


def test_the_file_menu_is_built_by_the_builder_with_an_icon_on_every_line(qapp) -> None:
    from app.ui.widgets.file_menu import FileActions, build_menu

    parent = QWidget()
    menu = build_menu(parent, "C:/a/report.docx", FileActions(
        open_file=lambda: None, reveal=lambda: None, search_inside=lambda: None,
        index_file=lambda: None, pin=lambda: None, similar=lambda: None,
        explain=lambda: None, same_period=lambda: None, find_copies=lambda: None,
        extra=[("Ignore this repository", "tip", lambda: None)], reindex=lambda: None,
        missing=True))
    assert menu.property(menus.BUILT_PROPERTY) is True
    lines = _lines(menu)
    assert all(not a.icon().isNull() for a in lines), [a.text() for a in lines]
    assert lines[0].text() == "Open" and menu.defaultAction() is lines[0]
    assert lines[0].shortcut().toString() in ("Return", "Enter")
    copy = next(a for a in lines if a.text() == "Copy path")
    assert copy.shortcut().matches(Qt.KeyboardModifier.ControlModifier | Qt.Key.Key_C) \
        or copy.shortcut().toString() == "Ctrl+C"
    # Open and view first, then acting on it, copying, and remembering it.
    words = ["-" if a.isSeparator() else a.text() for a in menu.actions()]
    assert words.index("Show in folder") < words.index("Search inside this file") \
        < words.index("Copy path") < words.index("Pin")


@pytest.mark.slow
def test_every_written_menu_line_has_an_icon_before_anyone_right_clicks() -> None:
    """2026-10-14: People's "Seen once" pile raised `ValueError` on every
    right-click - "Look through these faces…" had no icon, and the builder
    refuses such a line only when the menu is built, so no test that never
    opened that menu saw it. Every `Item("...")` written in `app/ui` without
    its own `icon=` must find one in the tables now, not at the right-click."""
    import ast

    from app.ui.widgets.icons import ICON_NAMES

    missing = []
    for path in UI.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "Item" or not node.args:
                continue
            words = node.args[0]
            if not (isinstance(words, ast.Constant) and isinstance(words.value, str)):
                continue
            if any(keyword.arg == "icon" for keyword in node.keywords):
                continue
            if glyph_for(words.value) not in ICON_NAMES:
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno} {words.value!r}")
    assert not missing, "menu lines with no icon:\n" + "\n".join(missing)
