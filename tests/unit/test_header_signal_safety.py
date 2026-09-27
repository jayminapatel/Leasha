r"""Nothing Python may be connected to `sectionResized`. It kills the process.

Eight Windows access violations across 26-27 August 2026, every one at the
same offset in `python312.dll`, every crash dump naming the slot connected to
`QHeaderView.sectionResized` at an **unknown line** - the frame faulted on
entry, before a single bytecode ran.

Three fixes were tried inside the slot and none worked, because the slot never
runs: the fault is Qt calling into PyQt's glue from inside `QHeaderView`'s own
layout. The last dumps showed it reached straight from `application.exec()`,
with nothing of ours in between. `remember_widths` carries the full history.

So the invariant is structural and blunt - **no connection exists** - and a
timer reads the widths instead. These tests defend both halves: the ban, and
the feature the ban would otherwise have destroyed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QTimer                                 # noqa: E402
from PyQt6.QtWidgets import QTableWidgetItem                    # noqa: E402

from app.ui.view_options import (                               # noqa: E402
    APPLYING, ViewPreferences, apply_to_table, remember_widths,
)
from app.ui.widgets.result_table import ResultTable             # noqa: E402

APP = Path(__file__).resolve().parents[2] / "app"
COLUMNS = [("name", "Name"), ("size", "Size"), ("when", "Modified")]


class _Button:
    """Stands in for the view button, and records what it was told."""

    def __init__(self) -> None:
        self.saved: list = []

    def remember_width(self, key, width) -> None:
        self.saved.append((key, width))


def _wire(_qt_application, width: int = 900) -> tuple:
    """A table wired exactly as the four real views wire theirs."""
    del _qt_application
    table = ResultTable([h for _k, h in COLUMNS], ranked=True,
                        aligns=["left", "right", "left"])
    table.resize(width, 400)
    table.show()
    button = _Button()
    remember_widths(table, button, COLUMNS)
    timers = [child for child in table.children() if isinstance(child, QTimer)]
    assert timers, "remember_widths started no watcher"

    def fill(rows: int = 3, prefs=None) -> None:
        table.setRowCount(rows)
        for row in range(rows):
            for column in range(3):
                table.setItem(row, column, QTableWidgetItem(
                    f"cell {row}{column} wwwwwwwwww"))
        table.set_row_objects([object()] * rows)
        apply_to_table(table, prefs or ViewPreferences(), columns=COLUMNS,
                       available=[key for key, _h in COLUMNS])

    return table, button, timers[0].timeout.emit, fill


class TestNothingIsConnectedToSectionResized:
    r"""The ban, checked by parsing rather than reading.

    **Grepping for `sectionResized` would be useless here**: the word appears
    a dozen times in `view_options.py`, in the docstring that explains why
    nothing may connect to it. A guard that trips on its own justification
    gets deleted the first time somebody hits it. This looks for what the code
    would *do* - an attribute call `<something>.sectionResized.connect(...)` -
    which no amount of prose can contain.
    """

    def _connections(self, path: Path) -> list:
        found: list = []
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if not isinstance(target, ast.Attribute) or target.attr != "connect":
                continue
            inner = target.value
            if isinstance(inner, ast.Attribute) and inner.attr == "sectionResized":
                found.append(f"{path.name}:{node.lineno}")
        return found

    def test_no_module_connects_to_it(self) -> None:
        offenders: list = []
        for path in sorted(APP.rglob("*.py")):
            offenders.extend(self._connections(path))
        assert offenders == [], (
            f"sectionResized is connected at {offenders}. This is the access "
            "violation of 2026-08-27 - read the note in remember_widths "
            "before reconnecting it.")

    def test_the_detector_can_actually_see_one(self, tmp_path) -> None:
        """Otherwise the test above passes on an empty search forever."""
        bait = tmp_path / "bait.py"
        bait.write_text("header.sectionResized.connect(slot)\n", encoding="utf-8")
        assert self._connections(bait), "the detector finds nothing"


class TestADraggedWidthIsStillRemembered:
    """The ban is only acceptable if the feature survives it."""

    def test_a_settled_drag_is_saved(self, _qt_application) -> None:
        table, button, tick, fill = _wire(_qt_application)
        fill()
        for _ in range(3):
            tick()
        assert button.saved == [], "a fill was mistaken for a drag"

        table.horizontalHeader().resizeSection(0, 321)
        tick()
        assert button.saved == [], "saved while the drag was still moving"
        tick()
        assert ("name", 321) in button.saved, "a finished drag was not saved"

    def test_it_is_saved_once(self, _qt_application) -> None:
        table, button, tick, fill = _wire(_qt_application)
        fill()
        table.horizontalHeader().resizeSection(0, 321)
        for _ in range(5):
            tick()
        assert button.saved.count(("name", 321)) == 1

    def test_dragging_only_the_last_column_still_counts(
            self, _qt_application) -> None:
        r"""The stretched column is skipped *only* when something else moved.

        Refusing it outright would mean the last column could never be sized,
        because `setStretchLastSection` stays on until a width is stored.
        """
        table, button, tick, fill = _wire(_qt_application)
        fill()
        for _ in range(3):
            tick()
        header = table.horizontalHeader()
        last = max(index for index in range(header.count())
                   if not table.isColumnHidden(index))
        header.resizeSection(last, 260)
        tick()
        tick()
        assert button.saved, "the last column could not be sized at all"


class TestNothingElseIsMistakenForAChoice:

    def test_a_fill_saves_nothing(self, _qt_application) -> None:
        r"""The original bug this whole area exists around.

        Widths are fitted to content on every fill. Recording those would pin
        every column at whatever the first result set happened to need.
        """
        table, button, tick, fill = _wire(_qt_application)
        for rows in (3, 5, 8):
            fill(rows)
            for _ in range(3):
                tick()
        assert button.saved == []

    def test_a_window_resize_saves_nothing(self, _qt_application) -> None:
        """`setStretchLastSection` moves a column; nobody chose that width."""
        table, button, tick, fill = _wire(_qt_application)
        fill()
        for _ in range(3):
            tick()

        table.resize(600, 400)
        for _ in range(3):
            tick()
        assert button.saved == []

    def test_a_stretched_last_column_is_not_pinned_by_another_drag(
            self, _qt_application) -> None:
        """Narrowing column one widens the last. Only one was dragged."""
        table, button, tick, fill = _wire(_qt_application)
        fill()
        for _ in range(3):
            tick()
        table.horizontalHeader().resizeSection(0, 321)
        tick()
        tick()
        assert [key for key, _w in button.saved] == ["name"], (
            f"the stretched column was recorded too: {button.saved}")


class TestTheWatcherIsSafeToLeaveRunning:
    """It ticks forever, unattended, for the life of the window."""

    def test_it_is_parented_to_the_table(self, _qt_application) -> None:
        """So it dies with the table instead of reading a deleted widget."""
        table, _button, _tick, _fill = _wire(_qt_application)
        timers = [child for child in table.children() if isinstance(child, QTimer)]
        assert timers and timers[0].parent() is table

    def test_a_tick_during_an_apply_records_nothing(self, _qt_application) -> None:
        table, button, tick, fill = _wire(_qt_application)
        fill()
        table.setProperty(APPLYING, True)
        table.horizontalHeader().resizeSection(0, 321)
        tick()
        tick()
        table.setProperty(APPLYING, False)
        assert button.saved == []

    def test_apply_to_table_resyncs_the_baseline(self, _qt_application) -> None:
        """Without the resync the watcher records the next fitted width."""
        table, _button, _tick, fill = _wire(_qt_application)
        fill()
        assert callable(getattr(table, "leasha_resync_widths", None)), (
            "apply_to_table has nothing to resync against")


class TestALetGoViewTakesItsWatcherWithIt:
    r"""The native crash between two tests (2026-09, no Python exception).

    A view somebody had let go of stayed alive - view, View button, the
    button's `on_change` closure, view: a reference cycle - so its width
    watcher kept ticking on tables nobody owned until the *cyclic garbage
    collector* found it. The collector deletes the C++ widget wherever an
    allocation happens to set it off, which can be inside another widget's
    timer during an event-loop turn. Measured: a dropped view was still alive,
    and its timer had fired 15 times in 300ms, until `gc.collect()`.

    These run with the collector off, so "deleted at once" means by reference
    count and cannot be rescued by a lucky collection.
    """

    @staticmethod
    def _pump(seconds: float) -> None:
        import time

        from PyQt6.QtWidgets import QApplication

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.005)

    def test_a_dropped_view_is_deleted_at_once_and_its_timer_stops(
            self, _qt_application) -> None:
        import gc
        import weakref

        from PyQt6.QtWidgets import QVBoxLayout, QWidget

        from app.ui import view_options

        class View(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.table = ResultTable([h for _k, h in COLUMNS], ranked=True,
                                         aligns=["left", "right", "left"])
                QVBoxLayout(self).addWidget(self.table)
                # A bound method, as `files_view`, `mail_view` and `code_view` pass.
                self.chooser = view_options.button(
                    self, None, "t", columns=COLUMNS,
                    on_change=self._prefs_changed, table=self.table)

            def _prefs_changed(self, _prefs) -> None:
                pass

        ticks: list = []
        gc.collect()
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            view = View()
            view.show()
            timer = [c for c in view.table.children() if isinstance(c, QTimer)][0]
            timer.setInterval(10)
            timer.timeout.connect(lambda: ticks.append(1))
            self._pump(0.1)
            assert ticks, "this test's own probe never ticked - it proves nothing"

            alive = weakref.ref(view)
            del view, timer
            assert alive() is None, (
                "a dropped view is still alive - something in it is a reference "
                "cycle, so it waits for the cyclic collector, timers running")
            before = len(ticks)
            self._pump(0.15)
            assert len(ticks) == before, "the watcher ticked on a dropped view"
        finally:
            if was_enabled:
                gc.enable()
        gc.collect()                       # and the collector then finds nothing to delete
        self._pump(0.05)

    def test_the_watcher_does_not_keep_its_table_alive(self, _qt_application) -> None:
        import gc
        import weakref

        from PyQt6.QtWidgets import QTableWidget

        was_enabled = gc.isenabled()
        gc.disable()
        try:
            # A plain QTableWidget: `ResultTable` has a cycle of its own that is
            # nothing to do with the watcher, and would hide this one.
            table = QTableWidget(2, 3)
            remember_widths(table, _Button(), COLUMNS)
            alive = weakref.ref(table)
            del table
            assert alive() is None, (
                "the width watcher's closures hold the table strongly: "
                "table -> leasha_resync_widths -> closure -> table")
        finally:
            if was_enabled:
                gc.enable()


#: Run in a child process, because before the fix it did not fail - it
#: segfaulted, and would have taken the whole test run down with it.
_ORPHANED_WATCHER = '''
import gc, time
from PyQt6 import sip
from PyQt6.QtWidgets import QApplication, QTableWidget
from app.ui import view_options

class Button:
    def remember_width(self, *_a):
        pass

app = QApplication([])
kept = []
for _ in range(5):
    table = QTableWidget(2, 3)
    view_options.remember_widths(table, Button(), [("a", "A"), ("b", "B"), ("c", "C")])
    sip.transferto(table, None)          # C++ owns it: it outlives its Python side
    kept.append(sip.unwrapinstance(table))
    del table
gc.collect()                             # the watcher's Python side is now garbage
end = time.monotonic() + 1.5
while time.monotonic() < end:            # two ticks of the 600ms watcher, at least
    app.processEvents()
    time.sleep(0.01)
print("survived")
'''


def test_a_watcher_whose_python_side_was_collected_does_not_crash_on_its_next_tick(
        tmp_path) -> None:
    """The native crash of 2026-09-27 (`view_options._WATCHERS` has the story).

    A table that lives on in C++ after its Python wrapper is dropped keeps its
    width timer ticking. The collector found `look` and its timer in an
    unreachable cycle and cleared the function, and the next tick called a
    function with no globals: exit -11, in whichever test was running. A core
    dump from the full suite named the frame. Before the fix this script
    ended with signal 11; it must now finish and say so.
    """
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    script = tmp_path / "orphaned_watcher.py"
    script.write_text(_ORPHANED_WATCHER, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(root))
    done = subprocess.run([sys.executable, str(script)], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)

    assert done.returncode == 0 and "survived" in done.stdout, (
        f"exit {done.returncode}: a collected width watcher still ticked into "
        f"a cleared closure\n{done.stderr[-2000:]}")
