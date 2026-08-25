"""Every control says what it is, to something that cannot see it.

Layer: L5

The review found **no accessible names anywhere in `app/ui`**. The example that
makes it concrete is the per-row checkbox in the file-types table: sixty
identical "check box, not checked" announcements, with the only thing
distinguishing them - the extension in the cell beside it - available visually
and nowhere else.

**Two shapes of control can never label themselves**, and they are the two this
checks:

* `QLineEdit` has no text of its own, ever. Its label is the placeholder, an
  accessible name, or nothing.
* `QCheckBox()` built with no string has nothing to announce. `QCheckBox("Pause
  on battery")` labels itself and is fine.

A tooltip is deliberately **not** accepted. Tooltips are not read by every
screen reader, are not reached by keyboard on some platforms, and vanish on a
touch device; treating one as a label is how a control ends up "labelled" in a
way nobody can hear.

Static, so it runs headless and covers files nobody thought to test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"

#: Constructors that produce a control with no intrinsic label.
UNLABELLED = {"QLineEdit"}

#: Naming any one of these on the same variable is enough.
LABELLING = {"setAccessibleName", "setPlaceholderText"}


def _modules() -> list[Path]:
    return sorted(p for p in UI.rglob("*.py") if p.name != "__init__.py")


def _assignments(tree: ast.AST) -> dict[str, tuple[str, int]]:
    """`{variable: (constructor, line)}` for controls that cannot label
    themselves - `QLineEdit()` always, `QCheckBox()` only when given no text."""
    found: dict[str, tuple[str, int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        name = getattr(call.func, "id", "") or getattr(call.func, "attr", "")

        unlabelled = name in UNLABELLED or (
            name == "QCheckBox" and not call.args
        )
        if not unlabelled:
            continue

        for target in node.targets:
            key = ast.unparse(target)
            found[key] = (name, node.lineno)
    return found


def _labelled(tree: ast.AST) -> set[str]:
    """Variables that have a labelling call made on them somewhere."""
    named: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LABELLING:
            continue
        named.add(ast.unparse(node.func.value))
    return named


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    labelled = _labelled(tree)
    return [
        f"{path.name}:{line} {variable} = {constructor}()"
        for variable, (constructor, line) in _assignments(tree).items()
        if variable not in labelled
    ]


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_every_control_that_cannot_label_itself_is_labelled(path: Path):
    offenders = _offenders(path)
    assert not offenders, (
        "these controls announce nothing to a screen reader:\n  "
        + "\n  ".join(offenders)
        + "\n\nGive each one setAccessibleName('...'), or a placeholder if it "
          "is a text box. A tooltip is not a label - it is not read by every "
          "reader and cannot be reached by keyboard on some platforms."
    )


#: Settings whose value is one of a known set. A free-text box for any of these
#: asks somebody to know an exact identifier and tells them nothing when they
#: get it wrong - the next start fails to download a model, by which time they
#: have forgotten what they typed.
#:
#: An **editable** combo satisfies this: the list is a suggestion, not a cage,
#: and an identifier nobody listed still works.
CHOOSABLE = {"RERANK_MODEL", "EMBED_MODEL"}


def _combo_names(path: Path) -> set[str]:
    """Object names set on something built from a QComboBox."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    combos = {
        ast.unparse(target)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        and (getattr(node.value.func, "id", "") or getattr(node.value.func, "attr", ""))
        == "QComboBox"
        for target in node.targets
    }
    named: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "setObjectName":
            continue
        if ast.unparse(node.func.value) not in combos:
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                named.add(argument.value)
    return named


def test_a_setting_with_known_values_is_offered_as_a_list():
    """No free text where the answer is one of a handful of known strings.

    `RERANK_MODEL` was a text box, and the model it held was nine times slower
    than the one the project had already measured and switched its default to -
    a choice nobody would have made if the alternatives had been on screen with
    their cost beside them.
    """
    offered: set[str] = set()
    for path in _modules():
        offered |= _combo_names(path)

    missing = sorted(CHOOSABLE - offered)
    assert not missing, (
        "these settings have a known set of sensible values but no list "
        f"anywhere in app/ui: {missing}\n"
        "Use an editable QComboBox with setObjectName(<KEY>) - editable, so an "
        "identifier nobody listed still works."
    )


def test_the_check_can_actually_fail(tmp_path: Path):
    """A guard that cannot fail is a guard nobody should trust."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from PyQt6.QtWidgets import QCheckBox, QLineEdit\n"
        "class W:\n"
        "    def __init__(self):\n"
        "        self.bare = QLineEdit()\n"
        "        self.tick = QCheckBox()\n"
        "        self.named = QCheckBox('Pause on battery')\n"
        "        self.ok = QLineEdit()\n"
        "        self.ok.setPlaceholderText('Filter')\n",
        encoding="utf-8",
    )
    found = " ".join(_offenders(sample))

    assert "self.bare" in found
    assert "self.tick" in found
    assert "self.named" not in found, "a checkbox with text labels itself"
    assert "self.ok" not in found, "a placeholder is a label"
