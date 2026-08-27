r"""Every control says what it does, not what it is called.

Layer: L5. §6a of the search-experience order: *every control's tooltip states
its effect — what pressing it does and what happens at the limit, in plain
words.* This is the test that keeps it true.

**Already the house style, and that is why this is worth pinning.** Measured
before the guard existed: **116 controls, 103 of them already carrying a
tooltip**. The thirteen without were all text boxes, twelve of which have a
placeholder - which is better than a tooltip, because it is visible without
hovering and reachable on a touch screen. Exactly one control in the window
had neither, and it now has one.

**A placeholder counts, and demanding both would make the window worse.** A
search box whose placeholder already reads "Search everything — files, mail
and code" does not need a tooltip repeating it; two labels saying the same
thing is text nobody reads twice.

Static, like `test_accessible_names.py`, so it runs headless and covers files
nobody thought to test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"

#: Controls somebody acts on. **A `QLabel` is not here**: it is the text, and
#: a tooltip on a sentence explaining the sentence is noise.
ACTIONABLE = {
    "QPushButton", "QToolButton", "QCheckBox", "QRadioButton",
    "QComboBox", "QSpinBox", "QDoubleSpinBox", "QSlider", "QLineEdit",
}

#: Either of these tells somebody what the control does. A placeholder is
#: preferred where it fits - it needs no hover and survives a touch screen.
EXPLAINING = {"setToolTip", "setPlaceholderText"}

#: Controls that legitimately explain themselves some other way.
#:
#: **Each entry is a claim that has to stay true**, which is why they are
#: named individually rather than by file: a whole file excused would hide the
#: next control added to it.
ALLOWED: dict = {
    # The heading row of the results list. Its "control" is the sort order,
    # which is announced by the header text itself.
}


def _modules() -> list:
    return sorted(p for p in UI.rglob("*.py") if p.name != "__init__.py")


def _controls(tree: ast.AST) -> dict:
    """`{variable: (constructor, line)}` for every actionable control."""
    found: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        name = getattr(call.func, "id", "") or getattr(call.func, "attr", "")
        if name not in ACTIONABLE:
            continue
        for target in node.targets:
            key = getattr(target, "attr", None) or getattr(target, "id", None)
            if key:
                found[key] = (name, node.lineno)
    return found


def _explained(tree: ast.AST) -> set:
    """Variables that had a tooltip or a placeholder set on them."""
    found: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", "") not in EXPLAINING:
            continue
        owner = getattr(node.func, "value", None)
        key = getattr(owner, "attr", None) or getattr(owner, "id", None)
        if key:
            found.add(key)
    return found


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_every_control_explains_itself(path):
    r"""**A control nobody can ask about is a control nobody presses.**

    "Rerank" and "Two-phase" are not English; a tooltip is the only place this
    application gets to say what they mean, and the wording on these controls
    *is* the feature - `indexing_settings.py` says so in its own opening line.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    explained = _explained(tree)
    missing = [
        f"{path.name}:{line} {kind} `{key}`"
        for key, (kind, line) in sorted(_controls(tree).items())
        if key not in explained and key not in ALLOWED
    ]
    assert not missing, (
        "These controls have neither a tooltip nor a placeholder, so nothing "
        "in the window says what they do:\n  " + "\n  ".join(missing)
        + "\n\nSay what pressing it does and what happens at the limit, in "
          "plain words. A placeholder is better where one fits."
    )


def test_the_guard_would_notice_a_control_with_nothing_on_it():
    """A test that cannot fail is a test that proves nothing - and this one is
    a static scan, which is easy to write in a way that quietly matches
    nothing at all."""
    tree = ast.parse(
        "self.button = QPushButton('Go')\n"
        "self.other = QPushButton('Stop')\n"
        "self.other.setToolTip('Stops the run and keeps what it found.')\n")
    controls = _controls(tree)
    explained = _explained(tree)
    assert set(controls) == {"button", "other"}
    assert "other" in explained and "button" not in explained


def test_a_placeholder_counts_as_an_explanation():
    """Better than a tooltip where it fits: no hover needed, and it survives
    a touch screen."""
    tree = ast.parse(
        "self.box = QLineEdit()\n"
        "self.box.setPlaceholderText('Try: my homework about volcanoes')\n")
    assert "box" in _explained(tree)


def test_a_label_is_not_a_control():
    """A tooltip on a sentence, explaining the sentence, is noise."""
    tree = ast.parse("self.note = QLabel('Nothing has been indexed yet.')\n")
    assert _controls(tree) == {}


def test_the_allow_list_stays_short():
    """**Every entry is a claim that has to stay true.** A long list is a
    guard that has been argued down rather than met."""
    assert len(ALLOWED) <= 3
