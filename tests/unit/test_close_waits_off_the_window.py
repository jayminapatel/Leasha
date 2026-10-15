r"""Closing waits for the indexing child off the window's thread.

Layer: L5

**What happened, 2026-10-14 06:47.** The lag monitor caught the window's thread
5 s inside `subprocess.wait`: `closeEvent` -> `end_all_children` ->
`ChildIndexRun.shutdown` -> `_force_end`. The child was given its grace, then
terminated, and the window served nothing for all of it - the owner's rule
(2026-10-15) is that the window never freezes or goes "Not responding".

**The wait stays; it moves.** The stores must not close while a child can still
write to them (work order 0x section 2c), so the close still waits for every
child to end. `close_windows.wait_off_window` runs that wait on a thread of its
own and keeps paint and timer events served on the window's.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer  # noqa: E402


def test_timers_keep_firing_while_the_close_waits_for_a_child(qapp):
    from app.ui.close_windows import wait_off_window

    ticks = []
    beat = QTimer()
    beat.setInterval(20)
    beat.timeout.connect(lambda: ticks.append(time.monotonic()))
    beat.start()
    try:
        ended = threading.Event()

        def slow_child_end() -> int:
            time.sleep(0.6)                          # a child given its grace
            ended.set()
            return 1

        assert wait_off_window(slow_child_end) == 1
    finally:
        beat.stop()

    assert ended.is_set(), "returned before the child had ended - the stores would close over it"
    # 600 ms of waiting at a 20 ms beat: blocked, it is 0 or 1 (the beat runs only after the wait).
    assert len(ticks) >= 10, f"the window's timers fired {len(ticks)} times during a 600 ms wait"


def test_the_wait_is_on_another_thread_and_its_error_comes_back(qapp):
    from app.ui.close_windows import wait_off_window

    seen = []

    def failing() -> None:
        seen.append(threading.current_thread() is threading.main_thread())
        raise RuntimeError("the child would not end")

    with pytest.raises(RuntimeError, match="would not end"):
        wait_off_window(failing)
    assert seen == [False]


def _stage_actions(function) -> dict[str, str]:
    """`stage("name", action)` in a function's source: the action's source text, by name."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    out = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "stage"
                and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)):
            out[node.args[0].value] = ast.unparse(node.args[1])
    return out


def test_close_ends_the_child_processes_off_the_windows_thread():
    from app.ui.shell import MainWindow

    actions = _stage_actions(MainWindow.closeEvent)
    for name, ender in (("index process", "end_all_children"),
                        ("folder watch process", "end_all_watch_children")):
        assert name in actions, f"closeEvent has no {name!r} stage any more; has it moved?"
        assert "wait_off_window" in actions[name] and ender in actions[name], (
            f"closeEvent's {name!r} stage runs {actions[name]} on the window's thread: "
            "a child's grace and termination freeze the window for seconds")
