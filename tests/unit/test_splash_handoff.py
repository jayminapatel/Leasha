"""The splash hands over the moment the window is up (order 0r 2b, 2026-09-29).

Measured on the owner's display: the splash closed only after the deferred
pages had been built and the vector store had started connecting, so it sat over
a finished window for 3-4 s on a warm start and about 5 s on a cold one. The
hand-off now comes straight after `window.show()`, and the deferred pages are
held until it is done - the splash's hold and fade pump events, and building
Settings inside that pump would stall the fade half-way.

Read from `_run_window`'s source, the way `test_startup_import_order` reads it:
the order of five calls is the whole of the fix, and running the real start-up
would need a display, a single-instance lock and two models.
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "app" / "main.py"


def _run_window() -> ast.FunctionDef:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_window":
            return node
    raise AssertionError("_run_window not found in app/main.py")


def _call_lines(function: ast.FunctionDef, owner: str, method: str) -> list[int]:
    return sorted(
        node.lineno for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == method and isinstance(node.func.value, ast.Name)
        and node.func.value.id == owner)


def _only(function: ast.FunctionDef, owner: str, method: str) -> int:
    lines = _call_lines(function, owner, method)
    assert len(lines) == 1, f"expected one {owner}.{method}() in _run_window, found {lines}"
    return lines[0]


def test_the_splash_hands_over_straight_after_the_window_shows() -> None:
    run = _run_window()
    show = _only(run, "window", "show")
    hold = _only(run, "window", "hold_deferred_start")
    release = _only(run, "window", "release_deferred_start")
    warm = _only(run, "vectors", "warm")
    # The early-return path (another copy already running) closes the splash
    # too; the hand-off is the one after the window is shown.
    handoff = [line for line in _call_lines(run, "splash", "hide_and_close") if line > show]

    assert hold < show, "the deferred pages must be held before anything pumps events"
    assert len(handoff) == 1, f"one hand-off after show(), found lines {handoff}"
    assert show < handoff[0] < release, "hand over, then let the pages build"
    assert release < warm, "the vector connect starts after the hand-off, not under the fade"
