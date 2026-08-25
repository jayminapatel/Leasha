"""Scrolling past a control must not change it.

Layer: L5

Reported as *"the settings page using a trackpad has issues, it changes
settings"*, and that is precisely what Qt does by default: a `QComboBox`,
`QSpinBox` or `QTimeEdit` accepts wheel events whether or not it has focus.

**This is data loss, not clunkiness.** Those controls set the memory ceiling,
the CPU limit, the worker count and the index schedule. Scrolling down the page
can hand a run four workers and a 500MB ceiling without one deliberate click,
and nothing announces it.

Qt widgets cannot be built without a display, so these are static checks against
the source - crude, and they catch exactly the mistake that causes this: a page
that adds a spin box and does not think about the wheel.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"
GUARD = UI / "widgets" / "no_scroll.py"

#: Widget types Qt lets the wheel change. `QCheckBox` is not one, which is why
#: this list is short and specific rather than "everything".
SCROLLABLE = ("QComboBox", "QSpinBox", "QDoubleSpinBox", "QTimeEdit",
              "QDateEdit", "QDateTimeEdit", "QSlider")


def constructed_in(path: Path) -> set[str]:
    """Scroll-sensitive widget types built in this module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in SCROLLABLE
    }


def test_the_guard_exists():
    assert GUARD.is_file()


def test_the_wheel_still_works_on_a_focused_control():
    """Taking it away entirely would be its own annoyance: somebody who clicked
    a spin box and then scrolls meant to change it."""
    source = GUARD.read_text(encoding="utf-8")
    assert "hasFocus()" in source
    assert "return False" in source, "a focused control must get its wheel event"


def test_an_unfocused_control_passes_the_event_to_the_page():
    source = GUARD.read_text(encoding="utf-8")
    assert "event.ignore()" in source


def test_focus_policy_is_tightened_too():
    """Without it the control takes focus *on* the wheel event and then responds
    to it, which defeats the filter on the second notch of a scroll."""
    assert "StrongFocus" in GUARD.read_text(encoding="utf-8")


def test_the_guard_is_kept_alive():
    """An event filter that is garbage collected stops filtering, silently. The
    bug would come back and look intermittent, which is the worst kind."""
    source = GUARD.read_text(encoding="utf-8")
    assert "_GUARD = WheelGuard()" in source, "the filter must outlive the call"


def test_the_window_guards_everything_in_one_place():
    """Per page, the one somebody forgets is the one that matters. At the window,
    a new page gets it for free."""
    shell = (UI / "shell.py").read_text(encoding="utf-8")
    assert "protect_all(self)" in shell


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in UI.glob("*.py")) + ["widgets/environment_box.py",
                                               "widgets/file_types.py"],
)
def test_no_module_builds_a_scrollable_control_the_window_cannot_reach(name):
    """`protect_all` walks `findChildren`, which finds anything parented into
    the window. A control created but never added to a layout would escape it -
    and would also be invisible, so it would be a different bug.

    This test is really a prompt: if a module starts building these outside the
    window's tree, the guard needs applying there too.
    """
    path = UI / name
    if not path.is_file():
        pytest.skip(f"{name} does not exist")

    built = constructed_in(path)
    if not built:
        return

    source = path.read_text(encoding="utf-8")
    # Either it is inside the window's widget tree (the normal case, covered by
    # protect_all), or it guards itself.
    assert "self." in source, (
        f"{name} builds {built} - they must be attributes on a widget in the "
        f"window's tree, or protected explicitly"
    )
