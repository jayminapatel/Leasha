r"""The keyboard's ring round the current row of every tree and table (`widgets/row_ring.py`).

Layer: L5. Review 2026-10-14 follow-up to DS-2: a sheet rule can only ring one cell, so the
tables and multi-column trees showed no place for somebody using the arrow keys. One filter,
`install_row_ring(view)`, rings the whole current row while the view has the keyboard.

Offscreen Qt for what is drawn (focus is stood in for by `hasFocus`, which the filter asks);
a scan of `app/ui` for the guard that every tree and table built there gets the ring.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


def _near(found, wanted, slack: int = 40) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(found.getRgb()[:3], wanted.getRgb()[:3]))


def _ring():
    from app.ui.widgets.paint_kit import tok

    return tok(None, "focus_ring")


def _tree(columns: int = 3):
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

    from app.ui.widgets.row_ring import install_row_ring

    tree = QTreeWidget()
    tree.setColumnCount(columns)
    for i in range(4):
        tree.addTopLevelItem(QTreeWidgetItem([f"row {i}"] + ["x"] * (columns - 1)))
    tree.resize(320, 200)
    install_row_ring(tree)
    tree.setCurrentItem(tree.topLevelItem(1))
    return tree


def _table(behaviour: str = "SelectRows"):
    from PySide6.QtWidgets import QAbstractItemView, QTableWidget, QTableWidgetItem

    from app.ui.widgets.row_ring import install_row_ring

    table = QTableWidget(4, 3)
    for r in range(4):
        for c in range(3):
            table.setItem(r, c, QTableWidgetItem(f"{r},{c}"))
    table.setSelectionBehavior(getattr(QAbstractItemView.SelectionBehavior, behaviour))
    table.resize(360, 200)
    install_row_ring(table)
    table.setCurrentCell(1, 1)
    return table


def _shot(view, focused: bool, monkeypatch):
    monkeypatch.setattr(view, "hasFocus", lambda: focused)
    return view.viewport().grab().toImage()


def _ends(view):
    """The left and right edge pixels of the current row's ring, and its middle height."""
    from app.ui.widgets.row_ring import ring_rect

    rect = ring_rect(view, view.currentIndex())
    return rect.left() + 2, rect.right() - 2, rect.center().y()


@pytest.mark.parametrize("make", [_tree, _table], ids=["tree", "table"])
def test_the_whole_current_row_is_ringed_when_the_view_has_the_keyboard(make, qapp, monkeypatch):
    view = make()
    image = _shot(view, True, monkeypatch)
    left, right, y = _ends(view)
    assert _near(image.pixelColor(left, y), _ring())
    assert _near(image.pixelColor(right, y), _ring())          # the far end too: the row, not a cell


@pytest.mark.parametrize("make", [_tree, _table], ids=["tree", "table"])
def test_no_ring_without_the_keyboard(make, qapp, monkeypatch):
    view = make()
    image = _shot(view, False, monkeypatch)
    left, right, y = _ends(view)
    assert not _near(image.pixelColor(left, y), _ring(), slack=10)
    assert not _near(image.pixelColor(right, y), _ring(), slack=10)


def test_only_the_current_row_is_ringed(qapp, monkeypatch):
    from app.ui.widgets.row_ring import ring_rect

    tree = _tree()
    other = tree.model().index(3, 0)
    rect = ring_rect(tree, other)
    image = _shot(tree, True, monkeypatch)
    assert not _near(image.pixelColor(rect.left() + 2, rect.center().y()), _ring(), slack=10)
    tree.setCurrentIndex(other)                                 # the ring follows the keyboard
    image = _shot(tree, True, monkeypatch)
    assert _near(image.pixelColor(rect.left() + 2, rect.center().y()), _ring())


def test_without_the_keyboard_the_view_looks_exactly_as_before(qapp, monkeypatch):
    """Hover and selection are the view's own: the filter adds a ring and nothing else."""
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

    def plain():
        tree = QTreeWidget()
        tree.setColumnCount(3)
        for i in range(4):
            tree.addTopLevelItem(QTreeWidgetItem([f"row {i}", "x", "x"]))
        tree.resize(320, 200)
        tree.setCurrentItem(tree.topLevelItem(1))
        return tree

    ringed = _tree()
    bare = plain()
    monkeypatch.setattr(bare, "hasFocus", lambda: False)
    assert _shot(ringed, False, monkeypatch) == bare.viewport().grab().toImage()


def test_a_table_that_moves_cell_by_cell_rings_the_cell(qapp, monkeypatch):
    from app.ui.widgets.row_ring import ring_rect

    table = _table("SelectItems")
    cell = table.visualRect(table.currentIndex())
    assert ring_rect(table, table.currentIndex()) == cell
    image = _shot(table, True, monkeypatch)
    assert _near(image.pixelColor(cell.left() + 2, cell.center().y()), _ring())
    first = table.visualRect(table.model().index(1, 0))
    assert not _near(image.pixelColor(first.left() + 2, first.center().y()), _ring(), slack=10)


def test_a_side_list_rings_its_current_line_through_the_same_filter(qapp, monkeypatch):
    from app.ui.presenter.side_entry import SideEntry
    from app.ui.widgets.row_ring import has_row_ring
    from app.ui.widgets.side_list import SideList

    side = SideList("testSide", accessible="Test")
    side.resize(220, 200)
    side.set_entries([SideEntry(key="a", label="Alpha"), SideEntry(key="b", label="Beta")])
    side.setCurrentItem(side.topLevelItem(0))
    assert has_row_ring(side)
    left, _right, y = _ends(side)
    assert _near(_shot(side, True, monkeypatch).pixelColor(left, y), _ring())
    assert not _near(_shot(side, False, monkeypatch).pixelColor(left, y), _ring(), slack=10)


def test_installing_twice_hangs_one_filter(qapp):
    from app.ui.widgets.row_ring import install_row_ring

    tree = _tree()
    first = tree._leasha_row_ring
    install_row_ring(tree)
    assert tree._leasha_row_ring is first


# ---------------------------------------------------------------------------
# The guard: every tree and table built in app/ui is ringed
# ---------------------------------------------------------------------------

_VIEWS = {"QTreeWidget", "QTreeView", "QTableWidget", "QTableView"}

#: Trees and tables built in app/ui that are not ringed, and why. Never grows without a reason.
_NO_RING_OK: dict[str, str] = {}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _unringed(source: str) -> list[str]:
    """`line:target` for each tree or table `source` builds without `install_row_ring` on it."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    ringed: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and _name(node.func) == "install_row_ring" and node.args):
            ringed.add(ast.unparse(node.args[0]))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_name(b) in _VIEWS for b in node.bases):
            body = ast.unparse(node)
            if "install_row_ring(self" not in body:
                found.append(f"{node.lineno}:class {node.name}")
        if isinstance(node, ast.Call) and _name(node.func) in _VIEWS:
            holder = parents.get(node)
            if isinstance(holder, (ast.Assign, ast.AnnAssign)):
                targets = holder.targets if isinstance(holder, ast.Assign) else [holder.target]
                if any(ast.unparse(t) in ringed for t in targets):
                    continue
                found.append(f"{node.lineno}:{ast.unparse(targets[0])}")
            else:
                found.append(f"{node.lineno}:{_name(node.func)}(...)")
    return found


@pytest.mark.slow
def test_every_tree_and_table_built_in_the_ui_is_ringed():
    loose = []
    for path in sorted(UI.rglob("*.py")):
        rel = path.relative_to(UI).as_posix()
        for spot in _unringed(path.read_text(encoding="utf-8")):
            line, target = spot.split(":", 1)
            if f"{rel}:{target}" not in _NO_RING_OK:
                loose.append(f"{rel}:{line} {target}")
    assert not loose, "a tree or table with no keyboard ring - call row_ring.install_row_ring: " + ", ".join(loose)


def test_the_ring_guard_would_notice_each_fault():
    source = ("class T(QTableWidget):\n"
              "    def __init__(self):\n"
              "        super().__init__()\n"
              "def make():\n"
              "    t = QTreeWidget()\n"
              "    u = QTableView()\n"
              "    install_row_ring(u)\n"
              "    lay.addWidget(QTableWidget())\n")
    assert _unringed(source) == ["1:class T", "5:t", "8:QTableWidget(...)"] or \
        sorted(_unringed(source)) == sorted(["1:class T", "5:t", "8:QTableWidget(...)"])


def test_every_allowance_has_a_reason():
    assert all(reason.strip() for reason in _NO_RING_OK.values())
